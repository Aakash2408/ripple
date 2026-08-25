"""Measure whether each language's codemod tells the truth about what it did.

THE LYING PATTERN, DEFINED
A codemod lies when its explanation asserts a removal that did not happen. Two
observable forms:

    NO-OP LIE      explanation claims success, target token still present in code
    PHANTOM LIE    changed=True but only whitespace/comments moved, so the pipeline
                   believes a fix exists

Two forms are worse than a lie and measured separately:

    COLLATERAL     lines were deleted that do NOT mention the target -- the Python
                   type_removed defect, which dropped a live import neighbour
    NEIGHBOUR LOSS an identifier that is NOT the target disappeared from the file

NEIGHBOUR LOSS EXISTS BECAUSE COLLATERAL COULD NOT SEE THE WORST BUG
`_collateral_lines` only reports a deleted line that never mentioned the target. The
most destructive defect this tool was written to find does not qualify:

    import { User, Address } from './models';      <- DOES mention the target

Deleting that whole line to remove `User` takes `Address` with it, and the line is
invisible to the collateral check because `User` is on it. So the surviving-identifier
check is separate: each import fixture names a `neighbour` that must still be present
afterwards. Same defect shape in python, typescript and javascript, and the collateral
check missed all three.

SCOPE: ALL FIFTEEN LANGUAGES
This started at the six with a handler but no wired validator, on the reasoning that
typescript, python and go have a compiler that would reject a bad patch. That
reasoning was wrong twice over and the widening is a direct consequence:

  * the destructive import bug was found in TYPESCRIPT, a language with a wired
    validator. A validator only helps if the cell is validated at all, and
    remove_type is not a proven cell in any language -- so the bad patch shipped as
    a PR that reported success.
  * dart, php, scala, shell, swift and yaml have no handler, so they reach the
    generic fallback, and that fallback destroyed every one of them. They were
    outside the old scope entirely.

A gate that structurally cannot see the bugs it was written for is a vacuous pass.
Fixtures are skipped -- visibly, and listed in the output -- where a shape genuinely
does not exist in a language (Go imports packages, not types; shell has no types).

KNOWN GAPS, STATED RATHER THAN PAPERED OVER
Two real weaknesses this tool does NOT fail on, because neither is a lie and inventing
a failure for them would only pressure someone into loosening the checks:

1. For the six fallthrough languages `remove_field` still routes through
   `_generic_remove`, which deletes the whole line containing a reference. That
   removes functionality rather than corrupting syntax, and the line does mention the
   target, so it is neither a lie nor collateral nor neighbour loss.

2. PARTIAL RENAME in shell, found by this widening and not yet fixed. Measured:

       in    phone_number="x"          out   phone="x"
             echo "$phone_number"            echo "$phone_number"
       claim "Renamed 'phone_number' -> 'phone' ... 1 replacements made."

   The declaration was renamed and every USE was not, so the script now reads a
   variable that no longer exists. Root cause is upstream in app/source_regions.py,
   whose own docstring says it: `SCANNED = ("typescript", "javascript", "python")` and
   "anything else falls back to the TypeScript rules ... callers that care must gate
   on this set". `_rename_field` does not gate on it, so in shell `"$var"` is read as
   inert string CONTENT and skipped, while `#!/bin/sh` is read as code. It is the same
   interpolation problem `source_regions` already solves for TS template literals and
   Python f-strings, in a language it has no scanner for.

   Fixing it means adding a shell scanner to a module the diff contract also uses, so
   it is a separate change rather than one smuggled into this one. "Renames the
   declaration but not the uses" is a fourth failure mode and this tool does not claim
   to measure it.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import languages  # noqa: E402
from app.fix_templates import apply_fix_template  # noqa: E402

#: Every language with any fix path. See the module docstring for why this is no
#: longer the six-without-a-validator subset.
TARGETS = tuple(sorted(languages()))

#: Idiomatic consumers. Each has a DECLARATION file and a USAGE file, because the
#: two are different targets: a removal must edit usages (the declaration is
#: regenerated), a retype must edit the declaration (the usages are correct).
#: `token` is the identifier as that language actually spells it.
#:
#: `imports` and `enum` are OPTIONAL: omit them where the shape does not exist in the
#: language. `imports` must name a `neighbour` -- a second identifier on the same
#: statement that has to survive, which is the whole point of that probe.
CASES = {
    "csharp": {
        "token": "PhoneNumber",
        "decl": (
            "public class User\n"
            "{\n"
            "    public string Id { get; set; }\n"
            "    public string Email { get; set; }\n"
            "    public string PhoneNumber { get; set; }\n"
            "}\n"),
        "usage": (
            "public class Formatter\n"
            "{\n"
            "    public string FormatContact(User u)\n"
            "    {\n"
            "        return $\"{u.Email} {u.PhoneNumber}\";\n"
            "    }\n"
            "}\n"),
        "decl_typed": (
            "public class User\n"
            "{\n"
            "    public string Id { get; set; }\n"
            "    public int PhoneNumber { get; set; }\n"
            "}\n"),
        "imports": (
            "using Models.User;\n"
            "using Models.Address;\n"
            "\n"
            "public class Labeller\n"
            "{\n"
            "    public string Label(Address a) { return a.City; }\n"
            "}\n"),
        "neighbour": "Address",
        "enum": (
            "public enum Status\n"
            "{\n"
            "    ACTIVE,\n"
            "    PENDING,\n"
            "}\n"),
    },
    "dart": {
        "token": "phoneNumber",
        "decl": (
            "class User {\n"
            "  String id;\n"
            "  String email;\n"
            "  String phoneNumber;\n"
            "}\n"),
        "usage": (
            "String formatContact(User u) {\n"
            "  return '${u.email} ${u.phoneNumber}';\n"
            "}\n"),
        "decl_typed": (
            "class User {\n"
            "  String id;\n"
            "  int phoneNumber;\n"
            "}\n"),
        "imports": (
            "import 'package:app/models.dart' show User, Address;\n"
            "\n"
            "String label(Address a) => a.city;\n"),
        "neighbour": "Address",
        "enum": (
            "enum Status {\n"
            "  ACTIVE,\n"
            "  PENDING,\n"
            "}\n"),
    },
    "go": {
        "token": "PhoneNumber",
        "decl": (
            "type User struct {\n"
            "\tID          string\n"
            "\tEmail       string\n"
            "\tPhoneNumber string\n"
            "}\n"),
        "usage": (
            "func FormatContact(u *User) string {\n"
            "\treturn u.Email + \" \" + u.PhoneNumber\n"
            "}\n"),
        "decl_typed": (
            "type User struct {\n"
            "\tID          string\n"
            "\tPhoneNumber int32\n"
            "}\n"),
        # No `imports` fixture: Go imports PACKAGES, not types, so there is no
        # multi-name type-import statement to corrupt.
        "enum": (
            "const (\n"
            "\tStatusACTIVE = \"ACTIVE\"\n"
            "\tStatusPENDING = \"PENDING\"\n"
            ")\n"),
    },
    "java": {
        "token": "phoneNumber",
        "decl": (
            "public class User {\n"
            "    private String id;\n"
            "    private String email;\n"
            "    private String phoneNumber;\n"
            "}\n"),
        "usage": (
            "public class Formatter {\n"
            "    public String formatContact(User u) {\n"
            "        return String.format(\"%s %s\", u.email, u.phoneNumber);\n"
            "    }\n"
            "\n"
            "    public String viaGetter(User u) {\n"
            "        return u.getPhoneNumber();\n"
            "    }\n"
            "}\n"),
        "decl_typed": (
            "public class User {\n"
            "    private String id;\n"
            "    private int phoneNumber;\n"
            "}\n"),
        "imports": (
            "import models.User;\n"
            "import models.Address;\n"
            "\n"
            "public class Labeller {\n"
            "    String label(Address a) { return a.city; }\n"
            "}\n"),
        "neighbour": "Address",
        "enum": (
            "public enum Status {\n"
            "    ACTIVE,\n"
            "    PENDING\n"
            "}\n"),
    },
    "javascript": {
        "token": "phoneNumber",
        "decl": (
            "export const UserShape = {\n"
            "  id: \"\",\n"
            "  email: \"\",\n"
            "  phoneNumber: \"\",\n"
            "};\n"),
        "usage": (
            "export function formatContact(user) {\n"
            "  return `${user.email} ${user.phoneNumber}`;\n"
            "}\n"
            "\n"
            "export function toPayload(user) {\n"
            "  return {\n"
            "    id: user.id,\n"
            "    phone: user.phoneNumber,\n"
            "  };\n"
            "}\n"),
        "decl_typed": (
            "export const UserShape = {\n"
            "  id: \"\",\n"
            "  phoneNumber: 0,\n"
            "};\n"),
        "imports": (
            "const { User, Address } = require('./models');\n"
            "\n"
            "module.exports = { Address };\n"),
        "neighbour": "Address",
        "enum": (
            "export const Status = {\n"
            "  ACTIVE: 'ACTIVE',\n"
            "  PENDING: 'PENDING',\n"
            "};\n"),
    },
    "kotlin": {
        "token": "phoneNumber",
        "decl": (
            "data class User(\n"
            "    val id: String,\n"
            "    val email: String,\n"
            "    val phoneNumber: String,\n"
            ")\n"),
        "usage": (
            "fun formatContact(u: User): String {\n"
            "    return \"${u.email} ${u.phoneNumber}\"\n"
            "}\n"),
        "decl_typed": (
            "data class User(\n"
            "    val id: String,\n"
            "    val phoneNumber: Int,\n"
            ")\n"),
        "imports": (
            "import models.User\n"
            "import models.Address\n"
            "\n"
            "fun label(a: Address): String = a.city\n"),
        "neighbour": "Address",
        "enum": (
            "enum class Status {\n"
            "    ACTIVE,\n"
            "    PENDING,\n"
            "}\n"),
    },
    "php": {
        "token": "phone_number",
        "decl": (
            "<?php\n"
            "class User\n"
            "{\n"
            "    public $id;\n"
            "    public $email;\n"
            "    public $phone_number;\n"
            "}\n"),
        "usage": (
            "<?php\n"
            "function format_contact($u)\n"
            "{\n"
            "    return $u->email . ' ' . $u->phone_number;\n"
            "}\n"),
        "decl_typed": (
            "<?php\n"
            "class User\n"
            "{\n"
            "    public string $id;\n"
            "    public int $phone_number;\n"
            "}\n"),
        "imports": (
            "<?php\n"
            "use App\\Models\\User;\n"
            "use App\\Models\\Address;\n"
            "\n"
            "function label(Address $a) { return $a->city; }\n"),
        "neighbour": "Address",
        "enum": (
            "<?php\n"
            "enum Status: string\n"
            "{\n"
            "    case ACTIVE = 'ACTIVE';\n"
            "    case PENDING = 'PENDING';\n"
            "}\n"),
    },
    "python": {
        "token": "phone_number",
        "decl": (
            "class User:\n"
            "    id: str\n"
            "    email: str\n"
            "    phone_number: str\n"),
        "usage": (
            "def format_contact(u: User) -> str:\n"
            "    return f\"{u.email} {u.phone_number}\"\n"),
        "decl_typed": (
            "class User:\n"
            "    id: str\n"
            "    phone_number: int\n"),
        "imports": (
            "from src.models import User, Address\n"
            "\n"
            "\n"
            "def label(a: Address) -> str:\n"
            "    return a.city\n"),
        "neighbour": "Address",
        "enum": (
            "class Status(Enum):\n"
            "    ACTIVE = \"ACTIVE\"\n"
            "    PENDING = \"PENDING\"\n"),
    },
    "ruby": {
        "token": "phone_number",
        "decl": (
            "class User\n"
            "  attr_accessor :id, :email, :phone_number\n"
            "end\n"),
        "usage": (
            "def format_contact(u)\n"
            "  \"#{u.email} #{u.phone_number}\"\n"
            "end\n"),
        "decl_typed": (
            "class User\n"
            "  attr_accessor :id, :phone_number\n"
            "end\n"),
        "imports": (
            "require 'models/user'\n"
            "require 'models/address'\n"
            "\n"
            "def label(a)\n"
            "  Address.name_of(a)\n"
            "end\n"),
        "neighbour": "Address",
        "enum": (
            "module Status\n"
            "  ACTIVE = 'ACTIVE'\n"
            "  PENDING = 'PENDING'\n"
            "end\n"),
    },
    "rust": {
        "token": "phone_number",
        "decl": (
            "pub struct User {\n"
            "    pub id: String,\n"
            "    pub email: String,\n"
            "    pub phone_number: String,\n"
            "}\n"),
        "usage": (
            "pub fn format_contact(u: &User) -> String {\n"
            "    format!(\"{} {}\", u.email, u.phone_number)\n"
            "}\n"),
        "decl_typed": (
            "pub struct User {\n"
            "    pub id: String,\n"
            "    pub phone_number: i32,\n"
            "}\n"),
        "imports": (
            "use models::{User, Address};\n"
            "\n"
            "pub fn label(a: &Address) -> String {\n"
            "    a.city.clone()\n"
            "}\n"),
        "neighbour": "Address",
        "enum": (
            "pub enum Status {\n"
            "    ACTIVE,\n"
            "    PENDING,\n"
            "}\n"),
    },
    "scala": {
        "token": "phoneNumber",
        "decl": (
            "case class User(\n"
            "  id: String,\n"
            "  email: String,\n"
            "  phoneNumber: String,\n"
            ")\n"),
        "usage": (
            "def formatContact(u: User): String =\n"
            "  s\"${u.email} ${u.phoneNumber}\"\n"),
        "decl_typed": (
            "case class User(\n"
            "  id: String,\n"
            "  phoneNumber: Int,\n"
            ")\n"),
        "imports": (
            "import models.User\n"
            "import models.Address\n"
            "\n"
            "def label(a: Address): String = a.city\n"),
        "neighbour": "Address",
        "enum": (
            "sealed trait Status\n"
            "case object ACTIVE extends Status\n"
            "case object PENDING extends Status\n"),
    },
    "shell": {
        "token": "phone_number",
        "decl": (
            "#!/bin/sh\n"
            "id=\"\"\n"
            "email=\"\"\n"
            "phone_number=\"\"\n"),
        "usage": (
            "#!/bin/sh\n"
            "printf '%s %s\\n' \"$email\" \"$phone_number\"\n"),
        "decl_typed": (
            "#!/bin/sh\n"
            "id=\"\"\n"
            "phone_number=0\n"),
        # No `imports`: shell has no types, so there is no type-import to corrupt.
        "enum": (
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  ACTIVE) echo go ;;\n"
            "  PENDING) echo wait ;;\n"
            "esac\n"),
    },
    "swift": {
        "token": "phoneNumber",
        "decl": (
            "struct User {\n"
            "    let id: String\n"
            "    let email: String\n"
            "    let phoneNumber: String\n"
            "}\n"),
        "usage": (
            "func formatContact(_ u: User) -> String {\n"
            "    return \"\\(u.email) \\(u.phoneNumber)\"\n"
            "}\n"),
        "decl_typed": (
            "struct User {\n"
            "    let id: String\n"
            "    let phoneNumber: Int\n"
            "}\n"),
        # Swift imports MODULES, not types. `import struct Models.User` exists but is
        # rare; the common shape has no neighbour to lose.
        "enum": (
            "enum Status {\n"
            "    case ACTIVE\n"
            "    case PENDING\n"
            "}\n"),
    },
    "typescript": {
        "token": "phoneNumber",
        "decl": (
            "export interface User {\n"
            "  id: string;\n"
            "  email: string;\n"
            "  phoneNumber: string;\n"
            "}\n"),
        "usage": (
            "export function formatContact(u: User): string {\n"
            "  return `${u.email} ${u.phoneNumber}`;\n"
            "}\n"),
        "decl_typed": (
            "export interface User {\n"
            "  id: string;\n"
            "  phoneNumber: number;\n"
            "}\n"),
        "imports": (
            "import { User, Address } from './models';\n"
            "\n"
            "export function label(a: Address): string {\n"
            "  return a.city;\n"
            "}\n"),
        "neighbour": "Address",
        "enum": (
            "export enum Status {\n"
            "  ACTIVE = 'ACTIVE',\n"
            "  PENDING = 'PENDING',\n"
            "}\n"),
    },
    "yaml": {
        "token": "phone_number",
        "decl": (
            "User:\n"
            "  properties:\n"
            "    id: {type: string}\n"
            "    email: {type: string}\n"
            "    phone_number: {type: string}\n"),
        "usage": (
            "defaults:\n"
            "  email: none\n"
            "  phone_number: none\n"),
        "decl_typed": (
            "User:\n"
            "  properties:\n"
            "    id: {type: string}\n"
            "    phone_number: {type: integer}\n"),
        # No `imports`: a $ref is a reference, not a multi-name import statement.
        "enum": (
            "Status:\n"
            "  enum:\n"
            "    - ACTIVE\n"
            "    - PENDING\n"),
    },
}

REFUSAL_MARKERS = ("Could NOT", "PARTIAL:", "RIPPLE-ACTION-REQUIRED",
                   "NEEDS A HUMAN", "Error:",
                   # An explicit "0 replacements made" is an honest disclosure, not a
                   # claim of success, and treating it as one produced a FALSE
                   # ACCUSATION against shell's rename_field. The full text reads
                   # "Renamed 'x' -> 'y' across all case variants (...). 0 replacements
                   # made." -- the first sentence describes the attempt, the second
                   # reports the outcome truthfully.
                   "0 replacements made")


def _claims_success(explanation: str) -> bool:
    return not any(m in explanation for m in REFUSAL_MARKERS)


def _case_variants(token: str) -> set:
    """Every spelling a codemod might match, so "mentions the target" agrees with how
    the codemods actually search.

    Without this the collateral check FALSELY ACCUSED ruby: it deleted
    `require 'models/user'` to remove the type `User`, which is exactly right, but the
    line does not contain the literal string `User` -- only the lowercase form in a
    path. A detector that reports correct behaviour as damage is the same defect class
    it is looking for, and this is the second time that has happened in this file.
    """
    from app.fix_templates import name_variants
    return {v for v in name_variants(token).values() if v}


def _collateral_lines(before: str, after: str, token: str) -> list:
    """Lines present before and gone after that never mentioned the token.

    "Mentioned" spans every case variant -- see _case_variants. Losing a NEIGHBOUR is
    a separate and stricter check, because the destructive-import bug deletes a line
    that does mention the target.
    """
    spellings = _case_variants(token)
    gone = []
    after_lines = after.split("\n")
    for line in before.split("\n"):
        if not line.strip():
            continue
        if any(s in line for s in spellings):
            continue
        if line not in after_lines:
            gone.append(line.strip())
    return gone


#: An import-like statement in any of the fifteen languages. Used to check that a
#: neighbour's BINDING survived, not merely its name.
_IMPORT_LINE = re.compile(
    r'^[ \t]*(?:#include|import|from|use|using|require|include|open|package|const|let|var)\b',
    re.IGNORECASE)


def _binds(code: str, token: str) -> bool:
    """Does some import-like line in `code` still name `token`?"""
    return any(_IMPORT_LINE.match(line) and _present(line, token)
               for line in code.split("\n"))


def _present(code: str, token: str) -> bool:
    """Is `token` present as a whole identifier?"""
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
                          code))


def probe(lang: str, op_change_type: str, which: str, **kw):
    """Run one probe, or return None when the fixture does not exist for this language.

    A missing fixture is a genuine inapplicability (Go imports packages, not types),
    NOT a pass. main() prints every skip so an absent fixture cannot quietly shrink
    the gate.
    """
    case = CASES.get(lang)
    if case is None:
        # A language in TARGETS with no fixture entry at all. Returning None routes it
        # to the "ran NO probes" guard, which prints WHY -- a KeyError traceback also
        # exits non-zero but tells the reader nothing about the missing coverage.
        return None
    src = case.get(which)
    if src is None:
        return None
    # The TARGET of this probe, which is not always the field: remove_type aims at a
    # type name. The first version used case["token"] unconditionally, so for
    # remove_type every deleted line that mentioned `User` but not `phoneNumber` was
    # counted as collateral -- flagging correct behaviour as damage. A measurement
    # tool that misreports is the same defect class it is looking for.
    target = kw.pop("field_name", case["token"])
    neighbour = kw.pop("neighbour", None)
    fixed, explanation = apply_fix_template(
        code=src, language=lang, change_type=op_change_type,
        field_name=target, **kw)
    changed = fixed != src
    # A neighbour whose BINDING was taken by a removal aimed at something else.
    #
    # The first version of this checked only whether the identifier still appeared
    # anywhere in the file -- and that was too weak to catch the very bug it was
    # written for. Reverting the typescript delegation reproduces the destructive
    # import exactly:
    #
    #   -import { User, Address } from './models';
    #    export function label(a: Address): string { return a.city; }
    #
    # `Address` is still "present", in the signature, so the check passed while the
    # file no longer compiles. What the bug destroys is the BINDING, so that is what
    # has to be measured. Third detector defect found in this file, same shape each
    # time: a measurement that agrees with correct behaviour AND with the bug.
    neighbour_lost = bool(
        neighbour and _binds(src, neighbour) and not _binds(fixed, neighbour))
    return {
        "target": target,
        "neighbour": neighbour,
        "neighbour_lost": neighbour_lost,
        "changed": changed,
        "survives": _present(fixed, target),
        "claims_success": _claims_success(explanation),
        "collateral": _collateral_lines(src, fixed, target),
        "explanation": explanation.split("\n")[0],
        "before": src,
        "after": fixed,
    }


#: (label, change_type, fixture key, extra kwargs, is_a_removal)
#:
#: `imports` and `enum` were added after this tool failed to see two real defects:
#: the destructive multi-name import (invisible to the collateral check, because the
#: import line mentions the target) and the fabricated shape list on
#: remove_enum_value, which asserted "switch/case arms, match arms and constant
#: declarations" on an untouched file.
PROBES = (
    ("remove_field   (usages)", "removed_field", "usage", {}, True),
    ("remove_field   (decl)", "removed_field", "decl", {}, True),
    ("remove_type    (usages)", "type_removed", "usage", {"field_name": "User"}, True),
    ("remove_type    (imports)", "type_removed", "imports",
     {"field_name": "User", "neighbour": True}, True),
    ("remove_enum    (enum)", "removed_enum_value", "enum",
     {"field_name": "PENDING"}, True),
    ("rename_field   (usages)", "field_renamed", "usage", {"new_name": "phone"}, True),
    ("change_type    (decl)", "field_type_changed", "decl_typed",
     {"old_type": "integer", "new_type": "string"}, False),
)


def main() -> int:
    print("=" * 78)
    print(f"CODEMOD HONESTY AUDIT -- {len(TARGETS)} languages, "
          f"{len(PROBES)} probes each")
    print("=" * 78)
    print()

    lies, collateral_hits, neighbour_hits, rows, skips = [], [], [], [], []

    for lang in TARGETS:
        for label, ct, which, extra, removal in PROBES:
            extra = dict(extra)
            if extra.pop("neighbour", False):
                nb = CASES.get(lang, {}).get("neighbour")
                if nb:
                    extra["neighbour"] = nb
            r = probe(lang, ct, which, **extra)
            if r is None:
                skips.append((lang, label, which))
                continue
            # A removal that claims success while the token survives is a LIE.
            # A retype is not a removal, so token survival is expected there; the
            # lie test for it is claiming success having changed nothing.
            if removal:
                lie = r["claims_success"] and (r["survives"] or not r["changed"])
            else:
                lie = r["claims_success"] and not r["changed"]
            if lie:
                lies.append((lang, label, r))
            if r["collateral"]:
                collateral_hits.append((lang, label, r))
            if r["neighbour_lost"]:
                neighbour_hits.append((lang, label, r))
            rows.append((lang, label, r, lie))

    for lang in TARGETS:
        print(f"  {lang}")
        for l, label, r, lie in rows:
            if l != lang:
                continue
            flag = "LIE " if lie else ("    " if r["claims_success"] else "ok  ")
            extra = ""
            if r["collateral"]:
                extra += f" COLLATERAL={len(r['collateral'])}"
            if r["neighbour_lost"]:
                extra += f" LOST={r['neighbour']}"
            print(f"      {flag}{label:24s} changed={r['changed']!s:5s} "
                  f"survives={r['survives']!s:5s} "
                  f"claims_ok={r['claims_success']!s:5s}{extra}")
        for l, label, which in skips:
            if l == lang:
                print(f"      --  {label:24s} SKIPPED, no `{which}` fixture "
                      f"(shape does not exist in {lang})")
        print()

    print("-" * 78)
    print(f"  probes run: {len(rows)}   skipped: {len(skips)}")
    print(f"  LIES: {len(lies)}   COLLATERAL DAMAGE: {len(collateral_hits)}   "
          f"NEIGHBOUR LOSS: {len(neighbour_hits)}")
    print("-" * 78)
    if not rows:
        # An empty probe set would make every conclusion below a vacuous truth. This
        # repo has shipped that defect three times; refuse it here.
        print("  ✗ no probes ran -- refusing to report a pass over an empty set")
        return 1
    # Every language must contribute. A fixture dict that lost a whole language would
    # otherwise shrink the gate silently, which is the same vacuous-pass shape.
    silent = [lang for lang in TARGETS if not any(l == lang for l, _, _, _ in rows)]
    if silent:
        print(f"  ✗ these languages ran NO probes at all: {silent}")
        print("    A language with no fixtures is not a language with no defects.")
        return 1
    if lies:
        print("\n  Each lie, with the claim, the truth, and the actual diff:")
        for lang, label, r in lies:
            print(f"\n      {lang} / {label.split()[0]}  (target: {r['target']})")
            print(f"          claim : {r['explanation'][:100]}")
            print(f"          truth : changed={r['changed']}, target still present="
                  f"{r['survives']}")
            _print_diff(r["before"], r["after"], indent=10)
    if collateral_hits:
        print("\n  Collateral damage -- lines deleted that never mentioned the target:")
        for lang, label, r in collateral_hits:
            print(f"\n      {lang} / {label.split()[0]}  (target: {r['target']})")
            for line in r["collateral"][:4]:
                print(f"          DELETED: {line}")
            _print_diff(r["before"], r["after"], indent=10)
    if neighbour_hits:
        print("\n  Neighbour loss -- an identifier that was NOT the target vanished:")
        for lang, label, r in neighbour_hits:
            print(f"\n      {lang} / {label.split()[0]}  (target: {r['target']}, "
                  f"lost: {r['neighbour']})")
            print(f"          claim : {r['explanation'][:100]}")
            _print_diff(r["before"], r["after"], indent=10)

    if lies or collateral_hits or neighbour_hits:
        print("\n  FAILING. A codemod that corrupts a file while reporting success is")
        print("  strictly worse than one that refuses: the pipeline believes a fix")
        print("  exists, and for the languages with no wired validator nothing")
        print("  downstream will catch it.")
        return 1
    print(f"\n  ✅ {len(rows)} probes across {len(TARGETS)} languages: every codemod "
          f"either does\n     the work or says it could not, and no probe lost a "
          f"neighbouring identifier")
    return 0


def _print_diff(before: str, after: str, indent: int = 0) -> None:
    import difflib
    pad = " " * indent
    shown = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     lineterm="", n=0):
        if line.startswith(("---", "+++")):
            continue
        print(f"{pad}{line}")
        shown += 1
        if shown >= 12:
            print(f"{pad}...")
            break
    if shown == 0:
        print(f"{pad}(no textual difference at all)")


if __name__ == "__main__":
    sys.exit(main())
