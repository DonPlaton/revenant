"""Source-level guards on the single-file interface.

The page cannot be executed here, so these assert the properties that a browser
would otherwise have to catch: that every mark on the time ruler is placed by one
expression, that the masthead run stays out of the way when it should, and that
nothing in the file reaches for the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "ui" / "index.html"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the ruler
# --------------------------------------------------------------------------- #
def test_every_mark_on_the_ruler_comes_from_one_expression(page: str) -> None:
    """Ticks once sat in seven equal columns while the caret sat at i/6.

    They agreed only at zero, so the caret drifted up to a sixth of the track
    away from the tick it claimed to be on.
    """
    assert "const where = (i) => (i / (STOPS.length - 1)) * 100;" in page
    for user in ("el.ticks.innerHTML = STOPS.map((s, i) => `<i style=\"left:${where(i)}%\"></i>`)",
                 "el.lit.style.width = `${where(index)}%`",
                 "el.caret.style.left = `${where(index)}%`"):
        assert user in page, f"missing: {user}"
    assert "gridTemplateColumns" not in page, "a grid would put the marks back on their own scale"


def test_the_caret_and_a_tick_share_a_centre(page: str) -> None:
    """Both are drawn from the same left edge, so each pulls back by half itself."""
    assert "width:3px;height:19px;margin-left:-1.5px" in page
    assert ".track .ticks i{position:absolute;top:0;height:11px;margin-left:-.5px;" in page


def test_the_slider_thumb_does_not_inset_its_own_travel(page: str) -> None:
    """A wide thumb stops half its width short at each end and drags the value off the caret."""
    assert "input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:1px" in page
    assert "input[type=range]::-moz-range-thumb{width:1px" in page


# --------------------------------------------------------------------------- #
# the masthead run
# --------------------------------------------------------------------------- #
def test_the_run_yields_to_reduced_motion_and_to_screenshots(page: str) -> None:
    guard = page.split("function run() {", 1)[1].split("const width", 1)[0]
    assert "document.documentElement.dataset.still" in guard, "asset builds must stay still"
    assert 'matchMedia("(prefers-reduced-motion: reduce)").matches' in guard
    assert "if (running" in guard, "a second run must not start on top of the first"


def test_the_run_cleans_up_after_itself(page: str) -> None:
    body = page.split("function run() {", 1)[1].split("$(\"wordmark\")", 1)[0]
    for line in ("figure.remove();", "shape.remove();", "for (const crumb of crumbs) crumb.remove();",
                 "running = false;"):
        assert line in body, f"missing: {line}"


def test_the_run_draws_only_this_project(page: str) -> None:
    """The runner is the app's own icon and the shape behind it is a plain disc.

    Both take their colour from the tokens the interface already defines, so no
    third party's character, mark or livery is reproduced.
    """
    assert "color:var(--ember)" in page.split(".runner{", 1)[1].split("}", 1)[0]
    assert "background:var(--codex)" in page.split(".chomp{", 1)[1].split("}", 1)[0]
    # The path is the figure from assets/icon.svg.
    icon = (PAGE.parents[1] / "assets" / "icon.svg").read_text(encoding="utf-8")
    body = re.search(r'd="(M128 46c-33[^"]+)"', icon)
    assert body and body.group(1) in page


# --------------------------------------------------------------------------- #
# the file as a whole
# --------------------------------------------------------------------------- #
def test_the_interface_asks_the_network_for_nothing(page: str) -> None:
    """It has to work on a machine whose network is down, which is often the point."""
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:|//)', page)
    assert not remote, f"remote asset referenced: {remote}"
    assert "@import" not in page
    assert "connect-src 'self'" in page


def test_the_copy_carries_no_stray_typography(page: str) -> None:
    """Curly quotes and em dashes arrive by autocorrect and never leave on their own."""
    for stray in "—–“”‘’":
        assert stray not in page, f"stray {stray!r} in the interface copy"
