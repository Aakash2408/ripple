"""HAND-WRITTEN types, not generated -- see the TypeScript sibling fixture for why
that matters: a generated file would be repaired by re-running codegen and Ripple
would have no job.

The phone_number field is annotated as an integer, matching user.before.yaml
(type: integer). The spec has since changed it to a string, so this annotation is
now WRONG and the code in contact.py that treats it as a string cannot typecheck
against the new contract.

THIS FILE IS THE FIX TARGET. As with the TypeScript cell, a type change leaves the
usages correct and the DECLARATION stale, so the fix edits the declaration.

Exactly ONE integer-annotated field here, deliberately: _change_type_python()
rewrites every occurrence of the old type annotation in the file and ignores
field_name, so a second integer-annotated field would be silently retyped too and
the diff would stop being minimal.

Note also that this prose avoids writing the annotation pattern out literally. The
codemod does not respect comments, so a docstring containing it would itself be
rewritten -- which is how the first draft of this fixture reported "3 type
annotations updated" for a one-field change, two of them inside this very comment.
"""

from dataclasses import dataclass


@dataclass
class User:
    id: str
    email: str
    full_name: str
    phone_number: int
