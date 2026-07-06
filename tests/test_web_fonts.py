"""Bundled Jetendard web font (JetBrains Mono + Pretendard Hangul, SIL OFL 1.1).

Self-hosted woff2 under ``web/static/fonts`` so the on-prem web UI renders
Korean + code in one aligned monospace face with no CDN. These tests pin the
asset presence + CSS wiring so a stray delete or a broken ``@font-face`` path
is caught, and so the license file can never be dropped (OFL compliance).
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "agent_cli" / "web" / "static"
_FONTS = _STATIC / "fonts"

# The four weights the UI actually uses (400/500/600/700); italics + the other
# weights are synthesised/snapped, so they are intentionally NOT bundled.
_WEIGHTS = {
    "Jetendard-Regular.woff2": 400,
    "Jetendard-Medium.woff2": 500,
    "Jetendard-SemiBold.woff2": 600,
    "Jetendard-Bold.woff2": 700,
}


def test_all_four_woff2_present_and_nonempty():
    for name in _WEIGHTS:
        f = _FONTS / name
        assert f.is_file(), f"missing bundled font {name}"
        assert f.stat().st_size > 100_000, f"{name} looks truncated"


def test_ofl_license_bundled():
    # OFL 1.1 requires the license travel with the font.
    ofl = _FONTS / "OFL.txt"
    assert ofl.is_file()
    assert "SIL OPEN FONT LICENSE" in ofl.read_text(encoding="utf-8").upper()


def test_style_css_declares_each_weight():
    css = (_STATIC / "style.css").read_text(encoding="utf-8")
    assert css.count("@font-face") >= 4
    for name, weight in _WEIGHTS.items():
        # each bundled file is referenced …
        assert f"fonts/{name}" in css, f"{name} not referenced in style.css"
    # … at its declared weight (relative url() so --base-path stays correct)
    for weight in _WEIGHTS.values():
        assert f"font-weight: {weight};" in css


def test_monospace_stacks_prefer_jetendard():
    css = (_STATIC / "style.css").read_text(encoding="utf-8")
    # every monospace stack must lead with Jetendard, keeping the system fallback
    import re

    stacks = re.findall(r"font-family:[^;]*monospace;", css)
    assert stacks, "expected monospace font stacks in style.css"
    for s in stacks:
        assert '"Jetendard"' in s, f"mono stack missing Jetendard: {s}"
