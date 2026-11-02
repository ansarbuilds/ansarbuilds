"""Generate assets/hero.svg: an animated banner for the GitHub profile README.

The palette and typographic feel follow the portfolio at ansarbuilds.dev — ivory
paper, near-black ink, one coral accent, condensed display type over mono detail.

GitHub renders README images through <img>, so no JavaScript runs. The 3D motion is
precomputed here: every frame of a rotating icosahedron (plus an orbiting ring of
data points) is projected to 2D and baked into SMIL <animate> value lists.

Run:  python3 assets/generate_banner.py
"""

import math
from pathlib import Path

W, H = 1200, 420
FRAMES = 90
DUR = "26s"  # slow: the banner should read as calm, not busy
CX, CY, SCALE = 925, 196, 118
CAMERA = 4.2  # distance of the eye from the origin, for perspective

# --- palette (mirrors the portfolio tokens) ---------------------------------
IVORY = "#e6d5bd"
PAPER = "#efe2ce"  # a touch lighter, for the upper wash
INK = "#101010"
CORAL = "#d5360c"
CORAL_INK = "#74200d"
MUTED = "#51463c"

# --- geometry ---------------------------------------------------------------
phi = (1 + 5 ** 0.5) / 2
verts = []
for a in (-1, 1):
    for b in (-phi, phi):
        verts += [(0, a, b), (a, b, 0), (b, 0, a)]
norm = math.sqrt(1 + phi * phi)
verts = [(x / norm, y / norm, z / norm) for x, y, z in verts]
edges = sorted({
    tuple(sorted((i, j)))
    for i in range(len(verts)) for j in range(len(verts))
    if i != j and abs(math.dist(verts[i], verts[j]) - 2 / norm) < 1e-6
})
assert len(verts) == 12 and len(edges) == 30

ring = [(1.6 * math.cos(2 * math.pi * k / 14), 0.0, 1.6 * math.sin(2 * math.pi * k / 14)) for k in range(14)]


def rotate(p, t):
    """Rotation for loop position t in [0, 1); periodic so the loop is seamless."""
    x, y, z = p
    ay = 2 * math.pi * t
    ax = 0.45 + 0.22 * math.sin(2 * math.pi * t)
    x, z = x * math.cos(ay) + z * math.sin(ay), -x * math.sin(ay) + z * math.cos(ay)
    y, z = y * math.cos(ax) - z * math.sin(ax), y * math.sin(ax) + z * math.cos(ax)
    return x, y, z


def rotate_ring(p, t):
    x, y, z = p
    ay = -2 * math.pi * t  # ring counter-rotates once per loop
    x, z = x * math.cos(ay) + z * math.sin(ay), -x * math.sin(ay) + z * math.cos(ay)
    tilt = 0.34
    y, z = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
    lean = -0.3
    x, y = x * math.cos(lean) - y * math.sin(lean), x * math.sin(lean) + y * math.cos(lean)
    return x, y, z


def project(p):
    x, y, z = p
    f = CAMERA / (CAMERA - z)
    return CX + x * SCALE * f, CY + y * SCALE * f, f


def depth_alpha(z, lo=0.18, hi=1.0):
    return lo + (hi - lo) * (z + 1) / 2


fmt = lambda v: f"{v:.1f}"
ts = [k / FRAMES for k in range(FRAMES + 1)]  # last frame == first, closes the loop

# --- per-frame data -----------------------------------------------------------
vframes = [[rotate(v, t) for v in verts] for t in ts]
rframes = [[rotate_ring(r, t) for r in ring] for t in ts]


def anim(attr, values, dur=DUR):
    return f'<animate attributeName="{attr}" dur="{dur}" repeatCount="indefinite" values="{";".join(values)}"/>'


edge_svg = []
for i, j in edges:
    a = [project(vs[i]) for vs in vframes]
    b = [project(vs[j]) for vs in vframes]
    op = [f"{depth_alpha((vs[i][2] + vs[j][2]) / 2, 0.08, 0.6):.2f}" for vs in vframes]
    edge_svg.append(
        f'<line stroke="{INK}" stroke-width="1.1" stroke-linecap="round">'
        + anim("x1", [fmt(p[0]) for p in a]) + anim("y1", [fmt(p[1]) for p in a])
        + anim("x2", [fmt(p[0]) for p in b]) + anim("y2", [fmt(p[1]) for p in b])
        + anim("stroke-opacity", op) + "</line>"
    )

