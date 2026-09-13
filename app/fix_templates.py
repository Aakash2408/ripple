"""
Deterministic fix template engine for Ripple.
Handles field_removed, field_renamed, type_changed across 8+ languages WITHOUT any LLM.
"""
from __future__ import annotations

import collections
import re
from typing import Callable


# --- Case conversion utilities ---

def to_snake(name: str) -> str:
    s1 = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    return re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s1).lower()


def to_camel(name: str) -> str:
    parts = to_snake(name).split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def to_pascal(name: str) -> str:
    return ''.join(p.capitalize() for p in to_snake(name).split('_'))


def to_upper_snake(name: str) -> str:
    return to_snake(name).upper()


def name_variants(name: str) -> dict[str, str]:
    """Return all case variants of a field name.

    `literal` is the name EXACTLY as the contract declares it, which is not
    reconstructible from the others: an OpenAPI enum value may be written `pending`,
    and pascal/upper-snake would miss it. `_declared_symbol_names` needs it.
    """
    return {
        'literal': name,
        'snake': to_snake(name),
        'camel': to_camel(name),
        'pascal': to_pascal(name),
        'upper_snake': to_upper_snake(name),
    }


# --- Shared helpers ---

def _remove_lines_matching(code: str, pattern: re.Pattern) -> str:
    """Remove entire lines that match the pattern."""
    return '\n'.join(
        line for line in code.split('\n')
        if not pattern.search(line)
    )


_H = r'[^\S\n]'   # horizontal whitespace ONLY -- see _drop_list_element


def _drop_list_element(code: str, element: str) -> str:
    """Remove one element of a comma-separated list, taking its separator with it.

    `element` is a regex matching the element WITHOUT surrounding separators.

    Two rules, both learned from a real parse failure:

    1. WHICH COMMA IS THE ARTEFACT DEPENDS ON POSITION. Removing the element and
       leaving both commas alone leaves the list malformed:

           :id, :email, :phone_number      last    -> comma BEFORE is orphaned
           :id, :phone_number, :email      middle  -> either one works
           :phone_number, :id, :email      first   -> comma AFTER is orphaned

       So take the preceding comma when there is one, otherwise the following
       one. `_clean_trailing_commas` cannot rescue this: it only repairs `,,`
       and a comma against a bracket, and `:email, end` is neither.

    2. HORIZONTAL WHITESPACE ONLY. `\\s*` matches newlines, so a trailing `\\s*`
       after the last element of a line consumes the line break and welds the
       next line on. Measured with real ruby 2.0 -- `attr_accessor :id, :email,`
       swallowed the blank line AND the following `def`, producing
       `attr_accessor :id, :email, def initialize(id:, email:)` and
       `syntax error, unexpected ','`. Ruby is the worst case because a trailing
       comma there is a line continuation rather than a hard error, so the
       damage lands two lines away from the edit.

       Same reasoning as `_remove_empty_blocks` and `_clean_trailing_commas`,
       both of which already use `[^\\S\\n]` for exactly this. The lesson was
       written down one function away and not applied here.
    """
    code = re.sub(rf',{_H}*{element}', '', code)          # not first -> take the comma before
    code = re.sub(rf'{element}{_H}*,{_H}*', '', code)     # first     -> take the comma after
    return code


def _clean_trailing_commas(code: str) -> str:
    """Remove only genuinely INVALID comma artifacts.

    A trailing comma before a closing bracket on the next line is NOT an
    artifact -- it is valid in Python, JS/TS, and Rust, and is REQUIRED in
    Go composite literals:

        req := &pb.Request{
            Name:  name,
            Email: email,     <-- removing this comma breaks compilation
        }

    Stripping it produced PRs that did not build. Only collapse sequences
    that are invalid in every supported language: doubled commas left
    behind by a removed middle element, and a comma directly after an
    opening bracket from a removed first element.
    """
    # ",," or ", ,"  ->  ","   (removed a middle element)
    code = re.sub(r',(\s*),', r',\1', code)
    # "(," / "[," / "{,"  ->  "(" / "[" / "{"   (removed the first element)
    code = re.sub(r'([(\[{])\s*,\s*', r'\1', code)
    return code


def _remove_empty_blocks(code: str) -> str:
    """Collapse argument lists that became empty.

    Uses [^\\S\\n]* (horizontal whitespace only) rather than \\s* so a
    legitimate multi-line trailing comma before ')' is preserved -- \\s
    matches newlines and would strip the comma Go requires.
    """
    # "( ,"  ->  "("   (removed the first argument)
    code = re.sub(r'\([^\S\n]*,[^\S\n]*', '(', code)
    # "a, )" on ONE line  ->  "a)"   (dangling comma, same line only)
    code = re.sub(r',[^\S\n]*\)', ')', code)
    return code


def _clean_blank_lines(code: str) -> str:
    """Collapse 3+ consecutive blank lines to 2.

    That is what this always claimed to do. `\\n{3,}` -> `\\n\\n` collapsed to ONE
    blank line, because `\\n\\n` is a single blank line, not two -- the docstring and
    the regex disagreed and the regex won.

    It mattered most in Python, where PEP 8 requires TWO blank lines between
    top-level definitions. Every Python fix silently reformatted every gap in the
    file from two blank lines to one, so a one-field removal arrived as a diff
    touching every function boundary -- destroying the minimal-diff property the
    fixtures assert, in a language that had no fixture to notice until the validator
    was wired. TypeScript was unaffected only because one blank line between members
    is conventional there.

    `\\n{4,}` -> `\\n\\n\\n` is the documented behaviour: three or more blank lines
    become two, and two stay two.
    """
    return re.sub(r'\n{4,}', '\n\n\n', code)


def _postprocess(code: str) -> str:
    code = _clean_trailing_commas(code)
    code = _remove_empty_blocks(code)
    code = _clean_blank_lines(code)
    return code


# --- FIELD REMOVED templates per language ---

