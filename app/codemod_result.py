"""The result contract shared by every per-language codemod.

WHY THIS IS NOT DEFINED IN ts_codemod.py ANY MORE
It was, until a Python codemod needed the same three-way classification. Importing
it from a module named `ts_codemod` would have made the name a lie, and duplicating
the dataclass would have put TWO definitions of `complete` in the tree -- the
"two copies of a rule" defect this codebase has removed twice already, once from the
capability registry and once from the production predicate.

Same reasoning that moved the comment/string scanner to app/source_regions.py when
Python was added. This module holds no logic beyond the one derived property.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field


@dataclass
class CodemodResult:
    code: str
    changed: bool
    edits: list = _field(default_factory=list)
    refusals: list = _field(default_factory=list)
    notes: list = _field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every reference that MATTERS was handled.

        Notes are excluded deliberately: a comment or string mentioning the field
        is not a compile error, so letting it veto the fix would block almost every
        real consumer. A partial removal, by contrast, still leaves a type error, so
        "some edits" is not success -- it is a different failure.
        """
        return self.changed and not self.refusals