vert_svg = []
for i in range(len(verts)):
    p = [project(vs[i]) for vs in vframes]
    vert_svg.append(
        f'<circle fill="{INK}">'
        + anim("cx", [fmt(q[0]) for q in p]) + anim("cy", [fmt(q[1]) for q in p])
        + anim("r", [f"{1.6 + 2.0 * (vs[i][2] + 1) / 2:.2f}" for vs in vframes])
        + anim("fill-opacity", [f"{depth_alpha(vs[i][2], 0.2, 0.9):.2f}" for vs in vframes])
        + "</circle>"
    )

# The orbiting points carry the single accent colour; everything else is ink.
ring_svg = []
for k in range(len(ring)):
    p = [project(rs[k]) for rs in rframes]
    color = CORAL if k % 3 == 0 else CORAL_INK
    ring_svg.append(
        f'<circle fill="{color}">'
        + anim("cx", [fmt(q[0]) for q in p]) + anim("cy", [fmt(q[1]) for q in p])
        + anim("r", [f"{1.3 + 2.1 * (rs[k][2] + 1.6) / 3.2:.2f}" for rs in rframes])
        + anim("fill-opacity", [f"{0.18 + 0.72 * (rs[k][2] + 1.6) / 3.2:.2f}" for rs in rframes])
        + "</circle>"
    )

# Ring track: static ellipse approximating the orbit path
track = [project(rotate_ring((1.6 * math.cos(a), 0, 1.6 * math.sin(a)), 0)) for a in [2 * math.pi * s / 64 for s in range(65)]]
track_path = "M" + " L".join(f"{fmt(x)} {fmt(y)}" for x, y, _ in track)

# --- ruled paper: horizontal rules that breathe, like a drafting sheet --------
RULE_TOP, RULE_GAP, RULES = 86, 34, 9
rules = []
for r in range(RULES):
    y = RULE_TOP + r * RULE_GAP
    phase = (r * 0.8) % 9
    rules.append(
        f'<line x1="0" x2="{W}" y1="{y}" y2="{y}">'
        f'<animate attributeName="stroke-opacity" dur="9s" begin="-{phase:.2f}s" '
        f'repeatCount="indefinite" values="0.05;0.16;0.05" calcMode="spline" '
        f'keyTimes="0;0.5;1" keySplines=".4 0 .6 1;.4 0 .6 1"/></line>'
    )

# --- typed tagline ------------------------------------------------------------
phrases = [
    "building reliable data systems",
    "shipping evidence-backed AI tools",
    "taking small products idea → release",
]
SLOT = 4.6  # seconds per phrase
TOTAL = SLOT * len(phrases)
typed = []
for n, text in enumerate(phrases):
    width = len(text) * 12.4 + 6
    start, end = n * SLOT / TOTAL, (n + 1) * SLOT / TOTAL
    k = lambda f: f"{start + (end - start) * f:.4f}"
    key_times = f"0;{k(0)};{k(0.45)};{k(0.84)};{k(0.97)};{k(1.0)};1"
    if n == 0:
        key_times = f"0;{k(0.45)};{k(0.84)};{k(0.97)};{k(1.0)};1"
        widths = f"0;{width:.0f};{width:.0f};0;0;0"
    else:
        widths = f"0;0;{width:.0f};{width:.0f};0;0;0"
    typed.append(
        f'<clipPath id="type{n}"><rect x="96" y="242" height="32">'
        f'<animate attributeName="width" dur="{TOTAL:.1f}s" repeatCount="indefinite" '
        f'keyTimes="{key_times}" values="{widths}" calcMode="linear"/></rect></clipPath>'
        f'<text x="98" y="266" clip-path="url(#type{n})" class="mono">{text}</text>'
    )

