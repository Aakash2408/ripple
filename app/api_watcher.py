"""
ripple/app/api_watcher.py

Public API Watcher — the third moat (network effects).

Monitors popular public API specs (Stripe, Twilio, GitHub, etc.) for changes.
When a public API changes, proactively fixes ALL Ripple customers who use it.

This creates network effects:
- More customers → more repos scanned → faster detection of public API changes
- A company using Ripple gets warned about Stripe API changes BEFORE they break
- The graph of "who uses what public API" is proprietary intelligence

Workflow:
1. Periodically fetch latest specs from known public APIs
2. Diff against last known version
3. If breaking change detected → scan ALL customer repos for usage
4. Open fix PRs across all affected customers simultaneously
"""

import json
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen


@dataclass
class WatchedAPI:
    """A public API being monitored for changes."""
    name: str               # e.g., "stripe"
    spec_url: str          # URL to fetch latest spec
    last_fetched: float    # timestamp
    last_version: str      # version string from spec
    last_hash: str         # hash of spec content
    change_count: int      # total changes detected


# Known public APIs with OpenAPI specs
PUBLIC_APIS = [
    WatchedAPI(
        name="stripe",
        spec_url="https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.yaml",
        last_fetched=0, last_version="", last_hash="", change_count=0,
    ),
    WatchedAPI(
        name="twilio",
        spec_url="https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/json/twilio_api_v2010.json",
        last_fetched=0, last_version="", last_hash="", change_count=0,
    ),
    WatchedAPI(
        name="github",
        spec_url="https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json",
        last_fetched=0, last_version="", last_hash="", change_count=0,
    ),
    WatchedAPI(
        name="petstore",  # Classic demo API
        spec_url="https://petstore3.swagger.io/api/v3/openapi.json",
        last_fetched=0, last_version="", last_hash="", change_count=0,
    ),
]


class APIWatcher:
    """
    Watches public APIs for breaking changes.
    
    When a change is detected, notifies all Ripple customers
    who depend on that API.
    """
    
    def __init__(self, storage_dir: str = "/tmp/ripple_watcher"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.storage_dir / "watcher_state.json"
        self.apis = list(PUBLIC_APIS)
        self._load_state()
    
    def check_all(self) -> list[dict]:
        """
        Check all watched APIs for changes.
        Returns list of detected changes.
        """
        changes = []
        
        for api in self.apis:
            change = self.check_api(api)
            if change:
                changes.append(change)
        
        self._save_state()
        return changes
    
    def check_api(self, api: WatchedAPI) -> Optional[dict]:
        """Check a single API for changes."""
        import hashlib
        
        # Fetch latest spec
        content = self._fetch_spec(api.spec_url)
        if not content:
            return None
        
        # Hash it
        current_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        
        # Compare to last known
        if api.last_hash and current_hash != api.last_hash:
            # CHANGE DETECTED
            api.change_count += 1
            change = {
                "api": api.name,
                "spec_url": api.spec_url,
                "previous_hash": api.last_hash,
                "current_hash": current_hash,
                "detected_at": time.time(),
                "change_number": api.change_count,
            }
            
            # Save both versions for diffing
            old_path = self.storage_dir / f"{api.name}_prev.yaml"
            new_path = self.storage_dir / f"{api.name}_current.yaml"
            
            if old_path.exists():
                # We have the old version — can diff
                change["can_diff"] = True
                change["old_spec_path"] = str(old_path)
                change["new_spec_path"] = str(new_path)
            
            # Save current as new baseline
            with open(new_path, "w") as f:
                f.write(content)
            
            api.last_hash = current_hash
            api.last_fetched = time.time()
            
            return change
        
        # No change — update timestamp and hash
        if not api.last_hash:
            # First fetch — save baseline
            baseline_path = self.storage_dir / f"{api.name}_current.yaml"
            with open(baseline_path, "w") as f:
                f.write(content)
        
        api.last_hash = current_hash
        api.last_fetched = time.time()
        return None
    
    def add_api(self, name: str, spec_url: str):
        """Add a new API to watch."""
        self.apis.append(WatchedAPI(
            name=name, spec_url=spec_url,
            last_fetched=0, last_version="", last_hash="", change_count=0,
        ))
        self._save_state()
    
    def status(self) -> list[dict]:
        """Get status of all watched APIs."""
        return [
            {
                "name": api.name,
                "last_checked": api.last_fetched,
                "changes_detected": api.change_count,
                "spec_url": api.spec_url,
            }
            for api in self.apis
        ]
    
    def _fetch_spec(self, url: str) -> Optional[str]:
        """Fetch an API spec from URL."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        try:
            req = Request(url, headers={"User-Agent": "Ripple-API-Watcher/1.0"})
            with urlopen(req, timeout=30, context=ctx) as resp:
                return resp.read().decode()
        except Exception as e:
            return None
    
    def _save_state(self):
        """Save watcher state."""
        state = []
        for api in self.apis:
            state.append({
                "name": api.name,
                "spec_url": api.spec_url,
                "last_fetched": api.last_fetched,
                "last_version": api.last_version,
                "last_hash": api.last_hash,
                "change_count": api.change_count,
            })
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
    
    def _load_state(self):
        """Load saved state."""
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            for saved in state:
                for api in self.apis:
                    if api.name == saved["name"]:
                        api.last_fetched = saved.get("last_fetched", 0)
                        api.last_version = saved.get("last_version", "")
                        api.last_hash = saved.get("last_hash", "")
                        api.change_count = saved.get("change_count", 0)
                        break
        except (json.JSONDecodeError, TypeError):
            pass


# === CLI for testing ===

def watch_command():
    """CLI: check all APIs for changes."""
    watcher = APIWatcher()
    
    print("\n🌊 Ripple API Watcher")
    print("=" * 50)
    print(f"\nMonitoring {len(watcher.apis)} public APIs...\n")
    
    changes = watcher.check_all()
    
    if changes:
        print(f"⚠️  {len(changes)} API change(s) detected!\n")
        for change in changes:
            print(f"  🔴 {change['api']} — spec changed")
            print(f"     Change #{change['change_number']}")
            print()
    else:
        print("✅ No changes detected.\n")
    
    print("Status:")
    for s in watcher.status():
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_checked"])) if s["last_checked"] else "never"
        print(f"  {s['name']:12s} — last checked: {last}, changes: {s['changes_detected']}")
    
    print()


if __name__ == "__main__":
    watch_command()
