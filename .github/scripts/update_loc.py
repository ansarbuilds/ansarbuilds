"""
Count lines of code written across all owned GitHub repositories (public + private),
break them down across the Top 5 languages/stacks, generate assets/code-telemetry.svg,
and update README.md.

Runs automatically via .github/workflows/update-loc.yml whenever commits are pushed.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README_PATH = ROOT / "README.md"
SVG_PATH = ROOT / "assets" / "code-telemetry.svg"
MAX_SINGLE_COMMIT_LINES = 250_000  # skip raw dataset dumps (e.g. multi-million-line CSV imports)

# --- palette (matches assets/generate_banner.py & ansarbuilds.dev) -----------
PAPER = "#efe2ce"
IVORY = "#e6d5bd"
DEEP_IVORY = "#dcc9ae"
CREAM = "#f6ece0"
INK = "#101010"
CARD_INK = "#161311"
CORAL = "#d5360c"
CORAL_LIGHT = "#e85d35"
CORAL_INK = "#74200d"
MUTED = "#51463c"
MUTED_LIGHT = "#7a6a5a"


def get_token() -> str:
    for key in ("LOC_TOKEN", "GH_PAT", "GITHUB_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return subprocess.check_output(["gh", "auth", "token"]).decode().strip()


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ansarbuilds-loc-updater",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if "errors" in payload and not payload.get("data"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload


REPOS_QUERY = """
query($cursor: String) {
  viewer {
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes { name owner { login } }
    }
  }
}
"""

BATCH_FRAGMENT = """
fragment Hist on Repository {
  name
  owner { login }
  languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
    edges { size node { name } }
  }
  defaultBranchRef {
    target {
      ... on Commit {
        history(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes {
            additions
            parents { totalCount }
            author { name }
          }
        }
      }
    }
  }
}
"""

PAGE_QUERY = """
query($owner: String!, $name: String!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              additions
              parents { totalCount }
              author { name }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_all_repos(token: str) -> list[dict]:
    repos = []
    cursor = None
    while True:
        data = gql(token, REPOS_QUERY, {"cursor": cursor})["data"]["viewer"]["repositories"]
        repos.extend(data["nodes"])
        if data["pageInfo"]["hasNextPage"]:
            cursor = data["pageInfo"]["endCursor"]
        else:
            break
    return repos


STACK_CONFIG = {
    "Python": {
        "title": "PYTHON",
        "tools": ["PyTorch", "DuckDB", "MCP", "Agents"],
        "accent": CORAL,
        "icon": "python",
    },
    "TypeScript & JS": {
        "title": "TYPESCRIPT & JS",
        "tools": ["React", "Vite", "Workers", "Node"],
        "accent": "#b8320e",
        "icon": "ts",
    },
    "Swift": {
        "title": "SWIFT",
        "tools": ["SwiftUI", "macOS", "iOS", "Metal"],
        "accent": "#94290d",
        "icon": "swift",
    },
    "Rust & Tauri": {
        "title": "RUST & TAURI",
        "tools": ["Tauri", "Cargo", "Desktop", "CLI"],
        "accent": CORAL_INK,
        "icon": "rust",
    },
    "C, SQL & Native": {
        "title": "C, SQL & NATIVE",
        "tools": ["ARM64", "SQL", "Snowflake", "Shell"],
        "accent": "#4a382b",
        "icon": "systems",
    },
}


def classify_language(lang: str) -> str:
    if lang in ("Python", "Jupyter Notebook"):
        return "Python"
    if lang in ("TypeScript", "JavaScript"):
        return "TypeScript & JS"
    if lang in ("Swift", "Objective-C", "Objective-C++"):
        return "Swift"
    if lang == "Rust":
        return "Rust & Tauri"
    if lang in ("C", "C++", "CMake", "Shell", "SQL", "PLpgSQL", "Dart", "Kotlin", "Go", "Java", "Ruby", "Dockerfile"):
        return "C, SQL & Native"
    return "Markup & Styling"


def collect_telemetry(token: str) -> dict:
    repos = fetch_all_repos(token)

    def fetch_batch(batch: list[dict]) -> list[dict]:
        aliases = []
        for idx, r in enumerate(batch):
            owner_json = json.dumps(r["owner"]["login"])
            name_json = json.dumps(r["name"])
            aliases.append(f"r{idx}: repository(owner: {owner_json}, name: {name_json}) {{ ...Hist }}")
        query = "query { " + " ".join(aliases) + " }\n" + BATCH_FRAGMENT
        return list(gql(token, query)["data"].values())

    batches = [repos[i : i + 8] for i in range(0, len(repos), 8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        batch_results = list(pool.map(fetch_batch, batches))

    total_lines = 0
    total_commits = 0
    raw_bucket_lines: dict[str, float] = {k: 0.0 for k in STACK_CONFIG}
    raw_bucket_lines["Markup & Styling"] = 0.0

    for batch in batch_results:
        for repo in batch:
            if not repo or not repo.get("defaultBranchRef") or not repo["defaultBranchRef"].get("target"):
                continue
            hist = repo["defaultBranchRef"]["target"]["history"]
            commits = list(hist["nodes"])
            cursor = hist["pageInfo"]["endCursor"] if hist["pageInfo"]["hasNextPage"] else None
            while cursor:
                page = gql(
                    token,
                    PAGE_QUERY,
                    {"owner": repo["owner"]["login"], "name": repo["name"], "cursor": cursor},
                )
                hist2 = page["data"]["repository"]["defaultBranchRef"]["target"]["history"]
                commits.extend(hist2["nodes"])
                cursor = hist2["pageInfo"]["endCursor"] if hist2["pageInfo"]["hasNextPage"] else None

            repo_add = 0
            for c in commits:
                author_name = ((c.get("author") or {}).get("name") or "").lower()
                if "github-actions" in author_name or "[bot]" in author_name:
                    continue
                if c["parents"]["totalCount"] > 1:
                    continue
                additions = c.get("additions") or 0
                if additions > MAX_SINGLE_COMMIT_LINES:
                    continue
                repo_add += additions
                total_commits += 1

            total_lines += repo_add
            if repo_add == 0:
                continue

            edges = (repo.get("languages") or {}).get("edges") or []
            if not edges:
                raw_bucket_lines["Python"] += repo_add
                continue

            weights: dict[str, float] = {}
            for e in edges:
                lname = e["node"]["name"]
                sz = float(e["size"])
                if lname == "Jupyter Notebook":
                    sz *= 0.15  # discount embedded base64 plot blobs in .ipynb
                bucket = classify_language(lname)
                weights[bucket] = weights.get(bucket, 0.0) + max(sz, 1.0)

            tot_w = sum(weights.values())
            for bucket, w in weights.items():
                raw_bucket_lines[bucket] = raw_bucket_lines.get(bucket, 0.0) + repo_add * (w / tot_w)

    # Fold Markup & Styling proportionally or keep top 5 exact so top 5 + markup = total_lines
    top5_keys = list(STACK_CONFIG.keys())
    top5_list = []
    for key in top5_keys:
        cfg = STACK_CONFIG[key]
        lines_val = int(round(raw_bucket_lines.get(key, 0.0)))
        top5_list.append(
            {
                "key": key,
                "title": cfg["title"],
                "tools": cfg["tools"],
                "accent": cfg["accent"],
                "icon": cfg["icon"],
                "lines": lines_val,
            }
        )

    top5_list.sort(key=lambda x: -x["lines"])
    markup_lines = max(0, total_lines - sum(item["lines"] for item in top5_list))

    return {
        "total_lines": total_lines,
        "total_commits": total_commits,
        "repo_count": len(repos),
        "top5": top5_list,
        "markup_lines": markup_lines,
        "updated_date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
    }


def render_icon(kind: str, cx: float, cy: float) -> str:
    """Crisp vector emblem centered at (cx, cy) inside a 38x38 dark ink badge."""
    x0, y0 = cx - 19, cy - 19
    box = f'<rect x="{x0}" y="{y0}" width="38" height="38" rx="7" fill="{INK}"/>'
    if kind == "python":
        # Stylized Python dual-interlock emblem
        return (
            box
            + f'<g transform="translate({cx - 11},{cy - 11})">'
            f'<path d="M10.5 1 C6 1 5.5 3 5.5 5.5 L5.5 8 L11 8 L11 9.2 L3.8 9.2 C1.5 9.2 0.5 10.8 0.5 14 C0.5 17.2 1.5 18.8 3.8 18.8 L5.5 18.8 L5.5 16.2 C5.5 14 7 12.6 9.2 12.6 L14.8 12.6 C16.5 12.6 17.8 11.3 17.8 9.6 L17.8 5.5 C17.8 2.8 15.5 1 10.5 1 Z" fill="{CORAL}"/>'
            f'<path d="M11.5 21 C16 21 16.5 19 16.5 16.5 L16.5 14 L11 14 L11 12.8 L18.2 12.8 C20.5 12.8 21.5 11.2 21.5 8 C21.5 4.8 20.5 3.2 18.2 3.2 L16.5 3.2 L16.5 5.8 C16.5 8 15 9.4 12.8 9.4 L7.2 9.4 C5.5 9.4 4.2 10.7 4.2 12.4 L4.2 16.5 C4.2 19.2 6.5 21 11.5 21 Z" fill="{IVORY}"/>'
            f'<circle cx="8.2" cy="4.3" r="1.1" fill="{INK}"/>'
            f'<circle cx="13.8" cy="17.7" r="1.1" fill="{INK}"/>'
            f"</g>"
        )
    if kind == "ts":
        # React orbit + TS monogram
        return (
            box
            + f'<g transform="translate({cx},{cy})">'
            f'<ellipse rx="12" ry="4.6" fill="none" stroke="{CORAL}" stroke-width="1.3" transform="rotate(30)"/>'
            f'<ellipse rx="12" ry="4.6" fill="none" stroke="{IVORY}" stroke-width="1.3" stroke-opacity="0.75" transform="rotate(-30)"/>'
            f'<circle r="2.2" fill="{CORAL}"/>'
            f"</g>"
        )
    if kind == "swift":
        # Swift soaring bird silhouette
        return (
            box
            + f'<g transform="translate({cx - 11},{cy - 11})">'
            f'<path d="M19.8 15.2 C20.7 13.3 20.6 10.7 19.1 8.2 C17.7 5.8 15.1 4.1 12.8 3.2 C14.6 5.4 15.7 7.8 15.8 9.9 C13.1 7.6 9.7 5.3 5.8 3.2 C7.3 5.4 9.3 7.8 11.5 9.8 C8.8 8.3 5.7 6.3 3.2 4.3 C4.6 7.0 7.2 10.1 10.4 12.2 C7.8 12.7 4.8 12.3 2.0 10.8 C4.3 14.3 8.3 16.7 12.9 16.8 C15.2 16.8 17.3 16.1 18.8 15.1 C19.6 15.8 20.5 16.7 21.0 17.6 C21.2 16.6 20.6 15.7 19.8 15.2 Z" fill="{CORAL}"/>'
            f"</g>"
        )
    if kind == "rust":
        # Rust gear cog + Tauri core
        teeth = []
        for deg in range(0, 360, 45):
            teeth.append(
                f'<rect x="-1.6" y="-12.5" width="3.2" height="3.5" rx="0.8" fill="{IVORY}" transform="rotate({deg})"/>'
            )
        return (
            box
            + f'<g transform="translate({cx},{cy})">'
            + "".join(teeth)
            + f'<circle r="9.2" fill="none" stroke="{IVORY}" stroke-width="2.4"/>'
            f'<circle r="3.6" fill="{CORAL}"/>'
            f"</g>"
        )
    # systems: terminal prompt `>_` + brackets
    return (
        box
        + f'<g transform="translate({cx - 10},{cy - 7})" fill="none" stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="M1 2 L8 7.5 L1 13" stroke="{CORAL}" stroke-width="2.2"/>'
        f'<line x1="11" y1="13" x2="19" y2="13" stroke="{IVORY}" stroke-width="2.2"/>'
        f"</g>"
    )


def generate_svg(data: dict) -> str:
    W, H = 1200, 446
    total_lines = data["total_lines"]
    total_commits = data["total_commits"]
    repo_count = data["repo_count"]
    top5 = data["top5"]
    markup_lines = data["markup_lines"]
    max_lines = max(item["lines"] for item in top5) if top5 else 1

    # Ruled drafting sheet lines
    rules = []
    for r in range(11):
        y = 48 + r * 36
        phase = (r * 0.9) % 9
        rules.append(
            f'<line x1="0" x2="{W}" y1="{y}" y2="{y}" stroke="{INK}" stroke-opacity="0.07" stroke-width="1">'
            f'<animate attributeName="stroke-opacity" dur="9s" begin="-{phase:.2f}s" '
            f'repeatCount="indefinite" values="0.04;0.12;0.04"/></line>'
        )

    # Stacked spectrum bar on the left panel (x=56..436, width=380)
    spec_x, spec_y, spec_w, spec_h = 56, 336, 380, 12
    spectrum_rects = []
    cur_x = float(spec_x)
    denom = max(total_lines, 1)
    all_segments = [
        (item["lines"], item["accent"]) for item in top5
    ] + ([(markup_lines, "#b9a68e")] if markup_lines > 0 else [])

    for seg_lines, color in all_segments:
        w = max(4.0, spec_w * (seg_lines / denom))
        if cur_x + w > spec_x + spec_w:
            w = max(0.0, (spec_x + spec_w) - cur_x)
        if w > 0:
            spectrum_rects.append(
                f'<rect x="{cur_x:.1f}" y="{spec_y}" width="{w:.1f}" height="{spec_h}" fill="{color}"/>'
            )
        cur_x += w + 1.5

    # Top 5 rows on the right panel (x=482..1146)
    row_svg = []
    row_x = 482
    row_w = 664
    row_h = 62
    start_y = 66
    gap_y = 70
    bar_x = row_x + 390
    bar_max_w = 248

    for idx, item in enumerate(top5):
        ry = start_y + idx * gap_y
        pct = (item["lines"] / denom) * 100.0
        target_w = max(14, int(round(bar_max_w * (item["lines"] / max_lines))))
        lines_fmt = f"{item['lines']:,}"
        pct_fmt = f"{pct:.1f}%"
        title_esc = html.escape(item["title"])

        # Tool pills
        pill_x = row_x + 66
        pill_y = ry + 34
        pills = []
        for tool in item["tools"]:
            tool_esc = html.escape(tool)
            pw = 16 + int(len(tool) * 6.8)
            pills.append(
                f'<rect x="{pill_x}" y="{pill_y}" width="{pw}" height="18" rx="3" '
                f'fill="{IVORY}" stroke="{MUTED}" stroke-opacity="0.35"/>'
                f'<text x="{pill_x + 8}" y="{pill_y + 12.5}" class="pill">{tool_esc}</text>'
            )
            pill_x += pw + 6

        icon_cx = row_x + 33
        icon_cy = ry + 31
        phase = idx * 0.6

        row_svg.append(
            f'<g>'
            # Card background
            f'<rect x="{row_x}" y="{ry}" width="{row_w}" height="{row_h}" rx="7" '
            f'fill="{CREAM}" fill-opacity="0.78" stroke="{INK}" stroke-opacity="0.16"/>'
            # Left rank accent stripe
            f'<rect x="{row_x}" y="{ry + 10}" width="4" height="{row_h - 20}" rx="2" fill="{item["accent"]}"/>'
            # Vector icon badge
            + render_icon(item["icon"], icon_cx, icon_cy)
            # Language title
            + f'<text x="{row_x + 66}" y="{ry + 25}" class="lang-title">{title_esc}</text>'
            + "".join(pills)
            # Right side numbers: exact lines & share percentage
            + f'<text x="{bar_x + bar_max_w}" y="{ry + 25}" text-anchor="end" class="row-num">'
            f'{lines_fmt} <tspan class="row-unit">LOC</tspan> · <tspan fill="{CORAL_INK}">{pct_fmt}</tspan></text>'
            # Progress track & live fill bar
            + f'<rect x="{bar_x}" y="{ry + 38}" width="{bar_max_w}" height="8" rx="4" fill="{DEEP_IVORY}"/>'
            f'<rect x="{bar_x}" y="{ry + 38}" width="{target_w}" height="8" rx="4" fill="{item["accent"]}">'
            f'<animate attributeName="fill-opacity" values="0.82;1;0.82" dur="3.6s" begin="-{phase:.1f}s" repeatCount="indefinite"/>'
            f'</rect>'
            f'</g>'
        )

    total_fmt = f"{total_lines:,}"
    commits_fmt = f"{total_commits:,}"
    markup_fmt = f"{markup_lines:,}"

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Codebase telemetry: {total_fmt} lines of code written across {repo_count} GitHub repositories.">
<title>Codebase Telemetry — {total_fmt} Lines of Code</title>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0.7" y2="1">
    <stop offset="0" stop-color="{PAPER}"/>
    <stop offset="0.6" stop-color="{IVORY}"/>
    <stop offset="1" stop-color="{DEEP_IVORY}"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.22" cy="0.45" r="0.45">
    <stop offset="0" stop-color="{CORAL}" stop-opacity="0.13"/>
    <stop offset="0.7" stop-color="{CORAL}" stop-opacity="0.03"/>
    <stop offset="1" stop-color="{CORAL}" stop-opacity="0"/>
  </radialGradient>
  <clipPath id="card"><rect width="{W}" height="{H}" rx="6"/></clipPath>
  <clipPath id="specClip"><rect x="{spec_x}" y="{spec_y}" width="{spec_w}" height="{spec_h}" rx="4"/></clipPath>
  <style>
    .display {{ font-family: "Anton", "Arial Narrow", "Haettenschweiler", Impact, sans-serif; }}
    .eyebrow {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; fill: {MUTED}; letter-spacing: 3px; font-weight: 600; }}
    .sub {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; fill: {CORAL_INK}; letter-spacing: 1.5px; font-weight: 600; }}
    .stat-val {{ font-family: "Anton", "Arial Narrow", "Haettenschweiler", Impact, sans-serif; font-size: 28px; fill: {IVORY}; letter-spacing: 1px; }}
    .stat-lbl {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; fill: #b9a68e; letter-spacing: 1.5px; }}
    .lang-title {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; fill: {INK}; font-weight: 700; letter-spacing: 1px; }}
    .pill {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 10.5px; fill: {MUTED}; }}
    .row-num {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 13.5px; fill: {INK}; font-weight: 700; }}
    .row-unit {{ font-size: 11px; fill: {MUTED}; font-weight: 500; }}
    .foot {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px; fill: {MUTED}; }}
  </style>
