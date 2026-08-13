"""
ripple/app/dep_graph_viz.py

Dependency graph visualization for Ripple's ConsumerGraph.

Outputs:
- ASCII art (terminal / PR comments)
- Mermaid flowchart (GitHub markdown rendering)
- D3.js JSON (force-directed web dashboard)
- Stats markdown table (ranked endpoints)
- HTML page with embedded visualization (/graph endpoint)
"""

from __future__ import annotations

import html
import json
import time
from typing import Optional

from app.consumer_graph import ConsumerGraph, ConsumerEdge, APINode


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def _filter_edges(
    edges: list[ConsumerEdge],
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> list[ConsumerEdge]:
    """Apply common filters to a list of consumer edges."""
    now = time.time()
    stale_threshold = now - (90 * 86400)

    filtered = []
    for e in edges:
        if min_confidence > 0 and e.confidence < min_confidence:
            continue
        if consumer_repo and e.consumer_repo != consumer_repo:
            continue
        if stale_only and e.last_seen > stale_threshold:
            continue
        filtered.append(e)
    return filtered


def _filtered_nodes(
    graph: ConsumerGraph,
    endpoint: Optional[str] = None,
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> dict[str, APINode]:
    """Return nodes (optionally filtered to a single endpoint) with filtered edges."""
    if endpoint:
        # endpoint can be "METHOD:path" or just "/path"
        matching = {}
        for key, node in graph.nodes.items():
            if endpoint.upper() in key.upper() or endpoint in node.path:
                matching[key] = node
        nodes = matching
    else:
        nodes = graph.nodes

    result = {}
    for key, node in nodes.items():
        edges = _filter_edges(
            node.consumers,
            consumer_repo=consumer_repo,
            min_confidence=min_confidence,
            stale_only=stale_only,
        )
        if edges or not (consumer_repo or stale_only):
            # Create a shallow copy with filtered consumers
            filtered_node = APINode(
                spec_repo=node.spec_repo,
                spec_file=node.spec_file,
                path=node.path,
                method=node.method,
                consumers=edges,
                last_change=node.last_change,
                change_count=node.change_count,
            )
            result[key] = filtered_node
    return result


# ---------------------------------------------------------------------------
# ASCII rendering
# ---------------------------------------------------------------------------

def render_ascii(
    graph: ConsumerGraph,
    endpoint: Optional[str] = None,
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> str:
    """
    Render the consumer graph as ASCII art.

    Example output:
        POST /v1/payments
        ├── billing-service/src/client.ts  [conf: 0.95] ●●●●●
        ├── checkout/lib/pay.py            [conf: 0.80] ●●●●○
        └── mobile-api/payments.go         [conf: 0.50] ●●●○○
    """
    nodes = _filtered_nodes(graph, endpoint, consumer_repo, min_confidence, stale_only)

    if not nodes:
        return "(no matching endpoints in graph)"

    lines = []
    now = time.time()

    for key in sorted(nodes.keys()):
        node = nodes[key]
        lines.append(f"{node.method.upper()} {node.path}")

        consumers = sorted(node.consumers, key=lambda c: c.confidence, reverse=True)
        if not consumers:
            lines.append("    (no consumers)")
            lines.append("")
            continue

        for i, edge in enumerate(consumers):
            is_last = i == len(consumers) - 1
            prefix = "└── " if is_last else "├── "

            # Confidence bar (5 dots)
            filled = round(edge.confidence * 5)
            bar = "●" * filled + "○" * (5 - filled)

            # Stale marker
            days_ago = (now - edge.last_seen) / 86400
            stale_marker = " ⚠ STALE" if days_ago > 90 else ""

            repo_file = f"{edge.consumer_repo}/{edge.consumer_file}"
            # Pad for alignment
            padded = repo_file.ljust(45)
            lines.append(f"    {prefix}{padded} [conf: {edge.confidence:.2f}] {bar}{stale_marker}")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mermaid rendering
# ---------------------------------------------------------------------------

def render_mermaid(
    graph: ConsumerGraph,
    endpoint: Optional[str] = None,
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> str:
    """
    Render as Mermaid flowchart LR syntax.

    Output:
        ```mermaid
        flowchart LR
            api_post_v1_payments["POST /v1/payments"]
            consumer_billing["billing-service"]
            consumer_billing -->|conf: 0.95| api_post_v1_payments
        ```
    """
    nodes = _filtered_nodes(graph, endpoint, consumer_repo, min_confidence, stale_only)

    if not nodes:
        return "```mermaid\nflowchart LR\n    empty[\"(no data)\"]\n```"

    lines = ["```mermaid", "flowchart LR"]

    # Collect unique repos for node deduplication
    seen_repos: set[str] = set()
    api_ids: dict[str, str] = {}

    for key, node in nodes.items():
        # API node
        api_id = _mermaid_id(f"api_{node.method}_{node.path}")
        api_ids[key] = api_id
        label = f"{node.method.upper()} {node.path}"
        lines.append(f'    {api_id}["{label}"]:::api')

    for key, node in nodes.items():
        api_id = api_ids[key]
        for edge in node.consumers:
            repo_id = _mermaid_id(f"repo_{edge.consumer_repo}")
            if repo_id not in seen_repos:
                seen_repos.add(repo_id)
                lines.append(f'    {repo_id}["{edge.consumer_repo}"]:::consumer')

            conf_label = f"conf: {edge.confidence:.2f}"
            lines.append(f"    {repo_id} -->|{conf_label}| {api_id}")

    # Styles
    lines.append("")
    lines.append("    classDef api fill:#ff6b6b,stroke:#333,color:#fff")
    lines.append("    classDef consumer fill:#4ecdc4,stroke:#333,color:#fff")
    lines.append("```")

    return "\n".join(lines)


def _mermaid_id(raw: str) -> str:
    """Sanitize a string into a valid Mermaid node ID."""
    return "".join(c if c.isalnum() else "_" for c in raw).strip("_")[:60]


# ---------------------------------------------------------------------------
# D3.js JSON rendering
# ---------------------------------------------------------------------------

def render_d3_json(
    graph: ConsumerGraph,
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> dict:
    """
    Produce a D3.js force-directed graph structure.

    Returns:
        {
            "nodes": [{"id": ..., "type": "api"|"consumer", "label": ..., ...}],
            "links": [{"source": ..., "target": ..., "confidence": ..., ...}]
        }
    """
    nodes_map: dict[str, dict] = {}
    links: list[dict] = []

    filtered = _filtered_nodes(graph, None, consumer_repo, min_confidence, stale_only)
    now = time.time()

    for key, node in filtered.items():
        api_id = f"api:{key}"
        if api_id not in nodes_map:
            nodes_map[api_id] = {
                "id": api_id,
                "type": "api",
                "label": f"{node.method.upper()} {node.path}",
                "spec_repo": node.spec_repo,
                "change_count": node.change_count,
                "consumer_count": len(node.consumers),
            }

        for edge in node.consumers:
            consumer_id = f"consumer:{edge.consumer_repo}"
            if consumer_id not in nodes_map:
                nodes_map[consumer_id] = {
                    "id": consumer_id,
                    "type": "consumer",
                    "label": edge.consumer_repo,
                    "language": edge.language,
                }

            days_since = (now - edge.last_seen) / 86400
            links.append({
                "source": consumer_id,
                "target": api_id,
                "confidence": round(edge.confidence, 3),
                "observation_count": edge.observation_count,
                "days_since_seen": round(days_since, 1),
                "stale": days_since > 90,
                "file": edge.consumer_file,
            })

    return {
        "nodes": list(nodes_map.values()),
        "links": links,
    }


# ---------------------------------------------------------------------------
# Stats table
# ---------------------------------------------------------------------------

def render_stats_table(
    graph: ConsumerGraph,
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> str:
    """
    Render a markdown table of endpoints ranked by consumer count.

    | # | Endpoint | Consumers | Avg Confidence | Changes | Stale |
    """
    filtered = _filtered_nodes(graph, None, consumer_repo, min_confidence, stale_only)
    now = time.time()
    stale_threshold = now - (90 * 86400)

    rows = []
    for key, node in filtered.items():
        consumer_count = len(node.consumers)
        avg_conf = (
            sum(e.confidence for e in node.consumers) / consumer_count
            if consumer_count > 0
            else 0.0
        )
        stale_count = sum(1 for e in node.consumers if e.last_seen < stale_threshold)
        rows.append({
            "endpoint": f"{node.method.upper()} {node.path}",
            "consumers": consumer_count,
            "avg_confidence": avg_conf,
            "changes": node.change_count,
            "stale": stale_count,
        })

    # Sort by consumer count descending
    rows.sort(key=lambda r: r["consumers"], reverse=True)

    lines = [
        "| # | Endpoint | Consumers | Avg Confidence | Changes | Stale |",
        "|---|----------|-----------|----------------|---------|-------|",
    ]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"| {i} | `{row['endpoint']}` | {row['consumers']} | "
            f"{row['avg_confidence']:.2f} | {row['changes']} | {row['stale']} |"
        )

    if not rows:
        lines.append("| - | (no endpoints) | 0 | - | 0 | 0 |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML /graph endpoint handler
# ---------------------------------------------------------------------------

_GRAPH_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Ripple &mdash; Dependency Graph</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #c9d1d9; padding: 24px; }
  h1 { color: #58a6ff; margin-bottom: 8px; }
  .stats { color: #8b949e; margin-bottom: 24px; font-size: 14px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
  .tab { padding: 8px 16px; border-radius: 6px; cursor: pointer;
         background: #21262d; border: 1px solid #30363d; color: #c9d1d9; }
  .tab.active { background: #388bfd; color: #fff; border-color: #388bfd; }
  #mermaid-view, #d3-view { display: none; }
  #mermaid-view.active, #d3-view.active { display: block; }
  pre.mermaid { background: #161b22; padding: 16px; border-radius: 8px;
               overflow-x: auto; }
  svg { width: 100%; height: 600px; background: #161b22; border-radius: 8px; }
  .node-api circle { fill: #ff6b6b; }
  .node-consumer circle { fill: #4ecdc4; }
  .link { stroke: #30363d; stroke-opacity: 0.6; }
  .link.stale { stroke: #f85149; stroke-dasharray: 4; }
  text { fill: #c9d1d9; font-size: 11px; }
  .legend { margin-top: 16px; font-size: 12px; color: #8b949e; }
  .legend span { margin-right: 16px; }
  .legend .dot { display: inline-block; width: 10px; height: 10px;
                 border-radius: 50%; margin-right: 4px; vertical-align: middle; }
</style>
</head>
<body>
<h1>Ripple Dependency Graph</h1>
<div class="stats">{stats_line}</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('mermaid')">Mermaid</div>
  <div class="tab" onclick="switchTab('d3')">Force Graph</div>
</div>
<div id="mermaid-view" class="active">
  <pre class="mermaid">{mermaid_raw}</pre>
</div>
<div id="d3-view">
  <svg id="graph-svg"></svg>
</div>
<div class="legend">
  <span><span class="dot" style="background:#ff6b6b"></span>API Endpoint</span>
  <span><span class="dot" style="background:#4ecdc4"></span>Consumer Repo</span>
  <span><span class="dot" style="background:#f85149"></span>Stale (&gt;90d)</span>
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
mermaid.initialize({{ startOnLoad: true, theme: 'dark' }});

const graphData = {d3_json};

function switchTab(tab) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('mermaid-view').classList.remove('active');
  document.getElementById('d3-view').classList.remove('active');
  if (tab === 'mermaid') {{
    document.querySelector('.tab:nth-child(1)').classList.add('active');
    document.getElementById('mermaid-view').classList.add('active');
  }} else {{
    document.querySelector('.tab:nth-child(2)').classList.add('active');
    document.getElementById('d3-view').classList.add('active');
    renderD3();
  }}
}}

let d3Rendered = false;
function renderD3() {{
  if (d3Rendered) return;
  d3Rendered = true;

  const svg = d3.select('#graph-svg');
  const width = svg.node().getBoundingClientRect().width;
  const height = 600;

  const simulation = d3.forceSimulation(graphData.nodes)
    .force('link', d3.forceLink(graphData.links).id(d => d.id).distance(120))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(width / 2, height / 2));

  const link = svg.append('g')
    .selectAll('line')
    .data(graphData.links)
    .join('line')
    .attr('class', d => 'link' + (d.stale ? ' stale' : ''))
    .attr('stroke-width', d => Math.max(1, d.confidence * 4));

  const node = svg.append('g')
    .selectAll('g')
    .data(graphData.nodes)
    .join('g')
    .attr('class', d => 'node-' + d.type)
    .call(d3.drag()
      .on('start', dragstarted)
      .on('drag', dragged)
      .on('end', dragended));

  node.append('circle')
    .attr('r', d => d.type === 'api' ? 12 : 8);

  node.append('text')
    .attr('dx', 14).attr('dy', 4)
    .text(d => d.label);

  simulation.on('tick', () => {{
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
  }});

  function dragstarted(event, d) {{
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
  }}
  function dragged(event, d) {{ d.fx = event.x; d.fy = event.y; }}
  function dragended(event, d) {{
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null; d.fy = null;
  }}
}}
</script>
</body>
</html>"""


def graph_endpoint_handler(
    graph: ConsumerGraph,
    endpoint: Optional[str] = None,
    consumer_repo: Optional[str] = None,
    min_confidence: float = 0.0,
    stale_only: bool = False,
) -> str:
    """
    Return full HTML page with embedded Mermaid + D3 visualization.

    Wire this to your Flask/FastAPI route:
        @app.get("/graph")
        def graph_view(endpoint=None, consumer_repo=None, min_confidence=0.0, stale_only=False):
            return HTMLResponse(graph_endpoint_handler(graph, endpoint, ...))
    """
    stats = graph.stats()
    stats_line = (
        f"{stats['endpoints']} endpoints · {stats['consumer_edges']} edges · "
        f"{stats['repos']} repos · {stats['high_confidence_edges']} high-confidence"
    )

    # Mermaid (strip the ```mermaid fences for inline embedding)
    mermaid_full = render_mermaid(graph, endpoint, consumer_repo, min_confidence, stale_only)
    mermaid_raw = mermaid_full.replace("```mermaid\n", "").replace("\n```", "")

    # D3 JSON
    d3_data = render_d3_json(graph, consumer_repo, min_confidence, stale_only)

    return _GRAPH_HTML_TEMPLATE.format(
        stats_line=html.escape(stats_line),
        mermaid_raw=html.escape(mermaid_raw),
        d3_json=json.dumps(d3_data),
    )
