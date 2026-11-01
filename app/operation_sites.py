"""Where a consumer calls a REMOVED OPERATION -- one rule for finder and fixer.

A removed endpoint, channel, table or GraphQL root field is named by consumers
in a shape a plain identifier search cannot see:

    endpoint   GET /orders     client.get("/orders")         a path in a string
    channel    user/deleted    producer.send("user/deleted") a quoted literal
    table      orders          "SELECT id FROM orders"       SQL inside a string
    graphql    Query.orders    gql`{ orders { id } }`        a root selection

The symbol matcher looked for `GET /orders` / `user/deleted` / `orders` as
identifiers, so all three of the first found 0 consumers, and the GraphQL case
matched every line mentioning `orders` -- including `client.get("/orders")`,
which the fixer then commented out.

operation_sites() is the ONE rule: the consumer finder admits a file by it and
the fixer marks exactly the lines it returns, so the two cannot disagree.
"""
from __future__ import annotations

import re

#: vectors whose consumer lines are located here, not by symbol search
OPERATION_VECTORS = ("endpoint", "channel", "table", "graphql_operation")

_ROOT_KIND = {"Query": "query", "Mutation": "mutation",
              "Subscription": "subscription"}


# ─── string regions ─────────────────────────────────────────────────────────

def multiline_string_opens(content: str) -> dict:
    """{line index: index of the line its enclosing multi-line string opened
    on} for every line that STARTS inside a backtick or triple-quoted string.

    Used to keep markers out of query strings: a `//` or a commented-out line
    inserted inside `gql\\`...\\`` or a SQL \"\"\"...\"\"\" becomes part of the
    query and breaks it.
    """
    out, delim, opened = {}, None, None
    for i, line in enumerate(content.split("\n")):
        if delim is not None:
            out[i] = opened
        j = 0
        while j < len(line):
            if delim is None:
                if line.startswith('"""', j) or line.startswith("'''", j):
                    delim, opened, j = line[j:j + 3], i, j + 3
                    continue
                if line[j] == "`":
                    delim, opened, j = "`", i, j + 1
                    continue
                if line[j] in "\"'":
                    # An ordinary one-line string: skip it whole, so a
                    # backtick inside "..." does not open a template.
                    q, j = line[j], j + 1
                    while j < len(line) and line[j] != q:
                        j += 2 if line[j] == "\\" else 1
                    j += 1
                    continue
            else:
                if line[j] == "\\":
                    j += 2
                    continue
                if line.startswith(delim, j):
                    j += len(delim)
                    delim = None
                    continue
            j += 1
    return out


def _doc_spans(content: str, language: str) -> list:
    """(start, end) offsets of GraphQL documents in a file: the whole file
    for a .graphql document, else backtick and triple-quoted strings."""
    if language == "graphql":
        return [(0, len(content))]
    spans = []
    for m in re.finditer(r'`((?:\\.|[^`\\])*)`|"""(.*?)"""|\'\'\'(.*?)\'\'\'',
                         content, re.S):
        g = next(i for i in (1, 2, 3) if m.group(i) is not None)
        spans.append((m.start(g), m.end(g)))
    return spans


# ─── GraphQL documents ──────────────────────────────────────────────────────

_TYPE_SYSTEM = re.compile(
    r'^\s*(?:extend\s+)?(?:type|input|interface|enum|union|scalar|schema|'
    r'directive)\b', re.M)
_EXECUTABLE = re.compile(r'^\s*(?:query|mutation|subscription|fragment)\b|^\s*\{',
                         re.M)


def is_operation_document(content: str) -> bool:
    """True for a .graphql file of queries/mutations/fragments -- a CONSUMER --
    and False for a schema file, which defines the contract. A file mixing
    both is treated as a schema: editing a schema as if it consumed itself is
    the worse mistake."""
    clean = re.sub(r'#[^\n]*', '', content)
    return bool(_EXECUTABLE.search(clean)) and not _TYPE_SYSTEM.search(clean)


def _blank(text: str, pattern: str) -> str:
    """Replace matches with spaces, keeping every offset."""
    return re.sub(pattern, lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text,
                  flags=re.S)


def graphql_root_selections(doc: str) -> list:
    """[(offset, kind, field name)] for every field selected directly in an
    operation's root selection set. kind is query/mutation/subscription, or
    the type a fragment is declared on."""
    return [(o, k, n) for o, k, n, d in graphql_selections(doc) if d == 1]


def graphql_selections(doc: str) -> list:
    """[(offset, kind, field name, depth)] for every field selection in a
    GraphQL document; depth 1 is the operation's root selection set."""
    return [(o, k, n, d) for o, k, n, d, _t in graphql_typed_selections(doc)]


_SPREAD_ON = re.compile(r'\.\.\.\s*on\s+([_A-Za-z]\w*)\s*(?:@\w+\s*(?:\([^)]*\))?\s*)*$')
_SPREAD_NAME = re.compile(r'\.\.\.\s*$')


