from __future__ import annotations
"""
ripple/app/playbook_engine.py

Pattern Playbook Engine — PropBench PatternOracle integrated into Yoke.

Based on PropBench finding: Pattern recognition alone achieves 55% package-level
recall. Combined with Structure (ensemble) = 82%.

Playbooks encode: "When THIS type of change happens, THESE files always need updating."

Instead of searching one consumer at a time, playbooks predict ALL
affected files from the change type alone — before any grep/search.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PlaybookPrediction:
    """A predicted consumer from a playbook match."""
    file_pattern: str     # glob pattern or exact file
    confidence: float
    reason: str
    source: str           # which playbook predicted this


# === Playbook Definitions ===
# Each playbook: trigger condition → predicted consumers

PLAYBOOKS = [
    {
        "id": "openapi_field_change",
        "trigger": {
            "contract_type": "openapi",
            "change_types": ["added_required_field", "removed_field", "field_type_changed"],
        },
        "predictions": [
            {"pattern": "*_test.*", "confidence": 0.85, "reason": "API tests need updating when contract changes"},
            {"pattern": "*_client.*", "confidence": 0.80, "reason": "Client SDKs call the changed endpoint"},
            {"pattern": "*Client.*", "confidence": 0.80, "reason": "Client classes call the changed endpoint"},
            {"pattern": "*/docs/*", "confidence": 0.60, "reason": "API documentation references the endpoint"},
            {"pattern": "CHANGELOG*", "confidence": 0.55, "reason": "Changelog should mention breaking change"},
            {"pattern": "*.md", "confidence": 0.40, "reason": "README/docs may reference the API"},
        ],
    },
    {
        "id": "proto_field_change",
        "trigger": {
            "contract_type": "proto",
            "change_types": ["field_removed", "field_type_changed", "required_field_added", "message_removed"],
        },
        "predictions": [
            {"pattern": "*_pb2.py", "confidence": 0.95, "reason": "Generated Python proto code needs regeneration"},
            {"pattern": "*.pb.go", "confidence": 0.95, "reason": "Generated Go proto code needs regeneration"},
            {"pattern": "*_grpc.*", "confidence": 0.90, "reason": "gRPC generated code depends on proto"},
            {"pattern": "*_test.*", "confidence": 0.85, "reason": "Tests using proto messages need updating"},
            {"pattern": "*service*", "confidence": 0.70, "reason": "Service implementations use proto types"},
        ],
    },
    {
        "id": "graphql_schema_change",
        "trigger": {
            "contract_type": "graphql",
            "change_types": ["field_removed", "field_made_required", "type_removed", "required_argument_added"],
        },
        "predictions": [
            {"pattern": "*.generated.*", "confidence": 0.95, "reason": "Generated types from schema need regeneration"},
            {"pattern": "*resolver*", "confidence": 0.85, "reason": "Resolvers implement the schema fields"},
            {"pattern": "*query*", "confidence": 0.80, "reason": "Query files select the changed fields"},
            {"pattern": "*mutation*", "confidence": 0.80, "reason": "Mutations reference the changed types"},
            {"pattern": "*fragment*", "confidence": 0.75, "reason": "Fragments spread on the changed types"},
            {"pattern": "*_test.*", "confidence": 0.70, "reason": "Tests using GraphQL queries need updating"},
        ],
    },
    {
        "id": "database_schema_change",
        "trigger": {
            "contract_type": "database",
            "change_types": ["column_removed", "column_type_changed", "table_removed", "column_made_not_null"],
        },
        "predictions": [
            {"pattern": "*model*", "confidence": 0.90, "reason": "ORM models map to database columns"},
            {"pattern": "*repository*", "confidence": 0.85, "reason": "Repository/DAO queries the changed table"},
            {"pattern": "*migration*", "confidence": 0.80, "reason": "New migration needed for the schema change"},
            {"pattern": "*entity*", "confidence": 0.80, "reason": "Entity classes map to database tables"},
            {"pattern": "*_test.*", "confidence": 0.70, "reason": "Tests with database fixtures need updating"},
            {"pattern": "*seed*", "confidence": 0.60, "reason": "Seed/fixture data may reference changed columns"},
        ],
    },
]


class PlaybookEngine:
    """
    Matches changes to known playbooks and predicts all affected files.
    
    This is the Pattern Oracle from PropBench, applied to Yoke:
    - 55% package-level recall from patterns alone
    - Near-perfect precision (rarely predicts wrong things)
    - Fires instantly (no search needed — just pattern matching)
    """
    
    def __init__(self):
        self.playbooks = PLAYBOOKS
    
    def predict(self, contract_type: str, change_type: str) -> list[PlaybookPrediction]:
        """
        Given a contract type + change type, return predicted consumer patterns.
        
        Args:
            contract_type: "openapi", "proto", "graphql", "database"
            change_type: e.g., "added_required_field", "field_removed"
            
        Returns:
            List of predicted file patterns with confidence
        """
        predictions = []
        
        for playbook in self.playbooks:
            trigger = playbook["trigger"]
            
            if (trigger["contract_type"] == contract_type and
                change_type in trigger["change_types"]):
                
                for pred in playbook["predictions"]:
                    predictions.append(PlaybookPrediction(
                        file_pattern=pred["pattern"],
                        confidence=pred["confidence"],
                        reason=pred["reason"],
                        source=playbook["id"],
                    ))
        
        # Sort by confidence descending
        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions
    
    def get_playbook_for_change(self, contract_type: str, change_type: str) -> Optional[dict]:
        """Get the matching playbook definition."""
        for playbook in self.playbooks:
            trigger = playbook["trigger"]
            if (trigger["contract_type"] == contract_type and
                change_type in trigger["change_types"]):
                return playbook
        return None


class EnsembleConsumerFinder:
    """
    Ensemble Consumer Finder — combines all knowledge sources.
    
    Based on PropBench: Pattern + Structure + History = 82% recall.
    
    Combines:
    1. Grep (current baseline — finds direct references)
    2. Playbooks (Pattern Oracle — predicts by change type)
    3. History (Historian — predicts by co-change frequency)
    4. Multi-invoker (Structure Oracle — finds shared resources)
    
    Each source contributes predictions. Duplicates are merged,
    confidence is boosted when multiple sources agree.
    """
    
    def __init__(self, playbook_engine: PlaybookEngine, learner=None, detector=None):
        self.playbook_engine = playbook_engine
        self.learner = learner
        self.detector = detector
    
    def find_all_consumers(
        self,
        changed_file: str,
        contract_type: str,
        change_type: str,
        grep_results: list = None,
    ) -> list[dict]:
        """
        Combine all knowledge sources to find consumers.
        
        Returns ranked list of predicted consumers with source attribution.
        """
        all_predictions: dict[str, dict] = {}  # file → {confidence, sources, reasons}
        
        # Source 1: Grep results (if provided)
        if grep_results:
            for result in grep_results:
                file_path = result if isinstance(result, str) else getattr(result, 'file_path', str(result))
                self._add_prediction(all_predictions, file_path, 0.7, "grep", "Direct reference found in code")
        
        # Source 2: Playbook predictions
        playbook_preds = self.playbook_engine.predict(contract_type, change_type)
        for pred in playbook_preds:
            self._add_prediction(all_predictions, pred.file_pattern, pred.confidence, "playbook", pred.reason)
        
        # Source 3: History predictions
        if self.learner:
            history_preds = self.learner.predict_consumers(changed_file, top_n=10)
            for file_path, confidence, reason in history_preds:
                self._add_prediction(all_predictions, file_path, confidence, "history", reason)
        
        # Source 4: Multi-invoker detection
        if self.detector:
            warning = self.detector.check(changed_file)
            if warning:
                for invoker in warning.invokers:
                    self._add_prediction(
                        all_predictions, invoker.file_path,
                        invoker.confidence, "multi-invoker",
                        "Shared resource — multiple consumers detected"
                    )
        
        # Convert to sorted list
        results = []
        for file_path, data in all_predictions.items():
            results.append({
                "file": file_path,
                "confidence": min(0.99, data["confidence"]),  # cap at 99%
                "sources": data["sources"],
                "reasons": data["reasons"],
                "source_count": len(data["sources"]),
            })
        
        # Sort: multi-source predictions first, then by confidence
        results.sort(key=lambda r: (r["source_count"], r["confidence"]), reverse=True)
        return results
    
    def _add_prediction(self, predictions: dict, file_path: str, confidence: float, source: str, reason: str):
        """Add or merge a prediction."""
        if file_path not in predictions:
            predictions[file_path] = {
                "confidence": confidence,
                "sources": [source],
                "reasons": [reason],
            }
        else:
            existing = predictions[file_path]
            # Boost confidence when multiple sources agree
            existing["confidence"] = min(0.99, existing["confidence"] + confidence * 0.2)
            if source not in existing["sources"]:
                existing["sources"].append(source)
            existing["reasons"].append(reason)