</defs>

<g clip-path="url(#card)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#glow)"/>
  <g>{"".join(rules)}</g>

  <!-- Vertical architectural divider -->
  <line x1="458" y1="36" x2="458" y2="{H - 36}" stroke="{INK}" stroke-opacity="0.15" stroke-dasharray="4 4"/>

  <!-- LEFT PANEL: TOTAL TELEMETRY -->
  <g>
    <!-- Live pulsing beacon -->
    <circle cx="64" cy="44" r="4" fill="{CORAL}"/>
    <circle cx="64" cy="44" r="4" fill="none" stroke="{CORAL}" stroke-width="1.5">
      <animate attributeName="r" values="4;11;4" dur="2.6s" repeatCount="indefinite"/>
      <animate attributeName="stroke-opacity" values="0.9;0;0.9" dur="2.6s" repeatCount="indefinite"/>
    </circle>
    <text x="78" y="48" class="eyebrow">CODEBASE TELEMETRY · LIVE ON PUSH</text>
    <line x1="56" y1="62" x2="436" y2="62" stroke="{INK}" stroke-opacity="0.18"/>

    <!-- Big total lines of code display -->
    <text x="54" y="164" class="display" font-size="92" fill="{INK}" letter-spacing="1.5">{total_fmt}</text>
    <rect x="56" y="177" width="310" height="6" rx="2" fill="{CORAL}">
      <animate attributeName="width" dur="9s" repeatCount="indefinite" values="60;310;310;60" keyTimes="0;0.22;0.82;1" calcMode="spline" keySplines=".22 1 .36 1;0 0 1 1;.22 1 .36 1"/>
    </rect>
    <text x="56" y="205" class="sub">&gt; TOTAL LINES OF CODE WRITTEN</text>

    <!-- 3 dark architectural stat cards -->
    <g transform="translate(56, 226)">
      <rect x="0" y="0" width="118" height="72" rx="6" fill="{CARD_INK}"/>
      <text x="16" y="38" class="stat-val">{repo_count}</text>
      <text x="16" y="56" class="stat-lbl">REPOSITORIES</text>

      <rect x="131" y="0" width="128" height="72" rx="6" fill="{CARD_INK}"/>
      <text x="147" y="38" class="stat-val">{commits_fmt}</text>
      <text x="147" y="56" class="stat-lbl">COMMITS PUSHED</text>

      <rect x="272" y="0" width="108" height="72" rx="6" fill="{CARD_INK}"/>
      <text x="288" y="38" class="stat-val" fill="{CORAL}">TOP 5</text>
      <text x="288" y="56" class="stat-lbl">CORE STACKS</text>
    </g>

    <!-- Spectrum distribution bar -->
    <text x="56" y="325" class="foot" font-weight="600">STACK DISTRIBUTION SPECTRUM</text>
    <g clip-path="url(#specClip)">
      <rect x="{spec_x}" y="{spec_y}" width="{spec_w}" height="{spec_h}" fill="{DEEP_IVORY}"/>
      {"".join(spectrum_rects)}
    </g>

    <!-- Markup & config accounting line so numbers sum 100% transparently -->
    <text x="56" y="376" class="foot">+ {markup_fmt} LOC in HTML, CSS &amp; declarative templates</text>
    <text x="56" y="400" class="foot" fill="{CORAL_INK}">● Auto-recalculated on git push · synced {data["updated_date"]}</text>
  </g>

  <!-- RIGHT PANEL: TOP 5 LANGUAGES & TOOLS -->
  <g>
    <text x="{row_x}" y="48" class="eyebrow">TOP 5 LANGUAGES &amp; ENGINEERING STACK</text>
    <text x="{row_x + row_w}" y="48" text-anchor="end" class="foot">LINES WRITTEN · SHARE</text>
    {"".join(row_svg)}
  </g>