def graphql_typed_selections(doc: str) -> list:
    """[(offset, kind, field name, depth, enclosing type)] for every field
    selection. `enclosing type` is the type the selection is PROVABLY made on
    from the document alone -- the type condition of the fragment or inline
    fragment (`... on Order`) whose selection set directly contains it, or the
    operation kind at the root -- and None inside a field's sub-selection,
    whose type only the schema knows."""
    text = _blank(doc, r'\$\{.*?\}')            # ${FRAGMENT} interpolations
    text = _blank(text, r'"(?:\\.|[^"\\])*"')    # string values
    text = _blank(text, r'#[^\n]*')              # comments
    out, paren, kind, last_end = [], 0, "", 0
    stack = []                                   # enclosing type per open set
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "{" and paren == 0:
            if not stack:
                header = re.findall(r'[_A-Za-z]\w*', text[last_end:i])
                if not header:
                    kind = "query"                       # anonymous `{ ... }`
                elif header[0] in ("query", "mutation", "subscription"):
                    kind = header[0]
                elif header[0] == "fragment" and "on" in header:
                    kind = header[header.index("on") + 1]
                else:
                    kind = ""
                stack.append(kind or None)
            else:
                m = _SPREAD_ON.search(text, max(0, i - 200), i)
                stack.append(m.group(1) if m else None)
        elif ch == "}" and paren == 0:
            if stack:
                stack.pop()
            if not stack:
                last_end = i + 1
        elif stack and paren == 0 and (ch.isalpha() or ch == "_"):
            m = re.compile(r'[_A-Za-z]\w*').match(text, i)
            name = m.group(0)
            after = text[m.end():m.end() + 40].lstrip()
            head = text[max(0, i - 200):i]
            # `alias:` names the result, not the field; `...Frag` is a spread
            # and `... on T` a type condition; `@skip` is a directive.
            is_spread = bool(_SPREAD_NAME.search(head))
            is_condition = bool(re.search(r'\.\.\.\s*on\s+$', head))
            if (not after.startswith(":") and not is_spread and not is_condition
                    and not (name == "on" and re.search(r'\.\.\.\s*$', head))
                    and not head.endswith("@")):
                out.append((i, kind, name, len(stack), stack[-1]))
            i = m.end()
            continue
        i += 1
    return out


def graphql_field_selection_lines(doc: str, names) -> list:
    """0-based lines of a .graphql document that select a field in `names`."""
    names = set(names)
    return _lines_of(doc, (o for o, _k, n, _d in graphql_selections(doc)
                           if n in names))


# ─── the rule ───────────────────────────────────────────────────────────────

def _lines_of(content: str, offsets) -> list:
    return sorted({content.count("\n", 0, o) for o in offsets})


def _literal_rx(name: str) -> str:
    """A channel name as a whole quoted literal; `{param}` is any segment."""
    parts = re.split(r'(\{[^}]+\})', name)
    body = "".join(r'[^"\'`/\s]+' if p.startswith("{") else re.escape(p)
                   for p in parts)
    return r'(?<=["\'`])' + body + r'(?=["\'`])'


def operation_sites(content: str, target: str, vector: str,
                    language: str = "unknown") -> list:
    """0-based lines where this file uses the removed operation `target`."""
    calls, refs = operation_site_kinds(content, target, vector, language)
    return sorted(set(calls) | set(refs))


def operation_site_kinds(content: str, target: str, vector: str,
                         language: str = "unknown") -> tuple[list, list]:
    """(calls, references), 0-based lines.

    A CALL demonstrably invokes the removed operation, so the fixer may
    comment it out: an HTTP call using the removed verb, a SQL statement on
    the table, a channel literal passed straight to a call. A REFERENCE names
    it without calling it -- a built URL, `__tablename__ = "orders"`, a topic
    constant, a GraphQL selection -- and is only marked: commenting out a
    declaration breaks the code around it.
    """
    if vector == "endpoint":
        from .smart_consumer_finder import find_endpoint_consumers, _verbs_in
        hits = find_endpoint_consumers(content, "", target, language,
                                       min_confidence=0.6)
        verb = target.partition(" ")[0].lower()
        src = content.split("\n")
        # Only a verb ON the line makes it a call to cut: the matcher also
        # credits a verb up to 3 lines away, which admits the file correctly
        # but would comment out a URL merely built next to some other call.
        calls = {m.line_number - 1 for m in hits if m.match_type == "endpoint_call"
                 and verb in _verbs_in(src[m.line_number - 1])}
        refs = {m.line_number - 1 for m in hits} - calls
        return sorted(calls), sorted(refs)
    if vector == "channel":
        rx = re.compile(_literal_rx(target))
        offs = [m.start() for m in rx.finditer(content)]
        call_offs = [o for o in offs
                     if re.search(r'\(\s*\[?\s*["\'`]$', content[max(0, o - 12):o])]
        calls = _lines_of(content, call_offs)
        return calls, [i for i in _lines_of(content, offs) if i not in calls]
    if vector == "table":
        name = re.escape(target)
        sql = re.compile(
            r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE|EXISTS)\s+(?:\w+\.)?[`"\[]?'
            + name + r'(?![\w.])', re.I)
        literal = re.compile(r'(?<=["\'`])' + name + r'(?=["\'`])')
        calls = _lines_of(content, (m.start() for m in sql.finditer(content)))
        refs = [i for i in _lines_of(content,
                                     (m.start() for m in literal.finditer(content)))
                if i not in calls]
        return calls, refs
    if vector == "graphql_operation":
        root, _, field = target.rpartition(".")
        want = _ROOT_KIND.get(root)
        offs = []
        for start, end in _doc_spans(content, language):
            for off, kind, name in graphql_root_selections(content[start:end]):
                if name != field:
                    continue
                # A custom root (`schema { query: Root }`) cannot be told
                # apart from the document side, so any operation kind counts.
                if (want is None and kind in _ROOT_KIND.values()) \
                        or kind == want or kind == root:
                    offs.append(start + off)
        # A selection is never cut: removing one line of a multi-line
        # selection leaves an unbalanced document.
        return [], _lines_of(content, offs)
    return [], []
