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
# the cast and its five moments
# --------------------------------------------------------------------------- #
def test_one_helper_decides_whether_anything_moves(page: str) -> None:
    """A screenshot in progress and a reader who asked for less motion are the
    same answer, so every flourish asks the same question."""
    assert "const stillness = () => Boolean(document.documentElement.dataset.still) || calm.matches;" in page
    assert 'const calm = matchMedia("(prefers-reduced-motion: reduce)");' in page
    for gated in ("if (running || stillness()) return;",
                  "if (stillness() || performance.now() - struck < 55) return;",
                  "if (typed || stillness()) {"):
        assert gated in page, f"ungated: {gated}"


def test_the_figure_is_animated_in_parts(page: str) -> None:
    """A single sliding sprite is what makes this sort of thing look like a
    cut-out. Each part carries its own clock."""
    for part in ("@keyframes hem{", "@keyframes blink{", "@keyframes gasp{",
                 "@keyframes streak{", "@keyframes bite{", "@keyframes wake{"):
        assert part in page, f"missing: {part}"
    assert ".rev .peak:nth-child(5){animation-delay:320ms}" in page, "the hem ripples in sequence"


def test_the_run_cleans_up_after_itself(page: str) -> None:
    body = page.split("function run() {", 1)[1].split("$(\"wordmark\")", 1)[0]
    for line in ("figure.remove();", "shape.remove();", "for (const crumb of crumbs) crumb.remove();",
                 'for (const left of lane.querySelectorAll(".crumb")) left.remove();',
                 "running = false;"):
        assert line in body, f"missing: {line}"


def test_the_cast_draws_only_this_project(page: str) -> None:
    """The runner is the app's own icon and its pursuer is a block cursor.

    Both take their colour from tokens the interface already defines, so no
    third party's character, mark or livery is reproduced.
    """
    assert "color:var(--ember)" in page.split(".rev{", 1)[1].split("}", 1)[0]
    assert "color:var(--codex)" in page.split(".pur{", 1)[1].split("}", 1)[0]
    # The body and every hem peak are shared with assets/icon.svg, character for
    # character, so the mascot and the installed icon cannot drift apart.
    icon = (PAGE.parents[1] / "assets" / "icon.svg").read_text(encoding="utf-8")
    shared = re.findall(r'<path d="(M[^"]+)"/>', icon)
    assert len(shared) >= 6, "expected the body and five hem peaks in the icon"
    for path in shared[:6]:
        assert path in page, f"icon and mascot disagree on {path[:28]}"


def test_the_other_four_moments_are_wired(page: str) -> None:
    """One animation in one corner was the complaint. These are the rest."""
    assert 'bit.className = "spark";' in page and "sparks();" in page, "the ruler caret"
    assert 'soul.className = "soul";' in page and "@keyframes ascend{" in page, "the register rows"
    assert 'class="mote"' in page and "@keyframes rise{" in page, "the empty register"
    assert 'el.claim.dataset.typing = "true";' in page and "@keyframes pulse{" in page, "the claim"


def test_only_the_first_claim_is_typed(page: str) -> None:
    """Later ones answer a question the reader is already waiting on."""
    assert "let typed = false;" in page
    body = page.split("function claim(text) {", 1)[1].split("function paint()", 1)[0]
    assert "if (typed || stillness()) {" in body
    assert "typed = true;" in body


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
