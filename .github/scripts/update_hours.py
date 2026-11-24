"""
Estimate the hours spent building across all owned GitHub repositories
(public + private) from commit times, draw assets/build-hours.svg, and
update README.md.

GitHub does not record time spent, so the hours are an estimate from when
commits were made, the usual way (as in the git-hours tool):

  - every commit by me is put on one timeline, across all repositories;
  - two commits less than SESSION_GAP apart are one working session, and
    the time between them counts;
  - the first commit of a session also counts FIRST_COMMIT, for the work
    done before it.

Runs with update_loc.py in .github/workflows/update-loc.yml.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from update_loc import (
    CARD_INK, CORAL, CORAL_INK, CORAL_LIGHT, CREAM, DEEP_IVORY, INK, IVORY, MUTED, PAPER,
    fetch_all_repos, get_token, gql,
)

ROOT = Path(__file__).resolve().parents[2]
README_PATH = ROOT / "README.md"
SVG_PATH = ROOT / "assets" / "build-hours.svg"

SESSION_GAP = dt.timedelta(hours=2)
FIRST_COMMIT = dt.timedelta(minutes=30)
# GitHub gives commit times in UTC; the clock and the months are in my own time.
HOME = ZoneInfo("America/Toronto")

HISTORY = """
fragment Times on Repository {
  defaultBranchRef {
    target {
      ... on Commit {
        history(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { authoredDate parents { totalCount } author { name } }
        }
      }
    }
  }
}
"""

PAGE = """
query($owner: String!, $name: String!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { authoredDate parents { totalCount } author { name } }
          }
        }
      }
    }
  }
}
"""


def mine(node: dict) -> bool:
    name = ((node.get("author") or {}).get("name") or "").lower()
    return "github-actions" not in name and "[bot]" not in name and node["parents"]["totalCount"] <= 1


def collect_times(token: str) -> tuple[list[dt.datetime], int]:
    repos = fetch_all_repos(token)

    def one(repo: dict) -> list[dt.datetime]:
        owner, name = repo["owner"]["login"], repo["name"]
        query = f"query {{ r: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ ...Times }} }}\n" + HISTORY
        data = gql(token, query)["data"]["r"]
        if not data or not data.get("defaultBranchRef") or not data["defaultBranchRef"].get("target"):
            return []
        hist = data["defaultBranchRef"]["target"]["history"]
        nodes = list(hist["nodes"])
        cursor = hist["pageInfo"]["endCursor"] if hist["pageInfo"]["hasNextPage"] else None
        while cursor:
            page = gql(token, PAGE, {"owner": owner, "name": name, "cursor": cursor})
            hist = page["data"]["repository"]["defaultBranchRef"]["target"]["history"]
            nodes.extend(hist["nodes"])
            cursor = hist["pageInfo"]["endCursor"] if hist["pageInfo"]["hasNextPage"] else None
        return [dt.datetime.fromisoformat(n["authoredDate"].replace("Z", "+00:00")).astimezone(HOME) for n in nodes if mine(n)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        times = [t for chunk in pool.map(one, repos) for t in chunk]
    return sorted(set(times)), len(repos)


def estimate(times: list[dt.datetime]) -> dict:
    total = dt.timedelta()
    sessions = 0
    by_month: dict[str, float] = {}
    previous = None
    for t in times:
        if previous is None or t - previous >= SESSION_GAP:
            spent = FIRST_COMMIT
            sessions += 1
        else:
            spent = t - previous
        total += spent
        key = t.strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0.0) + spent.total_seconds() / 3600
        previous = t

    # When in the day the commits land.
    by_hour = [0] * 24
    for t in times:
        by_hour[t.hour] += 1

    days = sorted({t.date() for t in times})
    streak = best = 0
    for i, day in enumerate(days):
        streak = streak + 1 if i and (day - days[i - 1]).days == 1 else 1
        best = max(best, streak)

    hours = total.total_seconds() / 3600
    # The last twelve calendar months, empty ones included.
    now = dt.datetime.now(HOME)
    months = []
    for back in range(11, -1, -1):
        y, m = divmod(now.year * 12 + now.month - 1 - back, 12)
        months.append(f"{y}-{m + 1:02d}")
    return {
        "hours": hours,
        "sessions": sessions,
        "commits": len(times),
        "days": len(days),
        "streak": best,
        "since": times[0].strftime("%b %Y") if times else "",
        "months": [(m, by_month.get(m, 0.0)) for m in months],
        "by_hour": by_hour,
        "updated_date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
    }


def floor_to(value: float) -> int:
    """A round number below the estimate: 1,284 becomes 1,250, 287 becomes 280."""
    step = 50 if value >= 1000 else 10
    return int(value // step * step)


def generate_svg(d: dict) -> str:
    W, H = 1200, 360
    shown = floor_to(d["hours"])
    shown_fmt = f"{shown:,}+"

    # The clock: a ring that fills, a hand that sweeps, ticks for the hours.
    cx, cy, r = 196, 178, 112
    ticks = []
    for h in range(24):
        a = math.radians(h * 15 - 90)
        inner = r - (14 if h % 6 == 0 else 8)
        ticks.append(
            f'<line x1="{cx + inner * math.cos(a):.1f}" y1="{cy + inner * math.sin(a):.1f}" '
            f'x2="{cx + (r - 2) * math.cos(a):.1f}" y2="{cy + (r - 2) * math.sin(a):.1f}" '
            f'stroke="{INK}" stroke-opacity="{0.5 if h % 6 == 0 else 0.22}" stroke-width="{2 if h % 6 == 0 else 1.2}"/>'
        )

    # When I build: 24 petals round the clock face, one per hour of the day,
    # as long as the share of commits made in that hour.
    peak = max(d["by_hour"]) or 1
    petals = []
    for h, count in enumerate(d["by_hour"]):
        if not count:
            continue
        a = math.radians(h * 15 - 90)
        length = 14 + 62 * count / peak
        x2, y2 = cx + length * math.cos(a), cy + length * math.sin(a)
        delay = 0.6 + h * 0.05
        petals.append(
            f'<line x1="{cx}" y1="{cy}" x2="{cx}" y2="{cy}" stroke="{CORAL if count >= peak * 0.6 else CORAL_LIGHT}" '
            f'stroke-opacity="{0.45 + 0.55 * count / peak:.2f}" stroke-width="6" stroke-linecap="round">'
            f'<animate attributeName="x2" from="{cx}" to="{x2:.1f}" begin="{delay:.2f}s" dur="0.7s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1"/>'
            f'<animate attributeName="y2" from="{cy}" to="{y2:.1f}" begin="{delay:.2f}s" dur="0.7s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1"/>'
            f'</line>'
        )
    dial = "".join(
        f'<text x="{cx + (r - 26) * math.cos(math.radians(h * 15 - 90)):.1f}" y="{cy + (r - 26) * math.sin(math.radians(h * 15 - 90)) + 4:.1f}" '
        f'text-anchor="middle" class="dial">{label}</text>'
        for h, label in ((0, "12AM"), (6, "6AM"), (12, "12PM"), (18, "6PM"))
    )
    busiest = max(range(24), key=lambda h: d["by_hour"][h])
    busiest_fmt = dt.time(busiest).strftime("%-I %p").lower()

    # The last twelve months, as bars that rise.
    months = d["months"]
    top = max((v for _, v in months), default=1) or 1
    bars = []
    bx, by, bw, bh, gap = 850, 268, 22, 150, 4
    for i, (month, value) in enumerate(months):
        height = max(3.0, bh * value / top)
        x = bx + i * (bw + gap)
        label = dt.datetime.strptime(month, "%Y-%m").strftime("%b")[0]
        delay = 0.4 + i * 0.08
        bars.append(
            f'<rect x="{x}" y="{by}" width="{bw}" height="0" rx="3" fill="{CORAL if i == len(months) - 1 else INK}" fill-opacity="{1 if i == len(months) - 1 else 0.82}">'
            f'<animate attributeName="height" from="0" to="{height:.1f}" begin="{delay:.2f}s" dur="0.9s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1"/>'
            f'<animate attributeName="y" from="{by}" to="{by - height:.1f}" begin="{delay:.2f}s" dur="0.9s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1"/>'
            f'</rect>'
            f'<text x="{x + bw / 2}" y="{by + 18}" text-anchor="middle" class="foot">{label}</text>'
        )

    circumference = 2 * math.pi * (r + 12)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Build hours: about {shown_fmt} hours spent building across GitHub repositories since {d["since"]}, estimated from commit times.">
<title>Build Hours: {shown_fmt} hours</title>
<defs>
  <linearGradient id="hbg" x1="0" y1="0" x2="0.7" y2="1">
    <stop offset="0" stop-color="{PAPER}"/>
    <stop offset="0.6" stop-color="{IVORY}"/>
    <stop offset="1" stop-color="{DEEP_IVORY}"/>
  </linearGradient>
  <radialGradient id="hglow" cx="0.16" cy="0.5" r="0.4">
    <stop offset="0" stop-color="{CORAL}" stop-opacity="0.16"/>
    <stop offset="1" stop-color="{CORAL}" stop-opacity="0"/>
  </radialGradient>
  <clipPath id="hcard"><rect width="{W}" height="{H}" rx="6"/></clipPath>
  <style>
    .display {{ font-family: "Anton", "Arial Narrow", "Haettenschweiler", Impact, sans-serif; }}
    .eyebrow {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; fill: {MUTED}; letter-spacing: 3px; font-weight: 600; }}
    .sub {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; fill: {CORAL_INK}; letter-spacing: 1.5px; font-weight: 600; }}
    .stat-val {{ font-family: "Anton", "Arial Narrow", "Haettenschweiler", Impact, sans-serif; font-size: 28px; fill: {IVORY}; letter-spacing: 1px; }}
    .stat-lbl {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; fill: #b9a68e; letter-spacing: 1.5px; }}
    .foot {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px; fill: {MUTED}; }}
    .dial {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 9px; fill: {MUTED}; letter-spacing: 1px; font-weight: 600; }}
    .rise {{ opacity: 0; animation: rise .9s cubic-bezier(.2,.8,.2,1) forwards; }}
    @keyframes rise {{ from {{ opacity: 0; transform: translateY(14px); }} to {{ opacity: 1; transform: none; }} }}
  </style>
</defs>

<g clip-path="url(#hcard)">
  <rect width="{W}" height="{H}" fill="url(#hbg)"/>
  <rect width="{W}" height="{H}" fill="url(#hglow)"/>

  <!-- The clock -->
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="{CREAM}" fill-opacity="0.7" stroke="{INK}" stroke-opacity="0.2"/>
  {"".join(ticks)}
  <circle cx="{cx}" cy="{cy}" r="{r + 12}" fill="none" stroke="{DEEP_IVORY}" stroke-width="6"/>
  <circle cx="{cx}" cy="{cy}" r="{r + 12}" fill="none" stroke="{CORAL}" stroke-width="6" stroke-linecap="round"
    stroke-dasharray="{circumference:.1f}" stroke-dashoffset="{circumference:.1f}" transform="rotate(-90 {cx} {cy})">
    <animate attributeName="stroke-dashoffset" from="{circumference:.1f}" to="0" dur="2.4s" fill="freeze" calcMode="spline" keySplines=".3 0 .2 1"/>
  </circle>
  {dial}
  <g>{"".join(petals)}</g>
  <line x1="{cx}" y1="{cy}" x2="{cx}" y2="{cy - r + 18}" stroke="{INK}" stroke-width="3" stroke-linecap="round">
    <animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="360 {cx} {cy}" dur="12s" repeatCount="indefinite"/>
  </line>
  <circle cx="{cx}" cy="{cy}" r="6" fill="{INK}"/>
  <circle cx="{cx}" cy="{cy}" r="2.5" fill="{CORAL}"/>
  <text x="{cx}" y="{cy + r + 42}" text-anchor="middle" class="foot">WHEN I BUILD · BUSIEST AT {busiest_fmt.upper()}</text>

  <!-- The number -->
  <g class="rise" style="animation-delay:.2s">
    <circle cx="396" cy="52" r="4" fill="{CORAL}"/>
    <circle cx="396" cy="52" r="4" fill="none" stroke="{CORAL}" stroke-width="1.5">
      <animate attributeName="r" values="4;11;4" dur="2.6s" repeatCount="indefinite"/>
      <animate attributeName="stroke-opacity" values="0.9;0;0.9" dur="2.6s" repeatCount="indefinite"/>
    </circle>
    <text x="410" y="56" class="eyebrow">BUILD HOURS · SINCE {d["since"].upper()}</text>
    <text x="386" y="168" class="display" font-size="100" fill="{INK}" letter-spacing="1.5">{shown_fmt}</text>
    <rect x="390" y="182" width="0" height="6" rx="2" fill="{CORAL}">
      <animate attributeName="width" from="0" to="330" begin=".6s" dur="1.4s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1"/>
    </rect>
    <text x="390" y="212" class="sub">&gt; HOURS SPENT BUILDING ON GITHUB</text>
  </g>

  <g transform="translate(390, 234)"><g class="rise" style="animation-delay:.5s">
    <rect x="0" y="0" width="134" height="72" rx="6" fill="{CARD_INK}"/>
    <text x="16" y="38" class="stat-val">{d["sessions"]:,}</text>
    <text x="16" y="56" class="stat-lbl">WORK SESSIONS</text>
    <rect x="146" y="0" width="134" height="72" rx="6" fill="{CARD_INK}"/>
    <text x="162" y="38" class="stat-val">{d["days"]:,}</text>
    <text x="162" y="56" class="stat-lbl">DAYS BUILDING</text>
    <rect x="292" y="0" width="134" height="72" rx="6" fill="{CARD_INK}"/>
    <text x="308" y="38" class="stat-val" fill="{CORAL}">{d["streak"]}</text>
    <text x="308" y="56" class="stat-lbl">DAYS IN A ROW</text>
  </g></g>

  <!-- The last twelve months -->
  <line x1="826" y1="36" x2="826" y2="{H - 36}" stroke="{INK}" stroke-opacity="0.15" stroke-dasharray="4 4"/>
  <text x="850" y="56" class="eyebrow">HOURS · LAST 12 MONTHS</text>
  {"".join(bars)}
  <text x="850" y="{H - 30}" class="foot" fill="{CORAL_INK}">● Estimated from commit times · {d["updated_date"]}</text>
</g>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="6" fill="none" stroke="{INK}" stroke-opacity="0.25"/>
</svg>
'''


def build_readme_block(d: dict) -> str:
    shown = f"{floor_to(d['hours']):,}+"
    return (
        "<!-- hours-start -->\n"
        '<p align="center">\n'
        f'  <img src="./assets/build-hours.svg" width="100%" alt="Build hours: about {shown} hours spent building on GitHub since {d["since"]}, '
        f'across {d["sessions"]:,} work sessions on {d["days"]:,} days, estimated from commit times." />\n'
        "</p>\n"
        "<!-- hours-end -->"
    )


def update_files(d: dict) -> None:
    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SVG_PATH.write_text(generate_svg(d), encoding="utf-8")
    content = README_PATH.read_text(encoding="utf-8")
    block = build_readme_block(d)
    pattern = re.compile(r"<!-- hours-start -->.*?<!-- hours-end -->", re.DOTALL)
    if pattern.search(content):
        content = pattern.sub(block, content)
    else:
        # First run: right under the code telemetry.
        content = content.replace("<!-- loc-end -->", "<!-- loc-end -->\n\n" + block, 1)
    README_PATH.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    times, repo_count = collect_times(get_token())
    data = estimate(times)
    print(f"{data['hours']:.0f} hours across {data['sessions']} sessions, {data['commits']} commits, "
          f"{data['days']} days, {repo_count} repos, since {data['since']}.")
    update_files(data)
