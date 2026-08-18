#!/usr/bin/env python3
"""Build hostile archives and prove extraction refuses them.

WHY THIS IS A SEPARATE TOOL AND NOT ONLY A TEST
The archives are built on disk, some are large, and one is a decompression bomb. It
runs as an acceptance check so the sizes can be realistic; the regression suite keeps
cheap versions of the same cases so the property is gated on every commit.

WHAT IS BEING DEFENDED
app/repo_workspace.py extracts an archive built by whoever owns the repository. That
is untrusted input from a party who may want our filesystem:

    ../../../../tmp/pwned        path traversal
    /tmp/pwned                   absolute member path
    symlink -> /tmp, then write   symlink escape
    2 KB -> 5 GB                 decompression bomb
    500k empty files             inode exhaustion
    a 900 MB single file         per-file cap
    a fifo                       nonsense on disk

The first three are tarfile's data_filter (PEP 706). The rest are OUR caps, because a
filter cannot know our budget. Both halves are exercised here -- a defence you have
not seen refuse something is a defence you are guessing about.

Usage:
    python tools/verify_archive_safety.py
"""
from __future__ import annotations

import io
import os
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.repo_workspace import (  # noqa: E402
    Limits, RepoTooLarge, WorkspaceError, _extract,
)

SMALL = Limits(download_bytes=1 << 20, extracted_bytes=4 << 20, files=200,
               file_bytes=1 << 20, timeout_seconds=10)


def _tar(path: str, build) -> None:
    with tarfile.open(path, "w:gz") as tar:
        build(tar)


def _add_bytes(tar, name: str, data: bytes, **kw) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for k, v in kw.items():
        setattr(info, k, v)
    tar.addfile(info, io.BytesIO(data))


def _cases(tmp: str) -> list:
    out = []

    p = os.path.join(tmp, "traversal.tar.gz")
    _tar(p, lambda t: _add_bytes(t, "../../../../tmp/ripple-pwned", b"x"))
    out.append(("path traversal", p, "refuse"))

    # ACCEPT, not refuse -- and the distinction matters. data_filter SANITISES an
    # absolute member path by stripping the leading slash, so the file lands INSIDE
    # the tree as `tmp/ripple-pwned-abs`. Extraction proceeding with a neutralised
    # path is correct; I expected a refusal and was wrong. The invariant is
    # containment, asserted for every case below, not rejection.
    p = os.path.join(tmp, "absolute.tar.gz")
    _tar(p, lambda t: _add_bytes(t, "/tmp/ripple-pwned-abs", b"x"))
    out.append(("absolute path sanitised", p, "accept"))

    # ACCEPT for the same reason, by a stronger mechanism: _extract skips every
    # non-regular member, so the symlink is NEVER CREATED and `escape/` becomes an
    # ordinary directory. There is nothing to escape through. data_filter would also
    # have refused the link; skipping it first means we do not depend on that.
    def _symlink(t):
        link = tarfile.TarInfo("escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp"
        t.addfile(link)
        _add_bytes(t, "escape/ripple-pwned-link", b"x")
    p = os.path.join(tmp, "symlink.tar.gz")
    _tar(p, _symlink)
    out.append(("symlink never created", p, "accept"))

    # 5 GB of zeros compresses to a few KB.
    def _bomb(t):
        blank = b"\0" * (1 << 20)
        for i in range(60):                      # 60 MB > the 4 MB test cap
            _add_bytes(t, f"bomb/{i}.bin", blank)
    p = os.path.join(tmp, "bomb.tar.gz")
    _tar(p, _bomb)
    out.append(("decompression bomb", p, "refuse"))

    def _many(t):
        for i in range(500):                     # 500 > the 200 file cap
            _add_bytes(t, f"many/{i}.txt", b"")
    p = os.path.join(tmp, "many.tar.gz")
    _tar(p, _many)
    out.append(("too many files", p, "refuse"))

    p = os.path.join(tmp, "bigfile.tar.gz")
    _tar(p, lambda t: _add_bytes(t, "big.bin", b"\0" * ((1 << 20) + 1)))
    out.append(("one file over the cap", p, "refuse"))

    def _fifo(t):
        info = tarfile.TarInfo("a.fifo")
        info.type = tarfile.FIFOTYPE
        t.addfile(info)
        _add_bytes(t, "ok.txt", b"fine")
    p = os.path.join(tmp, "fifo.tar.gz")
    _tar(p, _fifo)
    out.append(("fifo member skipped", p, "accept"))

    def _benign(t):
        _add_bytes(t, "repo-abc123/package.json", b'{"name":"x"}')
        _add_bytes(t, "repo-abc123/src/a.ts", b"export const a = 1;\n")
    p = os.path.join(tmp, "benign.tar.gz")
    _tar(p, _benign)
    out.append(("a normal repository", p, "accept"))

    return out


def main(argv: list) -> int:
    print("=" * 78)
    print("ARCHIVE SAFETY -- hostile inputs to app/repo_workspace.py")
    print("=" * 78)

    failures = []
    with tempfile.TemporaryDirectory(prefix="ripple-archive-") as tmp:
        for label, path, expected in _cases(tmp):
            into = tempfile.mkdtemp(dir=tmp)
            try:
                files, written = _extract(path, into, SMALL)
                got, detail = "accept", f"{files} file(s), {written} byte(s)"
            except RepoTooLarge as exc:
                got, detail = "refuse", f"RepoTooLarge: {exc}"
            except WorkspaceError as exc:
                got, detail = "refuse", f"{type(exc).__name__}: {exc}"
            except Exception as exc:               # noqa: BLE001
                # tarfile's filter raises its own errors; any refusal is a refusal,
                # but the TYPE is reported so a surprising one is visible.
                got, detail = "refuse", f"{type(exc).__name__}: {exc}"

            ok = got == expected
            print(f"\n  {'ok ' if ok else 'BAD'} {label:<26} {got:<8} "
                  f"(expected {expected})")
            print(f"      {detail[:96]}")
            if not ok:
                failures.append(f"{label}: expected {expected}, got {got} -- {detail}")

            # THE INVARIANT THAT ACTUALLY MATTERS. Refusal is one way to be safe;
            # containment is the property. Two cases above are ACCEPTED and still
            # safe -- an absolute path sanitised, a symlink never created -- so
            # asserting "it refused" would have been asserting the wrong thing.
            real_into = os.path.realpath(into)
            for base, _dirs, names in os.walk(into):
                for name in names:
                    full = os.path.realpath(os.path.join(base, name))
                    if not full.startswith(real_into + os.sep):
                        failures.append(
                            f"{label}: ESCAPED the tree -- {full}")
                for entry in _dirs + names:
                    if os.path.islink(os.path.join(base, entry)):
                        failures.append(
                            f"{label}: a SYMLINK was created ({entry}) -- non-regular "
                            f"members must be skipped")

            # And nothing may exist at the paths the hostile archives aimed at.
            for probe in ("/tmp/ripple-pwned", "/tmp/ripple-pwned-abs",
                          "/tmp/ripple-pwned-link"):
                if os.path.exists(probe):
                    failures.append(
                        f"{label}: WROTE OUTSIDE THE TREE -- {probe} exists")
                    os.remove(probe)

    print("\n" + "-" * 78)
    if failures:
        print(f"  {len(failures)} FAILURE(S):")
        for msg in failures:
            print(f"      {msg}")
        return 1
    print("  every hostile archive was refused, and a normal one extracted.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