# --- chips --------------------------------------------------------------------
chips = [("Solutions Engineer", 78, 238), ("ex-Prime Video", 330, 182), ("Canada", 524, 130)]
chip_svg = []
for label, x, w in chips:
    chip_svg.append(
        f'<rect x="{x}" y="316" width="{w}" height="34" rx="4" fill="none" '
        f'stroke="{MUTED}" stroke-opacity="0.45"/>'
        f'<text x="{x + 18}" y="338" class="chip">{label}</text>'
    )

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Hi, I'm Ansar. Solutions Engineer building data systems, AI tools and apps.">
<title>Hi, I'm Ansar</title>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0.6" y2="1">
    <stop offset="0" stop-color="{PAPER}"/><stop offset="0.6" stop-color="{IVORY}"/><stop offset="1" stop-color="#dcc9ae"/>
  </linearGradient>
  <radialGradient id="halo" cx="0.5" cy="0.5" r="0.5">
    <stop offset="0" stop-color="{CORAL}" stop-opacity="0.16"/><stop offset="0.65" stop-color="{CORAL}" stop-opacity="0.04"/><stop offset="1" stop-color="{CORAL}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="leftFade" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="{IVORY}" stop-opacity="0.96"/><stop offset="0.5" stop-color="{IVORY}" stop-opacity="0.62"/><stop offset="0.74" stop-color="{IVORY}" stop-opacity="0"/>
  </linearGradient>
  <clipPath id="card"><rect width="{W}" height="{H}" rx="6"/></clipPath>
  <style>
    .display {{ font-family: "Anton", "Arial Narrow", "Haettenschweiler", Impact, sans-serif; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 21px; fill: {CORAL_INK}; }}
    .chip {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; fill: {MUTED}; letter-spacing: 1px; }}
    .eyebrow {{ font-family: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; fill: {MUTED}; letter-spacing: 4px; }}
  </style>
</defs>
<g clip-path="url(#card)">
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <!-- ruled sheet -->
  <g stroke="{INK}" stroke-width="1">{"".join(rules)}</g>
  <!-- registration marks, drifting slowly like dust on paper -->
  <g fill="{MUTED}">
    {"".join(f'<circle cx="{(i * 397) % W}" cy="{40 + (i * 151) % 330}" r="{0.7 + (i % 3) * 0.3:.2f}"><animate attributeName="opacity" dur="11s" begin="-{(i * 0.41) % 11:.2f}s" repeatCount="indefinite" values=".06;.3;.06" calcMode="spline" keyTimes="0;0.5;1" keySplines=".4 0 .6 1;.4 0 .6 1"/></circle>' for i in range(1, 34))}
  </g>
  <!-- 3D object -->
  <ellipse cx="{CX}" cy="{CY}" rx="250" ry="205" fill="url(#halo)"/>
  <path d="{track_path}" fill="none" stroke="{CORAL}" stroke-opacity="0.2" stroke-dasharray="2 7"/>
  <g>
    <animateTransform attributeName="transform" type="translate" dur="9s" repeatCount="indefinite" values="0 0;0 -8;0 0" calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1"/>
    <g>{"".join(edge_svg)}</g>
    {"".join(vert_svg)}
    {"".join(ring_svg)}
  </g>
  <rect width="{W}" height="{H}" fill="url(#leftFade)"/>
  <!-- text -->
  <g>
    <text x="80" y="62" class="eyebrow">MOHAMED ANSAR / SOLUTIONS ENGINEER</text>
    <line x1="80" y1="80" x2="640" y2="80" stroke="{INK}" stroke-opacity="0.2"/>
    <text x="76" y="196" class="display" font-size="118" fill="{INK}" letter-spacing="1">ANSAR</text>
    <rect x="76" y="212" height="7" fill="{CORAL}">
      <animate attributeName="width" dur="9s" repeatCount="indefinite" values="0;318;318;0" keyTimes="0;0.22;0.8;1" calcMode="spline" keySplines=".22 1 .36 1;0 0 1 1;.22 1 .36 1"/>
    </rect>
    <g>
      <text x="80" y="266" class="mono" fill="{CORAL}">&gt;</text>
      <g>{"".join(typed)}</g>
    </g>
    <g>{"".join(chip_svg)}</g>
  </g>
</g>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="6" fill="none" stroke="{INK}" stroke-opacity="0.25"/>
</svg>
'''

out = Path(__file__).with_name("hero.svg")
out.write_text(svg)
print(f"wrote {out} ({len(svg) / 1024:.0f} KB)")
