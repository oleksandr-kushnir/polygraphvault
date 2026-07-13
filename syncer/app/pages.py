"""Static HTML for the human-facing entry point.

The syncer is otherwise a headless JSON API; this module renders the single
``/settings`` landing page that links out to each service's auto-generated
OpenAPI (Swagger) console. Kept separate from routing so the markup is a
plain, independently testable function.
"""

from __future__ import annotations

from html import escape

# Colors are the GitHub Primer palette (light + dark), a widely used,
# WCAG-AA-tested neutral/blue system. All theming flows through CSS custom
# properties defined once in :root and overridden in the dark media query, so
# there is no rule-ordering trap: a single set of var()-based rules styles both
# themes. The RAG docs URL is substituted via str.replace (not .format) so the
# stylesheet's braces need no escaping.
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolyGraphVault — Settings</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f8fa;
    --card-bg: #ffffff;
    --card-border: #d0d7de;
    --shadow: 0 1px 3px rgba(31,35,40,.12), 0 8px 24px rgba(31,35,40,.08);
    --text: #1f2328;
    --text-muted: #59636e;
    --btn-bg: #f6f8fa;
    --btn-bg-hover: #eaeef2;
    --btn-border: #d0d7de;
    --accent: #0969da;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --card-border: #30363d;
      --shadow: none;
      --text: #e6edf3;
      --text-muted: #9198a1;
      --btn-bg: #21262d;
      --btn-bg-hover: #2a3038;
      --btn-border: #3d444d;
      --accent: #4493f8;
    }
  }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: var(--bg); color: var(--text);
  }
  .card {
    background: var(--card-bg); border: 1px solid var(--card-border);
    border-radius: 14px; padding: 2.25rem 2.5rem; box-shadow: var(--shadow);
    max-width: 30rem; width: calc(100% - 2rem);
  }
  h1 { margin: 0 0 .25rem; font-size: 1.4rem; color: var(--text); }
  p.sub { margin: 0 0 .5rem; color: var(--text-muted); font-size: .95rem; }
  p.lead { margin: 0 0 .6rem; color: var(--text-muted); font-size: .9rem; line-height: 1.5; }
  p.lead:last-of-type { margin-bottom: 1.6rem; }
  code { font-family: ui-monospace, SFMono-Regular, "Consolas", monospace;
    font-size: .85em; background: var(--btn-bg); border: 1px solid var(--btn-border);
    border-radius: 5px; padding: .05rem .3rem; }
  .btn {
    display: flex; flex-direction: column; gap: .15rem;
    text-decoration: none; color: var(--text);
    background: var(--btn-bg); border: 1px solid var(--btn-border);
    border-radius: 10px; padding: 1rem 1.2rem; margin-bottom: .9rem;
    transition: background .12s, border-color .12s;
  }
  .btn:hover { background: var(--btn-bg-hover); border-color: var(--accent); }
  .btn strong { font-size: 1.05rem; color: var(--accent); }
  .btn span { color: var(--text-muted); font-size: .85rem; }
</style>
</head>
<body>
<main class="card">
  <h1>PolyGraphVault Settings</h1>
  <p class="sub">Interactive API consoles (OpenAPI / Swagger).</p>
  <p class="lead">PolyGraphVault turns a Nextcloud folder into a queryable knowledge graph
    (<code>Nextcloud&nbsp;&rarr;&nbsp;syncer&nbsp;&rarr;&nbsp;PolyGraphRAG</code>).</p>
  <p class="lead">Use the <strong>Syncer API</strong> to configure and monitor which folders
    sync, and the <strong>PolyGraphRAG API</strong> to query the resulting graphs.</p>
  <p class="lead">Each console has a &ldquo;Try it out&rdquo; button that fires real requests
    &mdash; authorize with your API token first if one is set.</p>
  <a class="btn" href="/docs">
    <strong>Syncer API</strong>
    <span>Manage folder&nbsp;&rarr;&nbsp;workspace mappings (CRUD, run, events)</span>
  </a>
  <a class="btn" href="__RAG_DOCS_URL__">
    <strong>PolyGraphRAG API</strong>
    <span>Query and inspect the knowledge graph</span>
  </a>
</main>
</body>
</html>
"""


def render_settings_page(rag_docs_url: str) -> str:
    """Return the standalone HTML for ``GET /settings``.

    ``rag_docs_url`` is the *browser-facing* PolyGraphRAG docs URL (the syncer's
    internal Docker address is not reachable from a browser). The syncer's own
    Swagger link is same-origin, so it stays the relative ``/docs``.
    """
    return _TEMPLATE.replace("__RAG_DOCS_URL__", escape(rag_docs_url, quote=True))