</g>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="6" fill="none" stroke="{INK}" stroke-opacity="0.25"/>
</svg>
'''
    return svg


def build_readme_block(data: dict) -> str:
    total_fmt = f"{data['total_lines']:,}"
    top_summary = ", ".join(f"{item['title'].title()} ({item['lines']:,})" for item in data["top5"])
    return (
        f'<!-- loc-start -->\n'
        f'<p align="center">\n'
        f'  <img src="./assets/code-telemetry.svg" width="100%" '
        f'alt="Codebase Telemetry: {total_fmt} total lines of code written — {top_summary}" />\n'
        f'</p>\n'
        f'<!-- loc-end -->'
    )


def update_files(data: dict) -> bool:
    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_svg = generate_svg(data)
    old_svg = SVG_PATH.read_text(encoding="utf-8") if SVG_PATH.exists() else ""
    svg_changed = new_svg != old_svg
    if svg_changed:
        SVG_PATH.write_text(new_svg, encoding="utf-8")
        print(f"Wrote {SVG_PATH} ({len(new_svg) / 1024:.1f} KB)")

    content = README_PATH.read_text(encoding="utf-8")
    block = build_readme_block(data)
    pattern = re.compile(r"<!-- loc-start -->.*?<!-- loc-end -->", re.DOTALL)
    if pattern.search(content):
        updated = pattern.sub(block, content)
    else:
        raise RuntimeError("Could not find <!-- loc-start -->...<!-- loc-end --> marker in README.md")

    readme_changed = updated != content
    if readme_changed:
        README_PATH.write_text(updated, encoding="utf-8")
        print(f"Updated README.md ({data['total_lines']:,} total lines of code).")

    return svg_changed or readme_changed


if __name__ == "__main__":
    tok = get_token()
    telemetry = collect_telemetry(tok)
    print(
        f"Counted {telemetry['total_lines']:,} lines across "
        f"{telemetry['total_commits']:,} commits in {telemetry['repo_count']} repos."
    )
    for item in telemetry["top5"]:
        print(f"  {item['title']:18s}: {item['lines']:9,d} LOC ({', '.join(item['tools'])})")
    update_files(telemetry)
