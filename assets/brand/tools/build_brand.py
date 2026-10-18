# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools==4.66.1"]
# ///
"""Build every llmfailover brand asset from source, reproducibly.

    uv run assets/brand/tools/build_brand.py

Requires `resvg` on PATH for the PNG exports (`cargo install resvg --locked --version 0.48.1`).

The wordmark is set in JetBrains Mono ExtraBold and converted to outlines, so
the logos render identically without the font installed. The font is fetched
from the pinned upstream release and verified by SHA-256 (it is not vendored).
JetBrains Mono is licensed under the SIL Open Font License 1.1, which permits
using its outlines in artwork such as a logo.

The mark is an open circuit breaker on the primary line with a detour arc
around it: the request still gets from one end to the other.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = pathlib.Path(__file__).resolve().parents[1]
FONT_URL = "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip"
FONT_SHA256 = "6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf"

# Palette. One accent ("healthy route" green-teal), two tones so it passes
# contrast on both GitHub themes, plus a breaker red used only in the mark.
INK = "#0C1417"  # mark and wordmark on light backgrounds
SNOW = "#EEF4F1"  # mark and wordmark on dark backgrounds
ROUTE_ON_LIGHT = "#007A55"
ROUTE_ON_DARK = "#34D399"
TRIP_ON_LIGHT = "#C2410C"
TRIP_ON_DARK = "#FB923C"
MUTED_ON_DARK = "#93A3A0"
GITHUB_LIGHT_BG = "#FFFFFF"
GITHUB_DARK_BG = "#0D1117"


# ----------------------------------------------------------------- contrast


def _lum(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ----------------------------------------------------------------- fonts


def load_fonts() -> dict[str, TTFont]:
    cache = OUT / "tools" / ".cache" / "JetBrainsMono-2.304.zip"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FONT_URL, timeout=60) as r:
            cache.write_bytes(r.read())
    data = cache.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != FONT_SHA256:
        cache.unlink()
        sys.exit(f"font archive checksum mismatch: {digest}")
    z = zipfile.ZipFile(io.BytesIO(data))
    return {
        w: TTFont(io.BytesIO(z.read(f"fonts/ttf/JetBrainsMono-{w}.ttf"))) for w in ("ExtraBold", "Medium")
    }


def _ntos(n: float) -> str:
    s = f"{n:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def text_path(
    font: TTFont, text: str, size: float, x: float, baseline: float, tracking: float = 0.0
) -> tuple[str, float]:
    """Outline `text` as an SVG path. Returns (d, advance width)."""
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = size / font["head"].unitsPerEm
    pen = SVGPathPen(glyphs, ntos=_ntos)
    cursor = x
    for ch in text:
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, cursor, baseline)))
        cursor += font["hmtx"][name][0] * scale + tracking
    return pen.getCommands(), cursor - x - tracking


def x_height(font: TTFont, size: float) -> float:
    return font["OS/2"].sxHeight * size / font["head"].unitsPerEm


# ----------------------------------------------------------------- the mark


def mark(fg: str, route: str, trip: str, *, x: float = 0, y: float = 0, size: float = 64) -> str:
    """The mark on a 64-unit grid.

    Ink: two terminals joined by the primary line, broken by an open breaker
    lever (in the trip colour). Accent: the failover arc that carries the
    request over the open breaker to the far terminal.
    """
    s = size / 64
    return (
        f'<g transform="translate({_ntos(x)} {_ntos(y)}) scale({_ntos(s)})" fill="none" stroke-linecap="round" stroke-linejoin="round">'
        # failover arc (accent), drawn first so the terminals sit on top
        f'<path stroke="{route}" stroke-width="5.5" d="M10 46V30C10 17 18 11 32 11C46 11 54 17 54 30V46"/>'
        # primary line, left and right stubs
        f'<path stroke="{fg}" stroke-width="5.5" d="M10 46H21M43 46H54"/>'
        # open breaker lever, pivoting on the left contact
        f'<path stroke="{trip}" stroke-width="5" d="M21 46L37.5 35"/>'
        f'<circle cx="43" cy="46" r="2.6" fill="{fg}" stroke="none"/>'
        # terminals
        f'<circle cx="10" cy="46" r="6" fill="{fg}" stroke="none"/>'
        f'<circle cx="54" cy="46" r="6" fill="{route}" stroke="none"/>'
        "</g>"
    )


def favicon_mark() -> str:
    """Small-size variant: own tile, heavier strokes."""
    return (
        f'<rect width="32" height="32" rx="7" fill="{INK}"/>'
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">'
        f'<path stroke="{ROUTE_ON_DARK}" stroke-width="3.2" d="M7 23V15.5C7 10 10.5 7 16 7C21.5 7 25 10 25 15.5V23"/>'
        f'<path stroke="{SNOW}" stroke-width="3.2" d="M7 23H11.5M20.5 23H25"/>'
        f'<path stroke="{TRIP_ON_DARK}" stroke-width="3" d="M11.5 23L18.5 18"/>'
        f'<circle cx="7" cy="23" r="3.3" fill="{SNOW}" stroke="none"/>'
        f'<circle cx="25" cy="23" r="3.3" fill="{ROUTE_ON_DARK}" stroke="none"/>'
        "</g>"
    )


def svg(width: float, height: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_ntos(width)}" height="{_ntos(height)}" '
        f'viewBox="0 0 {_ntos(width)} {_ntos(height)}" role="img" aria-label="{title}">'
        f"<title>{title}</title>{body}</svg>\n"
    )


def lockup(fonts: dict[str, TTFont], fg: str, route: str, trip: str) -> str:
    size, gap = 44.0, 16.0
    xh = x_height(fonts["ExtraBold"], size)
    baseline = 32 + xh / 2
    d_llm, w_llm = text_path(fonts["ExtraBold"], "llm", size, 64 + gap, baseline, tracking=-0.6)
    d_fo, w_fo = text_path(
        fonts["ExtraBold"], "failover", size, 64 + gap + w_llm - 0.6, baseline, tracking=-0.6
    )
    width = 64 + gap + w_llm - 0.6 + w_fo + 2
    body = mark(fg, route, trip) + f'<path fill="{fg}" d="{d_llm}"/><path fill="{route}" d="{d_fo}"/>'
    return svg(width, 64, body, "llmfailover")


def avatar() -> str:
    """Opaque square avatar: mark on the ink tile, no rounding (GitHub rounds it)."""
    return f'<rect width="512" height="512" fill="{INK}"/>' + mark(
        SNOW, ROUTE_ON_DARK, TRIP_ON_DARK, x=96, y=86, size=320
    )


def social(fonts: dict[str, TTFont]) -> str:
    W, H, left = 1280, 640, 96
    eb, md = fonts["ExtraBold"], fonts["Medium"]
    parts = [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    parts.append(mark(SNOW, ROUTE_ON_DARK, TRIP_ON_DARK, x=left - 10, y=88, size=128))
    wsize = 88
    base = 88 + 64 + x_height(eb, wsize) / 2
    d1, w1 = text_path(eb, "llm", wsize, left + 146, base, tracking=-1.2)
    d2, _ = text_path(eb, "failover", wsize, left + 146 + w1 - 1.2, base, tracking=-1.2)
    parts.append(f'<path fill="{SNOW}" d="{d1}"/><path fill="{ROUTE_ON_DARK}" d="{d2}"/>')
    for i, line in enumerate(("When a model provider goes down,", "your requests route around it.")):
        d, _ = text_path(md, line, 50, left, 330 + i * 66)
        parts.append(f'<path fill="{SNOW}" d="{d}"/>')
    for i, line in enumerate(
        (
            "OpenAI-compatible gateway: Redis-shared circuit breakers, half-open",
            "self-healing, hedging, idempotency keys, per-tenant cost. Chaos-tested.",
        )
    ):
        d, _ = text_path(md, line, 25, left, 466 + i * 36)
        parts.append(f'<path fill="{MUTED_ON_DARK}" d="{d}"/>')
    d, _ = text_path(md, "github.com/niravpatidar37/llmfailover", 24, left, 572)
    parts.append(f'<path fill="{ROUTE_ON_DARK}" d="{d}"/>')
    return svg(
        W, H, "".join(parts), "llmfailover: when a model provider goes down, your requests route around it"
    )


# ----------------------------------------------------------------- safety gate

FORBIDDEN = [
    (re.compile(r"<\s*script", re.I), "script element"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "event handler attribute"),
    (re.compile(r"<\s*foreignObject", re.I), "foreignObject"),
    (re.compile(r"(?:xlink:)?href\s*=\s*\"(?!#)", re.I), "external reference"),
    (re.compile(r"url\(\s*['\"]?(?!#)", re.I), "external url()"),
    (re.compile(r"javascript:", re.I), "javascript: URL"),
]


def check_safe(name: str, text: str) -> None:
    for pattern, what in FORBIDDEN:
        if pattern.search(text):
            sys.exit(f"{name}: unsafe SVG content ({what})")


# ----------------------------------------------------------------- main


def main() -> None:
    checks = {
        "route accent on GitHub light": contrast(ROUTE_ON_LIGHT, GITHUB_LIGHT_BG),
        "route accent on GitHub dark": contrast(ROUTE_ON_DARK, GITHUB_DARK_BG),
        "trip on GitHub light": contrast(TRIP_ON_LIGHT, GITHUB_LIGHT_BG),
        "trip on GitHub dark": contrast(TRIP_ON_DARK, GITHUB_DARK_BG),
        "ink on GitHub light": contrast(INK, GITHUB_LIGHT_BG),
        "snow on GitHub dark": contrast(SNOW, GITHUB_DARK_BG),
        "favicon route on tile": contrast(ROUTE_ON_DARK, INK),
        "social muted on tile": contrast(MUTED_ON_DARK, INK),
    }
    for label, ratio in checks.items():
        print(f"contrast {label}: {ratio:.2f}:1")
        if ratio < 3.0:
            sys.exit(f"contrast below WCAG 3:1 for graphics: {label}")

    fonts = load_fonts()
    files = {
        "icon.svg": svg(64, 64, mark(INK, ROUTE_ON_LIGHT, TRIP_ON_LIGHT), "llmfailover"),
        "icon-dark.svg": svg(64, 64, mark(SNOW, ROUTE_ON_DARK, TRIP_ON_DARK), "llmfailover"),
        "favicon.svg": svg(32, 32, favicon_mark(), "llmfailover"),
        "avatar.svg": svg(512, 512, avatar(), "llmfailover"),
        "logo.svg": lockup(fonts, INK, ROUTE_ON_LIGHT, TRIP_ON_LIGHT),
        "logo-dark.svg": lockup(fonts, SNOW, ROUTE_ON_DARK, TRIP_ON_DARK),
        "social-preview.svg": social(fonts),
    }
    for name, text in files.items():
        check_safe(name, text)
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {name} ({len(text)} bytes)")

    resvg = shutil.which("resvg")
    if not resvg:
        sys.exit("resvg not found on PATH; install it to export PNGs")
    renders = [
        ("favicon.svg", "favicon-16.png", 16),
        ("favicon.svg", "favicon-32.png", 32),
        ("avatar.svg", "avatar-512.png", 512),
        ("social-preview.svg", "social-preview.png", 1280),
    ]
    for src, dst, width in renders:
        # List-form argv, no shell. All arguments are constants from `renders`
        # plus the absolute resvg path from shutil.which.
        argv = [resvg, "-w", str(width), str(OUT / src), str(OUT / dst)]
        subprocess.run(argv, check=True, shell=False)  # noqa: S603  # nosec B603
        print(f"rendered {dst} ({(OUT / dst).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