def _remove_field_go(code: str, variants: dict[str, str]) -> str:
    pascal = variants['pascal']
    camel = variants['camel']
    snake = variants['snake']
    names = {pascal, camel, snake}
    # Remove struct field declaration: FieldName Type `json:"..."`
    code = re.sub(rf'^\s*{pascal}\s+\S+.*$\n?', '', code, flags=re.MULTILINE)
    # Remove struct literal assignment: FieldName: value,
    code = re.sub(rf'^\s*{pascal}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove .FieldName access lines (entire statement if standalone)
    for n in names:
        code = re.sub(rf'^\s*\S*\.{n}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove function params containing the field name
    for n in names:
        code = re.sub(rf'\b{n}\s+\w+\s*,?\s*', '', code)
    return code


def _run_codemod(code: str, field: str, language: str, codemod) -> str:
    """Run a syntax-aware codemod, enforce the diff contract, record the outcome.

    Extracted when Python got a real codemod. It was inline in
    `_remove_field_typescript`, and copying it would have put two copies of the
    diff-contract enforcement in the tree -- including two copies of the decision to
    return the ORIGINAL code on violation, which is the part that must never drift.
    """
    from .diff_contract import check as _diff_check
    result = codemod(code, field)

    # THE DIFF CONTRACT, IN THE REQUEST PATH.
    #
    # On violation the ORIGINAL code is returned, not the partial result. A patch
    # that changed something it should not have is worse than no patch, and returning
    # unchanged code is already what apply_fix_template turns into a truthful "could
    # not remove" and what the outcome derivation turns into BLOCKED.
    refusals = list(result.refusals)
    out = result.code
    diff_violations = []
    if out != code:
        verdict = _diff_check(code, out, field, language)
        if not verdict.ok:
            diff_violations = verdict.violations
            refusals = refusals + [
                f"diff contract: {v}" for v in verdict.violations[:3]]
            out = code                      # refuse the patch entirely

    _LAST_CODEMOD_RESULT.clear()
    _LAST_CODEMOD_RESULT.update(
        language=language,
        refusals=refusals, notes=result.notes,
        edits=[] if diff_violations else [e["shape"] for e in result.edits],
        diff_violations=diff_violations)
    return out


def _remove_field_typescript(code: str, variants: dict[str, str]) -> str:
    """Delegates to app/ts_codemod.py, which is syntax-aware and REFUSES shapes it
    cannot remove safely.

    The regex version measured against the golden fixture produced
    `phone: user.};` -- unparseable -- because `\\b{field}\\s*,\\s*` stripped
    `phoneNumber,` as the tail of a member expression, and it left a
    template-literal interpolation untouched because its access pattern only
    matched a line whose first token was the access.

    Returns the code unchanged when the codemod refuses, which apply_fix_template
    turns into a truthful "could not remove" explanation and Stage 3's outcome
    derivation turns into BLOCKED with a reason.
    """
    from .ts_codemod import remove_field as _codemod
    return _run_codemod(code, variants['camel'], "typescript", _codemod)


def _remove_field_python(code: str, variants: dict[str, str]) -> str:
    """Delegates to app/py_codemod.py, which is syntax-aware and REFUSES shapes it
    cannot remove safely.

    WHAT THIS REPLACED, AND WHY IT WAS WORSE THAN NOTHING
    Five context-free regexes, measured on a realistic consumer as a complete no-op
    that reported "2 lines affected" -- every reference survived and one blank line
    was collapsed, so `changed` was True and the pipeline believed a fix existed.

    Two of the five were also actively unsafe rather than merely useless:

        r'\\b{f}\\s*:\\s*[^,=)]+(\\s*=[^,)]+)?\\s*,?\\s*'   stripped a FUNCTION
                                                          PARAMETER -- the shape
                                                          ts_codemod explicitly
                                                          refuses, because it breaks
                                                          every caller
        r'^\\s*.*\\[\\s*["\\']?{f}["\\']?\\s*\\].*$\\n?'      deleted the WHOLE LINE
                                                          containing a subscript,
                                                          taking the assignment and
                                                          any call on it with it

    Neither had a test that would have noticed, because the fixture library had no
    Python cell until the validator was wired.
    """
    from .py_codemod import remove_field as _codemod
    return _run_codemod(code, variants['snake'], "python", _codemod)


def _remove_field_java(code: str, variants: dict[str, str]) -> str:
    camel = variants['camel']
    pascal = variants['pascal']
    # Remove field declaration: private/protected/public Type fieldName;
    code = re.sub(rf'^\s*(private|protected|public)\s+\S+\s+{camel}\s*[;=].*$\n?', '', code, flags=re.MULTILINE)
    # Remove getter: public Type getFieldName() { ... }
    code = re.sub(rf'^\s*(public|protected)\s+\S+\s+get{pascal}\s*\(.*?\)\s*\{{[^}}]*\}}\s*$\n?', '', code, flags=re.MULTILINE | re.DOTALL)
    # Remove single-line getter
    code = re.sub(rf'^\s*(public|protected)\s+\S+\s+get{pascal}\s*\(.*$\n?', '', code, flags=re.MULTILINE)
    # Remove setter
    code = re.sub(rf'^\s*(public|protected)\s+void\s+set{pascal}\s*\(.*?\)\s*\{{[^}}]*\}}\s*$\n?', '', code, flags=re.MULTILINE | re.DOTALL)
    code = re.sub(rf'^\s*(public|protected)\s+void\s+set{pascal}\s*\(.*$\n?', '', code, flags=re.MULTILINE)
    # Remove this.field
    code = re.sub(rf'^\s*this\.{camel}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from constructor/method params
    code = re.sub(rf'\b\w+\s+{camel}\s*,?\s*', '', code)
    return code


def _remove_field_rust(code: str, variants: dict[str, str]) -> str:
    snake = variants['snake']
    # Remove struct field: pub field_name: Type,
    code = re.sub(rf'^\s*(pub\s+)?{snake}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove .field_name access (entire line)
    code = re.sub(rf'^\s*\S*\.{snake}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from function params
    code = re.sub(rf'\b{snake}\s*:\s*[^,)]+,?\s*', '', code)
    # Remove struct literal init: field_name: value,
    code = re.sub(rf'^\s*{snake}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    return code


def _remove_field_ruby(code: str, variants: dict[str, str]) -> str:
    """Remove a field from Ruby source.

    Ruby is the one language here whose field declarations are a comma-separated
    list ON ONE LINE (`attr_accessor :id, :email, :phone_number`), so it is the
    only one where the `$`-anchored whole-line patterns cannot fire first and the
    list-element path is load-bearing. See `_drop_list_element` for why the
    separator and the newline both matter -- the previous version produced output
    that real ruby refused to parse.
    """
    snake = re.escape(variants['snake'])
    # Sole attribute on the line: drop the whole declaration.
    code = re.sub(rf'^{_H}*attr_(accessor|reader|writer){_H}+:{snake}\b{_H}*$\n?',
                  '', code, flags=re.MULTILINE)
    # One element of a multi-attribute list. `\b` so a field named `phone` does
    # not match inside `:phone_number`.
    code = _drop_list_element(code, rf':{snake}\b')
    # Remove @field_name (entire line).
    code = re.sub(rf'^{_H}*@{snake}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove hash key on its own line: field_name: value,
    code = re.sub(rf'^{_H}*{snake}:{_H}*.*$\n?', '', code, flags=re.MULTILINE)
    # Keyword argument in a signature or call: `phone_number: value`. Same list
    # rule; `[^,)\n]*` rather than `[^,)]*` so the value cannot span lines.
    code = _drop_list_element(code, rf'\b{snake}:{_H}*[^,)\n]*')
    # Sole keyword argument: `def build(phone_number:)` -> `def build()`. Bounded
    # by the parens, so this cannot reach past the argument list.
    code = re.sub(rf'\({_H}*{snake}:{_H}*[^,)\n]*\)', '()', code)
    return code


def _remove_field_kotlin(code: str, variants: dict[str, str]) -> str:
    camel = variants['camel']
    # Remove val/var from data class: val fieldName: Type,
    code = re.sub(rf'^\s*(val|var)\s+{camel}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove .fieldName access (entire line)
    code = re.sub(rf'^\s*\S*\.{camel}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from function params: fieldName: Type
    code = re.sub(rf'\b{camel}\s*:\s*[^,)]+,?\s*', '', code)
    return code


def _remove_field_csharp(code: str, variants: dict[str, str]) -> str:
    pascal = variants['pascal']
    camel = variants['camel']
    # Remove property: public Type FieldName { get; set; }
    code = re.sub(rf'^\s*(public|private|protected|internal)\s+\S+\s+{pascal}\s*\{{.*\}}\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove auto-property single line
    code = re.sub(rf'^\s*(public|private|protected|internal)\s+\S+\s+{pascal}\s*;.*$\n?', '', code, flags=re.MULTILINE)
    # Remove .FieldName access
    code = re.sub(rf'^\s*\S*\.{pascal}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from constructor params
    code = re.sub(rf'\b\w+\s+{camel}\s*,?\s*', '', code)
    # Remove this.field = param
    code = re.sub(rf'^\s*(this\.)?{pascal}\s*=.*$\n?', '', code, flags=re.MULTILINE)
    return code


#: Reasons from the most recent TypeScript codemod run. Not elegant -- the handler
#: contract is (code, variants) -> str, so there is no return channel for them --
#: but losing them is worse: a PR that removes two references and silently declines
#: a third tells the reviewer nothing about the third.
#: Last codemod outcome, for the explanation. Was _LAST_TS_RESULT until
#: Python got a real codemod -- a TypeScript-specific name gating a
#: language-agnostic mechanism is how the Python refusals would have been
#: silently dropped from the PR body.
_LAST_CODEMOD_RESULT: dict = {}


REMOVE_HANDLERS: dict[str, Callable[[str, dict[str, str]], str]] = {
    'go': _remove_field_go,
    'typescript': _remove_field_typescript,
    'javascript': _remove_field_typescript,
    'python': _remove_field_python,
    'java': _remove_field_java,
    'rust': _remove_field_rust,
    'ruby': _remove_field_ruby,
    'kotlin': _remove_field_kotlin,
    'csharp': _remove_field_csharp,
    'c#': _remove_field_csharp,
}


# --- FIELD RENAMED templates ---

def _rename_field(code: str, old_variants: dict[str, str], new_variants: dict[str, str]) -> str:
    """Rename all occurrences, respecting case style. Skips strings and comments."""
    for style in ('snake', 'camel', 'pascal', 'upper_snake'):
        old = old_variants[style]
        new = new_variants[style]
        if old == new:
            continue
        # Negative lookbehind/lookahead to skip inside string literals and comments
        # Skip if preceded by quote or followed by quote (basic heuristic)
        pattern = rf'(?<!["\'/])\b{re.escape(old)}\b(?!["\'/])'
        code = re.sub(pattern, new, code)
    return code


# --- TYPE CHANGED templates per language ---

def _change_type_go(code: str, old_type: str, new_type: str) -> str:
    # struct field types, function params, variable declarations
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_typescript(code: str, old_type: str, new_type: str) -> str:
    # property types, param types, generics
    code = re.sub(rf':\s*{re.escape(old_type)}\b', f': {new_type}', code)
    code = re.sub(rf'<{re.escape(old_type)}>', f'<{new_type}>', code)
    code = re.sub(rf'\b{re.escape(old_type)}\b(?=\s*[|&\]])', new_type, code)
    return code


def _change_type_python(code: str, old_type: str, new_type: str) -> str:
    # type hints: -> OldType, : OldType, isinstance(..., OldType)
    code = re.sub(rf':\s*{re.escape(old_type)}\b', f': {new_type}', code)
    code = re.sub(rf'->\s*{re.escape(old_type)}\b', f'-> {new_type}', code)
    code = re.sub(rf'isinstance\(([^,]+),\s*{re.escape(old_type)}\)', rf'isinstance(\1, {new_type})', code)
    code = re.sub(rf'\b{re.escape(old_type)}\b(?=\s*\[)', new_type, code)
    return code


def _change_type_java(code: str, old_type: str, new_type: str) -> str:
    # field types, return types, param types, generics
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_rust(code: str, old_type: str, new_type: str) -> str:
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_kotlin(code: str, old_type: str, new_type: str) -> str:
    code = re.sub(rf':\s*{re.escape(old_type)}\b', f': {new_type}', code)
    code = re.sub(rf'\b{re.escape(old_type)}\b(?=\s*[<>)])', new_type, code)
    return code


def _change_type_csharp(code: str, old_type: str, new_type: str) -> str:
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_ruby(code: str, old_type: str, new_type: str) -> str:
    # Ruby is dynamically typed; replace in yard docs and sig blocks
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


TYPE_CHANGE_HANDLERS: dict[str, Callable[[str, str, str], str]] = {
    'go': _change_type_go,
    'typescript': _change_type_typescript,
    'javascript': _change_type_typescript,
    'python': _change_type_python,
    'java': _change_type_java,
    'rust': _change_type_rust,
    'ruby': _change_type_ruby,
    'kotlin': _change_type_kotlin,
    'csharp': _change_type_csharp,
    'c#': _change_type_csharp,
}


# ---------------------------------------------------------------------------
# CONTRACT type name -> LANGUAGE type name
#
# WHY THIS EXISTS
# The handlers above do a word-boundary replacement of whatever type strings
# they are given -- but diff engines emit the CONTRACT's vocabulary, not the
# consumer language's. proto says `int32`, JSON Schema says `integer`, Thrift
# says `i64`. No Java source contains the token `string`; it contains `String`.
#
# Without translation the operation was wrong in all nine languages, in three
# different ways, and none of them reported a problem:
#
#   java, kotlin, rust, python, ruby, javascript  silent no-op -- 0 replacements,
#       code unchanged, so no PR opens and a detected break produced silence.
#   typescript, csharp  WROTE THE CONTRACT NAME INTO SOURCE, producing
#       `phoneNumber: int32;` and `public int32 PhoneNumber` -- neither
#       compiles -- while reporting "1 type annotations updated".
#   go  correct BY COINCIDENCE: proto's int32 happens to be spelled int32 in Go.
#
# Writing an unmapped name into source is the worst of the three, because a
# reviewer sees a confident diff that cannot build. So an unmapped type now
# annotates instead of editing -- see the change_field_type branch.
#
# Keys are lowercased contract types drawn from the engines actually in the
# repo: proto, JSON Schema / OpenAPI, Avro, Thrift, GraphQL and SQL.
CONTRACT_TYPE_TO_NATIVE: dict[str, dict[str, str]] = {
    'string': {
        'go': 'string', 'typescript': 'string', 'python': 'str',
        'java': 'String', 'rust': 'String', 'kotlin': 'String',
        'csharp': 'string', 'swift': 'String', 'php': 'string',
        'scala': 'String', 'dart': 'String',
    },
    'int32': {
        'go': 'int32', 'typescript': 'number', 'python': 'int',
        'java': 'int', 'rust': 'i32', 'kotlin': 'Int',
        'csharp': 'int', 'swift': 'Int32', 'php': 'int',
        'scala': 'Int', 'dart': 'int',
    },
    'int64': {
        'go': 'int64', 'typescript': 'number', 'python': 'int',
        'java': 'long', 'rust': 'i64', 'kotlin': 'Long',
        'csharp': 'long', 'swift': 'Int64', 'php': 'int',
        'scala': 'Long', 'dart': 'int',
    },
    'bool': {
        'go': 'bool', 'typescript': 'boolean', 'python': 'bool',
        'java': 'boolean', 'rust': 'bool', 'kotlin': 'Boolean',
        'csharp': 'bool', 'swift': 'Bool', 'php': 'bool',
        'scala': 'Boolean', 'dart': 'bool',
    },
    'float': {
        'go': 'float32', 'typescript': 'number', 'python': 'float',
        'java': 'float', 'rust': 'f32', 'kotlin': 'Float',
        'csharp': 'float', 'swift': 'Float', 'php': 'float',
        'scala': 'Float', 'dart': 'double',
    },
    'double': {
        'go': 'float64', 'typescript': 'number', 'python': 'float',
        'java': 'double', 'rust': 'f64', 'kotlin': 'Double',
        'csharp': 'double', 'swift': 'Double', 'php': 'float',
        'scala': 'Double', 'dart': 'double',
    },
    'bytes': {
        'go': '[]byte', 'typescript': 'Uint8Array', 'python': 'bytes',
        'java': 'byte[]', 'rust': 'Vec<u8>', 'kotlin': 'ByteArray',
        'csharp': 'byte[]', 'swift': 'Data', 'php': 'string',
        'scala': 'Array[Byte]', 'dart': 'List<int>',
    },
    'timestamp': {
        'go': 'time.Time', 'typescript': 'Date', 'python': 'datetime',
        'java': 'Instant', 'rust': 'DateTime', 'kotlin': 'Instant',
        'csharp': 'DateTime', 'swift': 'Date', 'php': 'DateTime',
        'scala': 'Instant', 'dart': 'DateTime',
    },
}

# Dialect spellings that mean the same contract type. Engines disagree:
# JSON Schema says `integer`/`number`/`boolean`, Thrift says `i32`/`i64`/
# `binary`, Avro says `int`/`long`, GraphQL says `Int`/`Float`/`Boolean`, SQL
# says `VARCHAR`/`BIGINT`. Normalising here keeps one table instead of five.
_CONTRACT_TYPE_ALIASES = {
    'integer': 'int32', 'int': 'int32', 'i32': 'int32', 'int16': 'int32',
    'i16': 'int32', 'short': 'int32', 'uint32': 'int32', 'sint32': 'int32',
    'fixed32': 'int32', 'sfixed32': 'int32', 'smallint': 'int32',
    'long': 'int64', 'i64': 'int64', 'bigint': 'int64', 'uint64': 'int64',
    'sint64': 'int64', 'fixed64': 'int64', 'sfixed64': 'int64',
    'boolean': 'bool',
    'number': 'double', 'decimal': 'double', 'numeric': 'double',
    'real': 'float', 'float32': 'float', 'float64': 'double',
    'binary': 'bytes', 'blob': 'bytes', 'bytea': 'bytes',
    'varchar': 'string', 'text': 'string', 'char': 'string', 'uuid': 'string',
    'id': 'string', 'nvarchar': 'string',
    'datetime': 'timestamp', 'date': 'timestamp', 'time': 'timestamp',
    'instant': 'timestamp', 'timestamptz': 'timestamp',
}

# Languages with no type annotations to rewrite. A contract type change is real
# for them -- the VALUE shape changes at runtime -- but there is no declaration
# to edit, so editing would be theatre. These annotate instead.
UNTYPED_LANGUAGES = frozenset({'javascript', 'ruby', 'yaml', 'shell'})


def native_type(language: str, contract_type: str) -> str:
    """The language's spelling of a contract type, or "" if unknown.

    Returns "" rather than falling back to the contract name: writing `int32`
    into TypeScript or C# produced source that does not compile.
    """
    key = (contract_type or "").strip().lower().rstrip('?').strip()
    key = _CONTRACT_TYPE_ALIASES.get(key, key)
    return CONTRACT_TYPE_TO_NATIVE.get(key, {}).get(language.lower().strip(), "")


# ---------------------------------------------------------------------------
# remove_type / remove_enum_value / rename_type
#
# Driven by per-language pattern tables rather than 16 hand-written functions
# (2 operations x 8 languages). Same coverage, one place to audit.
#
# {name} is substituted with each case variant of the symbol. Patterns are
# applied line-wise with re.MULTILINE, so each removes a whole statement.
# ---------------------------------------------------------------------------

# References to a REMOVED TYPE: imports, declarations, annotations,
# constructions. Removal leaves the surrounding code thinner but may not make
# it compile -- anything left is surfaced by find_residual_references and the
# PR is marked partial, exactly as with removed fields.
_TYPE_REF_PATTERNS = {
    'go': [
        r'^\s*(?:var|const)\s+\w+\s+\*?{name}\b.*$',      # var u User
        r'^\s*\w+\s*:?=\s*&?{name}\s*\{{.*$',              # u := User{
        r'^\s*\w+\s+\*?{name}\s*$',                        # struct field of that type
        r'^\s*.*\b{name}\s*\{{\s*\}}.*$',                  # User{}
    ],
    'typescript': [
        r'^\s*import\s+.*\b{name}\b.*$',
        r'^\s*(?:let|const|var)\s+\w+\s*:\s*{name}\b.*$',
        r'^\s*\w+\s*:\s*{name}\b.*$',                      # interface prop / param
        r'^\s*.*\bnew\s+{name}\s*\(.*$',
    ],
    'python': [
        r'^\s*from\s+\S+\s+import\s+.*\b{name}\b.*$',
        r'^\s*import\s+.*\b{name}\b.*$',
        r'^\s*\w+\s*:\s*{name}\b.*$',                      # annotation
        r'^\s*\w+\s*=\s*{name}\s*\(.*$',                   # construction
    ],
    'java': [
        r'^\s*import\s+.*\b{name}\s*;.*$',
        r'^\s*(?:private|public|protected)?\s*{name}\s+\w+\s*;.*$',
        r'^\s*{name}\s+\w+\s*=\s*new\s+{name}\s*\(.*$',
    ],
    'rust': [
        # `[^{}\n]*` and the anchored tail restrict this to a SOLE import:
        # `use models::User;` matches, `use models::{User, Address};` does not.
        #
        # The original was r'^\s*use\s+.*\b{name}\b.*$', which deleted the whole
        # statement when the name appeared anywhere on it -- the fifth instance of the
        # destructive-import defect, after python, typescript, javascript and the
        # generic fallback. Measured: `use models::{User, Address};` vanished, so
        # `Address` was left used but unimported, and it reported "Removed references
        # to deleted type 'User' (1 lines affected)". Rust has NO wired validator, so
        # nothing downstream would have caught it.
        #
        # `use models::User as U;` also no longer matches, which is correct: the local
        # name is `U` and dropping the import would orphan every use of it.
        r'^\s*use\s+[^{}\n]*\b{name}\b\s*;?\s*$',
        r'^\s*let\s+\w+\s*:\s*{name}\b.*$',
        r'^\s*\w+\s*:\s*{name}\s*,?\s*$',
        r'^\s*let\s+\w+\s*=\s*{name}\s*\{{.*$',
    ],
    'ruby': [
        r'^\s*require\s+.*{name}.*$',
        r'^\s*\w+\s*=\s*{name}\.new\b.*$',
    ],
    'kotlin': [
        r'^\s*import\s+.*\b{name}\b.*$',
        r'^\s*(?:val|var)\s+\w+\s*:\s*{name}\b.*$',
        r'^\s*\w+\s*:\s*{name}\s*,?\s*$',
    ],
    'csharp': [
        r'^\s*using\s+.*\b{name}\b.*$',
        r'^\s*(?:public|private|protected)?\s*{name}\s+\w+\s*(?:\{{\s*get.*)?$',
        r'^\s*var\s+\w+\s*=\s*new\s+{name}\s*\(.*$',
    ],
}

#: Languages the `remove_type` dispatch actually reaches, and how.
#:
#: This exists because `_TYPE_REF_PATTERNS.keys()` STOPPED being the answer. Three
#: languages now delegate to syntax-aware codemods instead of the pattern table, and
#: one of them -- javascript -- has no pattern entry at all, so reading the table
#: understated it: `generate_fix("javascript", "remove_type")` returned False while a
#: real codemod was wired. The capability registry is only worth having if it tracks
#: the dispatch, so it reads this instead.
#:
#: DERIVED from `_TYPE_REF_PATTERNS` rather than re-listing those languages, so there
#: is one copy of that set. Only the codemod delegations are named here, and they are
#: named in exactly one other place -- the `if lang in (...)` in
#: `_remove_type_reference` -- which `test_capability_tables_match_the_dispatch`
#: compares against.
TYPE_REMOVAL_HANDLERS = dict(
    {lang: "_TYPE_REF_PATTERNS" for lang in _TYPE_REF_PATTERNS},
    **{
        "python": "py_codemod.remove_type",
        "typescript": "ts_codemod.remove_type",
        "javascript": "ts_codemod.remove_type",
    },
)


# References to a REMOVED ENUM VALUE: switch/case arms, match arms, and
# qualified constant references.
_ENUM_VALUE_PATTERNS = {
    'go': [
        r'^\s*case\s+.*\b{name}\b.*:.*$',
        r'^\s*.*\b\w+_{name}\b.*$',
    ],
    'typescript': [
        r'^\s*case\s+.*\b{name}\b.*:.*$',
        r'^\s*{name}\s*=.*,?\s*$',                         # enum member decl
        r'^\s*.*\b\w+\.{name}\b.*$',
    ],
    'python': [
        r'^\s*{name}\s*=.*$',                              # Enum member
        r'^\s*(?:elif|if)\s+.*\b{name}\b.*:\s*$',
        r'^\s*case\s+.*\b{name}\b.*:\s*$',                 # match/case
    ],
    'java': [
        r'^\s*case\s+{name}\s*:.*$',
        r'^\s*{name}\s*,?\s*$',                            # enum constant
    ],
    'rust': [
        r'^\s*{name}\s*=>.*$',                             # match arm
        r'^\s*{name}\s*,\s*$',                             # enum variant
    ],
    'ruby': [
        r'^\s*when\s+.*\b{name}\b.*$',
        r'^\s*{name}\s*=.*$',
    ],
    'kotlin': [
        r'^\s*{name}\s*->.*$',                             # when branch
        r'^\s*{name}\s*,?\s*$',
    ],
    'csharp': [
        r'^\s*case\s+.*\b{name}\b.*:.*$',
        r'^\s*{name}\s*,?\s*$',
    ],
}


def _apply_patterns(code: str, patterns: list, names: set) -> str:
    """Apply each {name}-templated pattern for every case variant."""
    for raw in patterns:
        for name in names:
            pattern = raw.replace('{name}', re.escape(name))
            code = re.sub(pattern + r'\n?', '', code, flags=re.MULTILINE)
    return code


def _symbol_names(variants: dict) -> set:
    """Case variants worth matching for a type or enum symbol."""
    return {v for v in (variants.get('pascal'), variants.get('camel'),
                        variants.get('snake'), variants.get('upper_snake')) if v}


def _remove_type_reference(code: str, variants: dict, lang: str) -> str:
    if lang == "python":
        # Python delegates to the syntax-aware codemod. The shared regex path was
        # DESTRUCTIVE here, not merely useless: its import pattern deleted the whole
        # `from X import A, B` statement when the removed name appeared anywhere on
        # it, so a still-used neighbour became undefined while every reference to the
        # removed type survived -- and it reported success. Measured, see
        # app/py_codemod.py::remove_type.
        from .py_codemod import remove_type as _codemod
        return _run_codemod(code, variants['pascal'], "python", _codemod)
    if lang in ("typescript", "javascript"):
        # Same defect, twice more, from the shared table:
        #
        #   typescript  `_TYPE_REF_PATTERNS['typescript']` opens with the identical
        #               whole-import-line deletion, so `import { User, Address }`
        #               vanished and `Address` became undefined.
        #   javascript  has NO entry, so it fell through to `_generic_remove`, which
        #               deletes every line matching a CASE VARIANT of the name --
        #               and `variants['snake'] == variants['camel'] == 'user'` for a
        #               type named `User`. A two-function module was reduced to `}`.
        #
        # They share one codemod because they already share `remove_field`, and the
        # editable shape (a binding list) is common to both. See
        # app/ts_codemod.py::remove_type.
        from .ts_codemod import remove_type as _codemod
        return _run_codemod(code, variants['pascal'], lang, _codemod)
    patterns = _TYPE_REF_PATTERNS.get(lang)
    if patterns is None:
        # No pattern table and no codemod. Use the CONSERVATIVE fallback, which
        # abstains rather than guessing: `_generic_remove` deleted every line
        # matching a case variant of the name, and destroyed all six of these
        # languages on an idiomatic consumer. See _generic_symbol_remove.
        return _generic_symbol_remove(code, variants)
    return _apply_patterns(code, patterns, _symbol_names(variants))


def _remove_case_block(code: str, names: set, lang: str) -> str:
    """Remove a whole switch/case arm, including its body.

    Removing only the `case X:` line orphans the statements beneath it:

        switch s {
            return 1          <- was the body of the removed arm
        case Status_ACTIVE:

    which does not compile. C-style languages need the arm body removed up to
    the next case/default or the closing brace.
    """
    c_style = lang in ('go', 'typescript', 'javascript', 'java', 'csharp', 'c#')
    if not c_style:
        return code

    lines = code.split('\n')
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        is_case = stripped.startswith('case ') or stripped.startswith('case\t')
        # Boundary must treat '_' as a separator: Go protobuf enums render as
        # Status_LEGACY, and \bLEGACY\b does NOT match there because '_' is a
        # word character. Same underscore-boundary trap as the consumer
        # matcher hit earlier.
        if is_case and any(
            re.search(rf'(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])', line)
            for n in names
        ):
            # Skip this arm: the case line plus its body up to the next
            # case/default, or the block's closing brace.
            indent = len(line) - len(line.lstrip())
            i += 1
            while i < len(lines):
                nxt = lines[i]
                nstr = nxt.strip()
                if nstr.startswith('case ') or nstr.startswith('default'):
                    break
                # closing brace at or above the case's indentation ends the switch
                if nstr.startswith('}') and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    return '\n'.join(out)


def _remove_inline_enum_member(code: str, names: set) -> str:
    """Remove a member from a single-line enum declaration.

    Found by the coverage matrix: the multiline form worked but the inline one
    did not, and inline enums are common in Java and C#:

        enum Status { LEGACY, ACTIVE }   ->   enum Status { ACTIVE }

    The line-wise patterns cannot express this because the member is not on
    its own line.
    """
    def _strip(match):
        head, body, tail = match.group(1), match.group(2), match.group(3)
        members = [m.strip() for m in body.split(',')]
        kept = [
            m for m in members
            if m and not any(
                re.fullmatch(rf'{re.escape(n)}(\s*=.*)?', m) for n in names
            )
        ]
        return f"{head}{', '.join(kept)}{tail}"

    # enum Name { A, B }  /  enum class Name { A, B }
    return re.sub(
        r'(\benum\s+(?:class\s+)?\w+\s*\{\s*)([^{}\n]*?)(\s*\})',
        _strip, code
    )


def _remove_enum_value(code: str, variants: dict, lang: str) -> str:
    names = _symbol_names(variants)
    # Whole-arm removal first, so bodies are not orphaned.
    code = _remove_case_block(code, names, lang)
    # Inline single-line enum declarations, which line patterns cannot reach.
    code = _remove_inline_enum_member(code, names)
    patterns = _ENUM_VALUE_PATTERNS.get(lang)
    if patterns is None:
        # Same conservative fallback as remove_type. An enum value is less
        # collision-prone than a type -- there is no convention of naming a variable
        # after the value -- but the rule is the same and one copy is better than
        # two. The useful shapes (`- pending` in yaml, `PENDING = 3,`) still qualify
        # as "exists only to name the symbol"; a prose line no longer does.
        #
        # include_upper because codegen emits enum values as `PENDING`. It stays OFF
        # for types, where `to_upper_snake("User")` is `USER` and `$USER` is an
        # environment variable, not a reference.
        return _generic_symbol_remove(code, variants, include_upper=True)
    return _apply_patterns(code, patterns, names)


# ---------------------------------------------------------------------------
# JUDGMENT operations
#
# These CANNOT be completed mechanically without changing behaviour:
#   remove_operation  deleting a call site removes functionality
#   add_required      inventing a value for a new required field is a guess
#   restrict_schema   narrowing a signature needs a semantic decision
#
# But leaving the code untouched is not acceptable either: fixed_code ==
# content means no PR opens, so a detected breaking change produces silence --
# the exact failure this whole plan exists to remove.
#
# So: annotate every affected site with a precise, greppable marker and let
# find_residual_references flag the rest. The diff is non-empty (a PR opens),
# points at exact lines, and never pretends to be a finished fix.
# ---------------------------------------------------------------------------

_LINE_COMMENT = {
    'go': '//', 'typescript': '//', 'javascript': '//', 'java': '//',
    'rust': '//', 'kotlin': '//', 'csharp': '//', 'c#': '//',
    'swift': '//', 'scala': '//', 'dart': '//', 'php': '//',
    'python': '#', 'ruby': '#', 'shell': '#', 'yaml': '#',
}

MARKER = 'RIPPLE-ACTION-REQUIRED'


def _comment_token(lang: str) -> str:
    return _LINE_COMMENT.get(lang, '#')


def _matches_symbol(line: str, names: set) -> bool:
    """Symbol match where '_' counts as a boundary (Status_LEGACY, get_user)."""
    return any(
        re.search(rf'(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])', line)
        for n in names
    )


def _annotate_sites(code: str, names: set, lang: str, note: str) -> tuple[str, int]:
    """Insert a marker comment above each line referencing the symbol.

    Returns (annotated_code, sites_annotated). Skips lines that are already
    comments, and never annotates the same line twice.
    """
    token = _comment_token(lang)
    out = []
    count = 0
    prev_was_marker = False
    for line in code.split('\n'):
        stripped = line.strip()
        is_comment = stripped.startswith(token) or stripped.startswith('*')
        if (not is_comment and stripped and _matches_symbol(line, names)
                and not prev_was_marker):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{token} {MARKER}: {note}")
            count += 1
        out.append(line)
        prev_was_marker = MARKER in line
    return '\n'.join(out), count


def _annotate_hint_sites(code: str, hints, lang: str, note: str) -> tuple[str, int]:
    """Annotate lines containing any of `hints` as a plain substring.

    Exists for add_required, where the usual anchor cannot work: a NEWLY
    required field is by definition absent from consumer code, so matching on
    field-name variants finds nothing and every add_required fix fell through
    to a file-top marker ("somewhere in this file, supply X"). The anchor has
    to be the CALL SITE instead -- the endpoint path literal, or the type being
    constructed.

    Substring rather than word-boundary matching, because the useful anchors are
    not identifiers: `"/users"` has no word boundary before the slash.

    Returns (annotated_code, sites_annotated).
    """
    token = _comment_token(lang)
    out = []
    count = 0
    prev_was_marker = False
    # Short hints match too much: a 2-char anchor would tag half the file.
    usable = [h for h in hints if h and len(h) >= 3]
    for line in code.split('\n'):
        stripped = line.strip()
        is_comment = stripped.startswith(token) or stripped.startswith('*')
        if (not is_comment and stripped and not prev_was_marker
                and any(h in line for h in usable)):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{token} {MARKER}: {note}")
            count += 1
        out.append(line)
        prev_was_marker = MARKER in line
    return '\n'.join(out), count


def _comment_out_sites(code: str, names: set, lang: str, note: str) -> tuple[str, int]:
    """Comment OUT lines referencing the symbol, with a marker above.

    Used only for remove_operation: the call target no longer exists, so the
    line cannot compile as written. Commenting it out keeps the original
    visible in the diff, whereas deleting it would hide that functionality was
    dropped.

    This does NOT guarantee a compiling file -- commenting out an assignment
    can leave dependent statements referencing undefined variables. Resolving
    that is the human decision the marker exists to prompt.
    """
    token = _comment_token(lang)
    out = []
    count = 0
    for line in code.split('\n'):
        stripped = line.strip()
        is_comment = stripped.startswith(token) or stripped.startswith('*')
        if not is_comment and stripped and _matches_symbol(line, names):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{token} {MARKER}: {note}")
            out.append(f"{indent}{token} {stripped}")
            count += 1
            continue
        out.append(line)
    return '\n'.join(out), count


# --- Main entry point ---

def annotate_references(code: str, language: str, symbol: str,
                        note: str) -> tuple[str, str]:
    """Mark every reference to `symbol` with MARKER and `note`. Changes nothing else.

    The escape hatch for a break Ripple has DETECTED but cannot describe well
    enough to transform -- an engine that reports a rename without naming the
    target, or a type change without reporting from/to.

    Exists because the alternatives are both wrong. Returning the code unchanged
    means fixed_code == content, so no PR opens and detection becomes silence.
    Borrowing another operation's template lies: routing an unnamed rename
    through field_removed DELETES references to a field that still exists under
    a new name, and the note would read "Removed all references".

    Always yields a non-empty diff so a PR opens: falls back to a file-top
    marker when no line references the symbol.
    """
    lang = language.lower().strip()
    variants = name_variants(symbol)
    result, sites = _annotate_sites(code, _symbol_names(variants), lang, note)
    where = f"{sites} site(s)"
    if sites == 0:
        token = _comment_token(lang)
        result = f"{token} {MARKER}: {note}\n" + code
        sites, where = 1, "file (no line-level reference found)"
    return result, (f"PARTIAL: marked {where} with {MARKER}. {note} Ripple did "
                    f"NOT transform the code: it lacks the information to do so "
                    f"correctly, and guessing would be worse than flagging.")


def apply_fix_template(
    code: str,
    language: str,
    change_type: str,
    field_name: str,
    new_name: str = '',
    old_type: str = '',
    new_type: str = '',
    site_hints: tuple = (),
) -> tuple[str, str]:
    """
    Apply a deterministic fix template to source code.

    Args:
        code: Source code to modify.
        language: Programming language (go, typescript, python, java, rust, ruby, kotlin, csharp).
        change_type: One of field_removed, removed_field, field_renamed, renamed_field, type_changed, field_type_changed.
        field_name: The field being changed.
        new_name: For rename operations, the new field name.
        old_type: For type change operations, the original type.
        new_type: For type change operations, the new type.
        site_hints: Call-site anchors (endpoint path literal, constructed type
            name) used ONLY by add_required, whose field is absent from consumer
            code by definition. Without them the fix can only mark the file, not
            the line. Substring-matched, so `"/users"` works.

    Returns:
        Tuple of (fixed_code, explanation_string).
    """
    lang = language.lower().strip()
    ct = change_type.lower().strip().replace('-', '_')
    variants = name_variants(field_name)

    # Route through the canonical taxonomy so every engine dialect reaches a
    # handler. Previously this matched 3 literal strings and returned
    # "Unknown change_type" for the other 44 -- which left the code unchanged,
    # so no PR opened and a detected breaking change produced silence.
    from .change_types import canonical_op, category, describe
    op = canonical_op(ct)

    if op == 'remove_field':
        handler = REMOVE_HANDLERS.get(lang)
        if handler is None:
            # Fallback: generic line removal for unsupported languages
            result = _generic_remove(code, variants)
        else:
            result = handler(code, variants)
        result = _postprocess(result)
        lines_removed = len(code.split('\n')) - len(result.split('\n'))
        if result == code:
            # "Removed all references ... (0 lines affected)" was a false claim in a
            # user-facing string, and it read identically whether the handler had
            # done nothing or corrupted the file. Say what happened instead.
            explanation = (
                f"Could NOT remove references to field '{field_name}' in {lang}: no "
                f"reference matched a shape this transformation can remove safely. "
                f"The code is unchanged."
            )
        else:
            # State the shapes ACTUALLY edited. The previous text was a fixed list --
            # "Cleaned: struct/class declarations, accessor methods, function params,
            # object literals, and direct field access patterns" -- appended to every
            # success regardless of what happened. It was a second false claim in the
            # same string as the first: even when the codemod worked, it named five
            # categories it had not necessarily touched. A codemod that reports its
            # own edits cannot drift from them.
            shapes = _LAST_CODEMOD_RESULT.get("edits") or []
            if shapes:
                counted = ", ".join(
                    f"{n}x {s}" if n > 1 else s
                    for s, n in sorted(collections.Counter(shapes).items()))
                detail = f"Removed: {counted}."
            else:
                detail = ("No shape detail available -- this language has no "
                          "syntax-aware codemod, so the edit set is not itemised.")
            explanation = (
                f"Removed references to field '{field_name}' "
                f"({lines_removed} lines affected). {detail}"
            )
        if _LAST_CODEMOD_RESULT.get("language") == lang:
            for note in _LAST_CODEMOD_RESULT.get("notes", []):
                explanation += f"\nNOTE: {note}"
            for refusal in _LAST_CODEMOD_RESULT.get("refusals", []):
                explanation += f"\nNEEDS A HUMAN: {refusal}"
        return result, explanation

    elif op == 'rename_field':
        if not new_name:
            return code, "Error: new_name required for rename operations."
        old_variants = name_variants(field_name)
        new_variants = name_variants(new_name)
        result = _rename_field(code, old_variants, new_variants)
        # DISTINCT variant STRINGS, not the four variant KEYS. For an all-lowercase
        # name snake and camel are the same string ("alltrue" == "alltrue"), so
        # summing per key counted every replacement twice and reported "4
        # replacements made" for a two-line edit. That number goes into a pull
        # request body on a repository we do not own, where a maintainer can count
        # the diff and see it disagree -- the same class of defect as a fix labelled
        # with a backend that never answered.
        counted = sorted({old_variants[s] for s in
                          ('snake', 'camel', 'pascal', 'upper_snake')})
        replacements = sum(code.count(v) - result.count(v) for v in counted)
        explanation = (
            f"Renamed '{field_name}' -> '{new_name}' across all case variants "
            f"(snake_case, camelCase, PascalCase, UPPER_SNAKE). "
            f"{replacements} replacements made. String literals and comments preserved."
        )
        return result, explanation

    elif op == 'change_field_type':
        if not old_type or not new_type:
            return code, "Error: old_type and new_type required for type change operations."

        # Translate CONTRACT vocabulary into the consumer language's. Engines
        # emit proto/JSON-Schema/Thrift type names; source code contains none of
        # them. See CONTRACT_TYPE_TO_NATIVE for what this was doing wrong in all
        # nine languages before.
        native_old = native_type(lang, old_type)
        native_new = native_type(lang, new_type)

        if lang in UNTYPED_LANGUAGES:
            # No declaration to edit. The change is still real -- the value's
            # shape changes at runtime -- so flag the references instead of
            # pretending a type annotation was updated.
            return annotate_references(
                code, lang, field_name,
                f"type of '{field_name}' changed from {old_type} to {new_type} "
                f"in the contract; {lang} has no type annotation to update -- "
                f"check the code that reads this value.")

        if not (native_old and native_new):
            # Refuse to write an unmapped contract name into source. Doing that
            # is what produced `phoneNumber: int32;` in TypeScript and
            # `public int32 PhoneNumber` in C# -- confident diffs that do not
            # compile, reported as success.
            unknown = old_type if not native_old else new_type
            return annotate_references(
                code, lang, field_name,
                f"type of '{field_name}' changed from {old_type} to {new_type}, "
                f"and Ripple has no {lang} equivalent for '{unknown}' -- update "
                f"the declaration by hand.")

        handler = TYPE_CHANGE_HANDLERS.get(lang)
        if handler is None:
            result = re.sub(rf'\b{re.escape(native_old)}\b', native_new, code)
        else:
            result = handler(code, native_old, native_new)

        if result == code:
            # The type is mapped but no declaration matched -- annotate rather
            # than return unchanged code, because unchanged code opens no PR and
            # a detected break would produce silence.
            return annotate_references(
                code, lang, field_name,
                f"type of '{field_name}' changed from {native_old} to "
                f"{native_new} -- no declaration matched automatically, verify "
                f"these references.")

        # Count the NATIVE token: counting the contract name reported 0 even
        # when replacements happened, and vice versa.
        replacements = code.count(native_old) - result.count(native_old)
        explanation = (
            f"Changed type '{native_old}' -> '{native_new}' in {lang} code "
            f"(contract: {old_type} -> {new_type}). "
            f"{replacements} type annotation(s) updated."
        )
        return result, explanation

    elif op == 'remove_type':
        result = _remove_type_reference(code, variants, lang)
        result = _postprocess(result)
        lines_removed = len(code.split('\n')) - len(result.split('\n'))
        # The old text asserted "Removed references to deleted type 'X' (N lines
        # affected): imports, declarations, type annotations and constructions" on
        # EVERY outcome -- including N=0, where nothing was removed at all, and
        # including the destructive case where an import line had been deleted out
        # from under a still-used neighbour. Same fabricated-list defect the
        # remove_field explanation had. Say what happened.
        if result == code:
            explanation = (
                f"Could NOT remove references to deleted type '{field_name}' in "
                f"{lang}: a deleted type has no substitute value, so nothing here "
                f"matched a shape this transformation can remove safely. The code is "
                f"unchanged."
            )
        else:
            shapes = _LAST_CODEMOD_RESULT.get("edits") or []
            if shapes:
                counted = ", ".join(
                    f"{n}x {s}" if n > 1 else s
                    for s, n in sorted(collections.Counter(shapes).items()))
                detail = f"Removed: {counted}."
            else:
                detail = (f"No shape detail available -- {lang} has no syntax-aware "
                          f"type-removal codemod, so the edit set is not itemised.")
            explanation = (
                f"Removed references to deleted type '{field_name}' "
                f"({lines_removed} lines affected). {detail}"
            )
        if _LAST_CODEMOD_RESULT.get("language") == lang:
            for note in _LAST_CODEMOD_RESULT.get("notes", []):
                explanation += f"\nNOTE: {note}"
            for refusal in _LAST_CODEMOD_RESULT.get("refusals", []):
                explanation += f"\nNEEDS A HUMAN: {refusal}"
        return result, explanation

    elif op == 'remove_enum_value':
        result = _remove_enum_value(code, variants, lang)
        result = _postprocess(result)
        lines_removed = len(code.split('\n')) - len(result.split('\n'))
        # The old text asserted "Removed references to deleted enum value 'X'
        # (N lines affected): switch/case arms, match arms and constant declarations
        # for {lang}" on EVERY outcome -- including N=0 with the file untouched, where
        # it named three shapes it had not cleaned. Measured:
        #
        #   apply_fix_template("const x = 1;\n", "typescript",
        #                      "removed_enum_value", "NOPE")
        #   -> unchanged, and "Removed references to deleted enum value 'NOPE'
        #      (0 lines affected): switch/case arms, match arms and constant
        #      declarations for typescript."
        #
        # Third occurrence of the fabricated-shape-list defect, after remove_field
        # and remove_type. Say what happened instead.
        if result == code:
            explanation = (
                f"Could NOT remove references to deleted enum value '{field_name}' "
                f"in {lang}: nothing here matched a shape this transformation can "
                f"remove safely. The code is unchanged."
            )
        else:
            explanation = (
                f"Removed references to deleted enum value '{field_name}' "
                f"({lines_removed} lines affected): switch/case arms, match arms "
                f"and constant declarations for {lang}."
            )
        return result, explanation

    elif op == 'rename_type':
        if not new_name:
            return code, "Error: new_name required for rename operations."
        result = _rename_field(code, name_variants(field_name), name_variants(new_name))
        replacements = sum(
            code.count(name_variants(field_name)[s]) - result.count(name_variants(field_name)[s])
            for s in ('snake', 'camel', 'pascal', 'upper_snake')
        )
        explanation = (
            f"Renamed type '{field_name}' -> '{new_name}' across all case "
            f"variants. {replacements} replacements made."
        )
        return result, explanation

    elif op == 'remove_operation':
        # The rpc/method/endpoint no longer exists, so the call site cannot
        # compile as written. Comment it out rather than delete it: deleting
        # would hide that functionality was dropped, and commenting keeps the
        # original line visible in the diff for whoever decides the fix.
        note = (f"'{field_name}' was removed from the contract. This call is "
                f"commented out -- restore an equivalent or delete deliberately.")
        result, sites = _comment_out_sites(code, _symbol_names(variants), lang, note)
        explanation = (
            f"PARTIAL: '{field_name}' was removed from the service contract. "
            f"Commented out {sites} call site(s) and marked each with {MARKER}. "
            f"NOTE: this file may still not compile -- commenting out an "
            f"assignment can leave dependent statements referencing undefined "
            f"variables. Removing an operation drops functionality, so the "
            f"replacement is a human decision and Ripple deliberately did not "
            f"choose one."
        )
        return result, explanation

    elif op == 'add_required':
        # Deliberately does NOT invent a value. Guessing a required field's
        # value is a silent behaviour change and the most likely way to ship a
        # confidently wrong fix.
        note = (f"required field '{field_name}' was added to the contract -- "
                f"supply a value at this construction site.")

        # Anchor on the CALL SITE, not the field. A newly required field cannot
        # appear in consumer code yet, so the field-name match below can only
        # ever miss -- measured both ways before this was added, and every
        # add_required fix landed as a file-top marker.
        result, sites = _annotate_hint_sites(code, site_hints, lang, note)
        precision = "call site(s)"

        if sites == 0:
            # The field name may still appear (e.g. an optional param of the
            # same name already exists), so this is worth trying second.
            result, sites = _annotate_sites(code, _symbol_names(variants), lang, note)
            precision = "site(s)"

        if sites == 0:
            token = _comment_token(lang)
            result = (f"{token} {MARKER}: required field '{field_name}' added "
                      f"to the contract; no construction site detected in this "
                      f"file -- verify manually.\n" + code)
            sites = 1
            precision = "file (no line-level anchor found)"
        explanation = (
            f"PARTIAL: required field '{field_name}' was added. Marked "
            f"{sites} {precision} with {MARKER}. Ripple did NOT invent a value: "
            f"choosing one silently changes behaviour, so the value is left to "
            f"a human."
        )
        return result, explanation

    elif op == 'restrict_schema':
        note = (f"schema for '{field_name}' was narrowed (signature/type/"
                f"additionalProperties) -- verify this call still satisfies it.")
        result, sites = _annotate_sites(code, _symbol_names(variants), lang, note)
        if sites == 0:
            token = _comment_token(lang)
            result = (f"{token} {MARKER}: schema for '{field_name}' was "
                      f"narrowed; no direct reference found in this file -- "
                      f"verify manually.\n" + code)
            sites = 1
        explanation = (
            f"PARTIAL: the schema for '{field_name}' was narrowed. Marked "
            f"{sites} site(s) with {MARKER}. Reconciling a narrowed schema "
            f"requires a semantic decision Ripple cannot make safely."
        )
        return result, explanation

    elif op == 'remove_package':
        # A whole contract file or directory is gone, so every symbol it
        # declared vanished at once. Two kinds of consumer file exist and they
        # need different treatment, but only a human can supply the
        # replacement, so both are annotated rather than edited:
        #
        #   importers  -- the import path no longer resolves. Commenting out the
        #                 import alone guarantees the file stops compiling,
        #                 since every use of it is now undefined. So annotate
        #                 the import instead of removing it, keeping the broken
        #                 dependency visible in the diff.
        #   members    -- files inside a deleted directory were themselves
        #                 removed upstream; nothing here can fix them.
        #
        # Deleting the import and its call sites would compile but silently drop
        # whatever the package did, which is the outcome Ripple must never
        # choose on the customer's behalf.
        note = (f"the contract '{field_name}' was DELETED upstream. Every symbol "
                f"it declared is gone. Restore an equivalent dependency or "
                f"remove this usage deliberately.")
        names = _symbol_names(variants)
        # Match on the path too, since an import references the directory rather
        # than any identifier: `import \"api/v1/user.proto\"`.
        tail = field_name.rstrip('/').rsplit('/', 1)[-1]
        if tail and tail not in names:
            names = names | {tail}
        result, sites = _annotate_sites(code, names, lang, note)
        if sites == 0:
            # No textual reference, yet this file was reported as a consumer --
            # most often a member of the deleted directory, whose relationship
            # is structural rather than by name. Mark it anyway: an unchanged
            # file opens no PR, which would turn a detected deletion back into
            # silence.
            token = _comment_token(lang)
            result = (f"{token} {MARKER}: the contract '{field_name}' was deleted "
                      f"upstream. This file referenced it structurally rather "
                      f"than by name (e.g. it lived inside the deleted "
                      f"package) -- review whether it should be removed or "
                      f"repointed.\n" + code)
            sites = 1
        explanation = (
            f"PARTIAL: the contract '{field_name}' was DELETED upstream, so every "
            f"symbol it declared is gone at once. Annotated {sites} site(s) with "
            f"{MARKER}. Nothing was edited or removed: dropping the import would "
            f"leave every usage undefined, and dropping the usages would silently "
            f"delete behaviour. Restoring an equivalent dependency or retiring "
            f"the code is a product decision Ripple deliberately did not make."
        )
        return result, explanation

    elif op == 'wire_incompatible':
        # A proto field number or thrift field id changed. This breaks the
        # SERIALIZATION contract, not the source contract: consumer code never
        # references field numbers, so there is nothing in the source to fix.
        #
        # Returning the code unchanged is therefore the CORRECT outcome, not a
        # failure. The distinction matters because unchanged code means no PR
        # opens -- callers must use is_wire_only() to tell "correctly nothing
        # to do" apart from "we could not fix it", and must still surface the
        # break, because it silently corrupts data between old and new peers.
        explanation = (
            f"NO SOURCE CHANGE REQUIRED: '{field_name}' had its field "
            f"number/id changed. This is a WIRE-COMPATIBILITY break -- "
            f"previously serialized data and any peer running the old schema "
            f"will misinterpret this field. Source code does not reference "
            f"field numbers, so no consumer edit can fix it. Resolve by "
            f"restoring the original number, or by coordinating a synchronised "
            f"redeploy of all producers and consumers."
        )
        return code, explanation

    else:
        # Every change_type the engines emit is classified in change_types.py,
        # so reaching here means either a genuinely new dialect or a category
        # handled in a later stage (judgment / wire-only). Report the category
        # rather than a bare "unknown", so the caller can act on it.
        cat = category(ct)
        if cat:
            return code, (
                f"No mechanical template for '{change_type}' "
                f"(category: {cat}) -- {describe(ct)}."
            )
        return code, (
            f"Unclassified change_type: '{change_type}'. "
            f"Add it to app/change_types.py CHANGE_TYPE_MAP."
        )

def _generic_remove(code: str, variants: dict[str, str]) -> str:
    """Fallback removal for unsupported languages: remove lines containing field name variants."""
    for style in ('snake', 'camel', 'pascal'):
        name = variants[style]
        pattern = re.compile(rf'^\s*.*\b{re.escape(name)}\b.*$\n?', re.MULTILINE)
        code = _remove_lines_matching(code, pattern)
    return code


#: Keywords that begin an import-like statement in the languages that reach the
#: generic fallback (dart, php, scala, shell, swift, yaml) plus the ones that do not.
_IMPORT_KW = r'(?:#include|import|from|use|using|require|include|open|package)'

#: What may remain on a line after the symbol is removed for the line to count as
#: "existing only to name that symbol". Punctuation, quotes and digits only -- any
#: surviving LETTER means the line carries other meaning and must not be deleted.
#: Digits are allowed so an ordinal-assigned enum member (`PENDING = 3,`) qualifies.
_STRUCTURAL_ONLY = re.compile(r'^[\s\-,:;()\[\]{}|=<>+*&.\'"`$0-9]*$')


def _declared_symbol_names(variants: dict, include_upper: bool = False) -> list:
    """The forms a TYPE or ENUM VALUE is actually WRITTEN in.

    Deliberately excludes the snake and camel variants that `_symbol_names` includes,
    because for a symbol those are not the symbol -- they are the conventional name
    of a VARIABLE holding one, or of a property. `name_variants("User")` yields snake
    `user` and camel `user`, and matching those is precisely what destroyed the six
    fallthrough languages:

        shell   echo "looking up user $user_id"     deleted -- prose, not a type
        yaml    user:                               deleted -- a property KEY, and
                                                    it left the $ref orphaned

    `literal` is included because it is the only form guaranteed to appear: an
    OpenAPI enum value declared `pending` is written `pending`, and pascal/upper-snake
    would both miss it.

    UPPER-SNAKE IS OPT-IN, and only for enum values, where codegen conventionally
    emits `PENDING`. It is NOT used for types, because `to_upper_snake("User")` is
    `USER` and that collides with a bare upper-case list entry. Measured -- removing
    a TYPE called `User` from

        required:
          - USER
          - HOME

    deletes `- USER` when upper-snake is enabled, because after the symbol is removed
    the line is structural-only. A list of required environment variables has nothing
    to do with a type, and nothing else in the file references it, so the removal
    would look COMPLETE and ship.

    (An earlier version of this comment claimed `echo "$USER"` was the hazard. It is
    not: the word `echo` survives, so the structural-only rule already refuses that
    line. The claim was wrong and mutation-testing caught it -- flipping upper-snake
    on for types left the test green until the assertion was rewritten around the
    case that actually manifests.)
    """
    keys = ['literal', 'pascal'] + (['upper_snake'] if include_upper else [])
    out = []
    for key in keys:
        value = variants.get(key)
        if value and value not in out:
            out.append(value)
    return out


def _generic_symbol_remove(code: str, variants: dict,
                           include_upper: bool = False) -> str:
    """Conservative fallback for a removed TYPE or ENUM VALUE in a language with no
    syntax-aware codemod and no pattern table.

    WHY THIS REPLACES _generic_remove FOR SYMBOLS
    `_generic_remove` deletes every line matching any CASE VARIANT of the name, and
    for a symbol that is catastrophic rather than merely broad. Measured through
    apply_fix_template, every one of the six fallthrough languages was destroyed by a
    single removed type:

        dart    import 'models.dart';  String label(User u){return u.email;}
                ->  import 'models.dart';  }        the stale import SURVIVED and
                                                    the live function was deleted
        scala   3 lines  ->  one blank line         entire file gone
        shell   echo "looking up user $user_id"  DELETED -- shell has no types at all
        yaml    the $ref AND its parent key gone, leaving `properties:` childless

    Note the inversion in dart/php/swift: the one line that IS stale -- the import --
    is the one that survived. It deleted the code and kept the reference.

    THE RULE, IN TWO PARTS
    1. Delete a line only when it exists SOLELY to name this symbol:

           import-like statement whose last identifier is the symbol
               import models.User        use App\\Models\\User;

           a bare member or list item
               - PENDING                 PENDING = 3,               PENDING,

    2. THEN require the removal to be COMPLETE. If any reference survives, put the
       original code back and refuse the whole thing.

    Part 2 is the part that is easy to miss and was measured as a real regression in
    the first draft of this function: php and scala correctly dropped the stale
    `use`/`import` line but left `User` in a live signature, so the output had the
    type both undefined AND unimported -- strictly worse than not touching the file.
    That is the same reason `CodemodResult.complete` requires "no refusals" rather
    than "some edits": a partial removal is not a smaller success, it is a different
    failure.

    So this completes exactly one real case -- a stale import of a type that is never
    used -- and refuses everything else. The caller sees `result == code` and reports
    "Could NOT remove ... The code is unchanged.", which is honest and, for languages
    with NO wired validator to catch a bad fix, the only safe answer.
    """
    names = _declared_symbol_names(variants, include_upper)
    if not names:
        return code
    # Longest first so `UserProfile` is considered before `User`.
    names.sort(key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    occurrence = re.compile(rf'\b(?:{alternation})\b')

    kept = []
    for line in code.split('\n'):
        if occurrence.search(line) and _line_exists_only_to_name(line, occurrence):
            continue
        kept.append(line)
    out = '\n'.join(kept)

    # Part 2: all or nothing. A surviving reference means the edit was partial.
    if occurrence.search(out):
        return code
    return out


def _line_exists_only_to_name(line: str, occurrence: re.Pattern) -> bool:
    """Is this line's entire purpose to name the symbol?"""
    # Shape 1: an import-like statement whose LAST identifier is the symbol, and
    # which does not import a comma-separated list. `import a.{Address, User}` is
    # refused in both directions: a comma before the symbol means a neighbour would
    # be lost, and text after it means the symbol was not the thing imported.
    if re.match(rf'^[ \t]*{_IMPORT_KW}\b', line, re.IGNORECASE):
        last = None
        for m in occurrence.finditer(line):
            last = m
        if last is not None:
            before, after = line[:last.start()], line[last.end():]
            if ',' not in before and re.fullmatch(r"[ \t]*[;'\")\}\]]*[ \t]*", after):
                return True
        return False

    # Shape 2: a bare member or list item -- removing the symbol leaves nothing but
    # structure. Any surviving letter means the line carries other meaning.
    return bool(_STRUCTURAL_ONLY.fullmatch(occurrence.sub('', line)))
