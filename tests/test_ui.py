"""Source-level guards on the single-file interface.

The page cannot be executed here, so these assert the properties that a browser
would otherwise have to catch: that every mark on the time ruler is placed by one
expression, that the masthead run stays out of the way when it should, and that
nothing in the file reaches for the network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def test_the_ruler_can_reach_everything_on_disk(page: str) -> None:
    """Sessions older than the furthest stop were unreachable from the app.

    On a real machine 61 of 162 Codex transcripts sat past ninety days, and the
    command line could list them while the window could not.
    """
    stops = page.split("const STOPS = [", 1)[1].split("];", 1)[0]
    assert '{ days: 36500, mark: "all", said: "everything on disk" }' in stops
    assert stops.count("days:") == 8
    assert 'min="0" max="7" step="1"' in page, "the range has to span every stop"


# --------------------------------------------------------------------------- #
# the cast and its five moments
# --------------------------------------------------------------------------- #
def test_one_helper_decides_whether_anything_moves(page: str) -> None:
    """A screenshot in progress and a reader who asked for less motion are the
    same answer, so every flourish asks the same question."""
    assert "const stillness = () => Boolean(document.documentElement.dataset.still) || calm.matches;" in page
    assert 'const calm = matchMedia("(prefers-reduced-motion: reduce)");' in page
    for gated in ("function run(which) {\n    if (stillness() || FRAME !== null) return;",
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
    body = page.split("function run(which) {", 1)[1].split("$(\"wordmark\")", 1)[0]
    # One sweep of the lane, and it iterates a copy: removing from a live
    # HTMLCollection while walking it leaves every other sprite behind.
    assert "for (const node of [...lane.children]) node.remove();" in body
    assert "for (const stale of [...lane.children]) stale.remove();" in body, "a new run clears the old"
    assert body.count("clear();") >= 2, "both choreographies have to end by clearing"


def test_each_agent_tab_gets_its_own_run(page: str) -> None:
    """The complaint was that the modes were indistinguishable. They are not now."""
    plans = page.split("const RUNS = {", 1)[1].split("};", 1)[0]
    assert '"claude-code": { lead: "runner"' in plans, "the revenant is chased"
    assert 'codex: { lead: "hunter"' in plans, "in Codex the roles swap"
    assert "all: { meet: true }" in plans, "the combined view is an approach, not a chase"
    assert "run(agent);\n        load();" in page, "switching tabs has to start one"
    assert ".runner.ahead .pupil{" in page, "a chasing revenant looks forward"


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


def test_nothing_pale_rings_the_figure_on_the_icon() -> None:
    """White eyes, an edge light and a glow all drew pale rings round the figure
    at taskbar sizes. The eyes are holes in the sheet now, as dark as the tile."""
    icon = (PAGE.parents[1] / "assets" / "icon.svg").read_text(encoding="utf-8")
    assert "#f4efe6" not in icon.lower(), "the icon's eyes went back to white"
    assert "radialGradient" not in icon, "a glow is back behind the figure"
    assert "stroke" not in icon, "the tile has an edge again"
    eyes = re.findall(r'<ellipse class="eye"[^>]*fill="#([0-9a-f]{6})"', icon, re.I)
    assert len(eyes) == 2
    assert all(max(int(e[i:i + 2], 16) for i in (0, 2, 4)) < 0x20 for e in eyes)


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


def test_the_page_script_actually_parses(page: str, tmp_path: Path) -> None:
    """Every other test here reads the page as text, and text can be wrong in a
    way no substring check notices.

    A second `const SETTLE` declared in the same scope once passed all of them.
    It is a SyntaxError, which stops every line of the page's script from
    running: the window opens and nothing in it works.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed, so the script cannot be parsed here")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S | re.I)
    assert scripts, "the page has no inline script"
    target = tmp_path / "page.js"
    target.write_text("\n;\n".join(scripts), encoding="utf-8")
    checked = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr.strip()


# --------------------------------------------------------------------------- #
# reviving
# --------------------------------------------------------------------------- #
def _revive_handler(page: str) -> str:
    return page.split('el.raise.addEventListener("click", async (event) => {', 1)[1].split("// ── window chrome", 1)[0]


def test_the_revival_is_shown_before_anything_launches(page: str) -> None:
    """The terminals come up on top and take focus, so an animation that is still
    running when they start plays to nobody. The request waits for it."""
    body = _revive_handler(page)
    wait = body.index("await Promise.race([show ? show.launch : new Promise((done) => setTimeout(done, beat)), hurried]);")
    ask = body.index('api("/api/revive"')
    stamps = body.index('stamp.textContent = "RAISED";')
    assert stamps < wait < ask, "stamps are scheduled, then the page waits, then it asks"
    assert "STAMP_FROM + every * Math.max(0, ids.length - 1) + STAMP_SETTLE" in body


def test_nothing_to_watch_means_nothing_to_wait_for(page: str) -> None:
    """A reader who asked for less motion is not made to sit through a pause
    that was only there to show them motion."""
    assert "const beat = stillness() ? 0 :" in _revive_handler(page)


def test_a_large_revival_does_not_take_longer_to_show(page: str) -> None:
    """The cascade is squeezed rather than lengthened, so twenty rows wait no
    longer than eight."""
    assert "const stampEvery = (count) => (count > 1 ? Math.min(90, STAMP_SPREAD / (count - 1)) : 0);" in page
    assert "const STAMP_SPREAD = 600;" in page


def test_a_revival_in_flight_cannot_be_sent_twice(page: str) -> None:
    """The wait opens a window in which a second click or an Enter would launch
    the same sessions again."""
    body = _revive_handler(page)
    assert 'if (!chosen.size || el.raise.dataset.busy === "true") return;' in body


def test_revived_sessions_are_unmarked_as_soon_as_they_launch(page: str) -> None:
    """Otherwise they stay marked until the list reloads, and their new
    processes may not have registered as live by the time of a second click."""
    body = _revive_handler(page)
    success = body.split("if (result.ok) {", 1)[1].split("} else {", 1)[0]
    assert "chosen.delete(id);" in success
    assert "repaintTally();" in success


def test_the_lap_a_revival_earns_waits_until_it_can_be_seen(page: str) -> None:
    """It used to play behind the terminals that had just opened."""
    assert 'addEventListener("focus", () => {\n    if (!lapOwed) return;' in page
    success = _revive_handler(page).split("if (result.ok) {", 1)[1].split("} else {", 1)[0]
    assert "lapOwed = true;" in success
    assert "document.hasFocus()" in success, "paid at once if nothing took the focus"


def test_a_soul_is_free_to_rise_past_its_row(page: str) -> None:
    """Rows carry content-visibility, which contains their paint and clips
    anything that leaves the box. A soul appended to its row was sheared off at
    the row's top edge a few pixels into its rise."""
    assert "content-visibility:auto" in page.split(".row{", 1)[1].split("}", 1)[0]
    body = _revive_handler(page)
    assert "el.register.appendChild(soul);" in body
    assert "soul.style.top = `${node.offsetTop + 12}px`;" in body
    assert "node.appendChild(soul)" not in body


def test_the_stamp_does_not_land_on_the_turn_count(page: str) -> None:
    """Both sit at the right edge of a row; the count steps aside while stamped."""
    assert ".row:has(.stamp) .turns{opacity:0" in page


# --------------------------------------------------------------------------- #
# the shuffle
# --------------------------------------------------------------------------- #
def _shuffle_engine(page: str) -> str:
    return page.split("function startShuffle(host, opts) {", 1)[1].split("function deal(ids, corner) {", 1)[0]


def _dealer(page: str) -> str:
    return page.split("function deal(ids, corner) {", 1)[1].split("const STAMP_FROM", 1)[0]


def test_more_than_five_are_dealt_rather_than_stamped(page: str) -> None:
    """Six stamps in a column read as a list. Five and under keep them."""
    assert "const SHUFFLE_FROM = 6;" in page
    body = _revive_handler(page)
    assert "const dealt = ids.length >= SHUFFLE_FROM && !stillness();" in body
    assert "if (!document.documentElement.dataset.still && !dealt) {" in body


def test_a_dealt_revival_launches_as_the_cards_land(page: str) -> None:
    """Filmed on a real desktop, a terminal asked for as the first card left the
    deck came up 0.4 s later over the app and hid the rest of the deal. The
    launch is timed for the last card's landing instead: the request goes out a
    little early and carries the rest of the wait for the service to keep."""
    engine = _shuffle_engine(page)
    assert "launch: 1680" in engine
    assert "const LANDED = T.launch + DEAL * (SHOWN - 1) + FLIGHT;" in engine
    assert "const firstAt = LANDED * PACE - LEAD;" in engine
    assert "const launchAt = Math.max(T.launch, (firstAt - ASK_AHEAD) / PACE);" in engine
    assert "if (!launched && t >= launchAt) {" in engine
    dealer = _dealer(page)
    assert "onLaunch: resolve," in dealer
    assert "lead: (opens[drawn(layout)] || {}).lead," in dealer
    assert "wish: () => ({ room, wait: Math.round(show ? show.wait() : 0) })," in dealer


def test_windows_of_their_own_are_spaced_however_the_revival_goes(page: str) -> None:
    """The gap was the scene's, so an Escape, a revival of five or fewer, or
    reduced motion sent every window at once again: the burst that held even
    the first one back by over a second."""
    assert "const WINDOW_GAP = 110;" in page
    assert 'const sent = { ids, days: stop().days, agent, layout, stagger: drawn(layout) === "tabs" ? 0 : WINDOW_GAP };' in page
    engine = page.split("  const SHUFFLE_FROM = 6;", 1)[1].split("  // Lays the stage over the register", 1)[0]
    assert "STAGGER" not in engine and "stagger: () =>" not in engine, "the scene no longer decides the gap"


def test_the_sheet_is_not_rewritten_while_it_hangs_still(page: str) -> None:
    drape = page.split("    function drape(drag, flare) {", 1)[1].split("\n    }\n", 1)[0]
    assert "if (key === draped) return;" in drape.split("setAttribute", 1)[0]


def test_a_scene_without_frames_still_lets_the_revival_through(page: str) -> None:
    """A hidden window gets no animation frames and a scene can throw; neither
    may hold a launch hostage."""
    dealer = _dealer(page)
    assert "setTimeout(resolve, show.LAUNCH + 400);" in dealer
    assert "} catch (e) {\n        clear();\n        resolve();" in dealer


def test_the_shuffle_fits_the_window_it_plays_in(page: str) -> None:
    """At the minimum window size the register is about 660 x 240, and a grid
    of twelve composed for a full-size window ran off its right edge."""
    engine = _shuffle_engine(page)
    assert "const k = Math.min(1, W / 860, H / 280);" in engine
    assert "host.appendChild(" not in engine, "everything but the veil lives in the scaled scene"
    dealer = _dealer(page)
    assert 'addEventListener("resize", fit);' in dealer
    assert 'removeEventListener("resize", fit);' in dealer


def test_a_failed_revival_cuts_the_shuffle_short(page: str) -> None:
    body = _revive_handler(page)
    failure = body.split("if (result.ok) {", 1)[1].split("} else {", 1)[1]
    assert "if (show) show.fail();" in failure


def test_the_shuffle_says_what_it_is_doing(page: str) -> None:
    """The toast names the deal from the first frame; the stage itself is
    hidden from assistive tech so the announcement is not doubled."""
    dealer = _dealer(page)
    assert "Dealing ${count} sessions into one window. Esc to skip." in dealer
    assert "Dealing ${count} sessions into windows of their own. Esc to skip." in dealer
    assert 'host.setAttribute("aria-hidden", "true");' in dealer


def test_the_shuffle_keeps_to_its_own_classes(page: str) -> None:
    """The page styles .bar, .name and .body globally; an unprefixed class in
    the scene picks those rules up and breaks the drawing."""
    engine = _shuffle_engine(page) + _dealer(page)
    names = re.findall(r'className = "([^"]+)"', engine) + re.findall(r'class="([^"]+)"', engine)
    assert names, "the scene creates no elements"
    for name in names:
        for token in name.split():
            assert token.startswith("sh-"), f"unprefixed class {token!r} in the shuffle"
    styles = page.split("/* ── the shuffle", 1)[1].split("/* ── states", 1)[0]
    for selector in re.findall(r"(?:^|})\s*([^{}@/]+)\{", styles):
        first = selector.strip().split(",")[0].split()[0]
        assert first.startswith(".sh-"), f"shuffle rule {selector.strip()!r} reaches outside the scene"


def test_session_names_reach_the_scene_as_text(page: str) -> None:
    """A label is whatever the user typed as a prompt or a rename; markup in it
    must be shown, never parsed."""
    engine = _shuffle_engine(page)
    for line in engine.splitlines():
        if "innerHTML" in line:
            assert "names" not in line and "label" not in line, line.strip()
    assert 'm.querySelector(".sh-name").textContent = opts.names[i] || "";' in engine
    assert 'screen.said.textContent = opts.names[front] || "";' in engine


def test_the_shuffle_is_composed_above_the_toast(page: str) -> None:
    """The toast sits over the foot of the register; at the minimum window size
    it covered the bottom row of dealt windows."""
    assert "H = host.clientHeight - (opts.below || 0);" in _shuffle_engine(page)
    dealer = _dealer(page)
    assert dealer.index("word(drawn(layout) ===") < dealer.index("el.word.offsetHeight")
    assert "below," in dealer


def test_the_request_carries_what_was_on_screen_at_the_click(page: str) -> None:
    """The ruler, the agent tab and the layout can all change during the wait;
    the deal draws the layout of the click, so that is the one that is sent."""
    body = _revive_handler(page)
    captured = body.index("const sent = { ids, days: stop().days, agent, layout, stagger:")
    assert captured < body.index("await Promise.race([show ? show.launch")
    assert "body: JSON.stringify(show ? { ...sent, ...show.wish() } : sent)," in body


def test_cards_are_dealt_in_the_order_the_terminals_open(page: str) -> None:
    """The backend opens tabs in the order of the ids, and the last one opened
    is in front. The k-th card off the deck lands in the k-th slot."""
    engine = _shuffle_engine(page)
    assert "const rank = (id) => order2.length - 1 - order2.indexOf(id);" in engine
    assert "const dealtAt = (slot) => T.launch + slot * DEAL;" in engine
    assert "const slot = rank(id);\n        const go = dealtAt(slot);" in engine
    assert "const front = SHOWN - 1;" in engine


def test_the_deck_is_gathered_from_the_marked_rows(page: str) -> None:
    """Cards that appear from nowhere could be anything; lifted off the marks,
    they are visibly the sessions that were chosen."""
    dealer = _dealer(page)
    assert "node.getBoundingClientRect().top - top + 17.5" in dealer, "measured by the row box"
    assert "origins," in dealer
    assert '<b class="sh-seed"></b>' in _shuffle_engine(page)


def test_the_destination_shows_its_empty_slots_first(page: str) -> None:
    """Slots drawn dead at the tap, lit as they fill, tell the viewer where the
    cards are going before the first one arrives. More than twelve says so."""
    assert ".sh-tab.sh-live{" in page and ".sh-hole{" in page
    engine = _shuffle_engine(page)
    assert 'tab.classList.toggle("sh-live", live)' in engine
    assert "tail.textContent = `+${more}`;" in engine


def test_the_masthead_waits_for_the_deal_to_land(page: str) -> None:
    success = _revive_handler(page).split("if (result.ok) {", 1)[1].split("} else {", 1)[0]
    assert "(show ? show.ended : Promise.resolve()).then(" in success


def test_nothing_leaves_a_veil_over_the_register(page: str) -> None:
    """A frame that throws, frames that stop, or motion switched off mid-deal
    all end the scene; otherwise the rows would stay hidden behind it."""
    engine = _shuffle_engine(page)
    assert "if (stillness()) return finish();" in engine
    assert "} catch (e) {\n        return finish();" in engine
    assert "guard = setTimeout(() => show.stop(), show.END + 1500);" in _dealer(page)


_SCENE_HARNESS = r"""
const fake = () => {
  const el = {
    style: { setProperty() {} }, dataset: {}, textContent: "", innerHTML: "",
    clientWidth: 0, clientHeight: 0,
    classList: { set: new Set(), contains(c) { return this.set.has(c); },
                 toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); } },
    setAttribute() {}, getAttribute: () => "", appendChild() {}, querySelector: () => fake(),
    querySelectorAll: () => [fake(), fake()], remove() {},
  };
  el.classList.add = (c) => el.classList.set.add(c);
  let first = null, last = null;
  Object.defineProperty(el, "firstChild", { get: () => (first = first || fake()) });
  Object.defineProperty(el, "lastChild", { get: () => (last = last || fake()) });
  return el;
};
globalThis.document = { createElement: fake };
globalThis.requestAnimationFrame = () => 0;
const stillness = () => false;
%ENGINE%
let frames = 0;
for (const layout of ["tabs", "windows"]) {
  for (const count of [6, 9, 12, 17]) {
    for (const withOrigins of [true, false]) {
      for (const [w, h] of [[902, 398], [662, 238], [1822, 758]]) {
        const host = fake();
        host.clientWidth = w; host.clientHeight = h;
        const agents = [...Array(count)].map((_, i) => (i % 3 === 1 ? "codex" : "claude-code"));
        const show = startShuffle(host, {
          count, layout, figure: "", agents, names: agents.map((a, i) => `s${i}`), below: 48, lead: 250,
          origins: withOrigins ? agents.slice(0, 12).map((_, i) => ({ x: 74, y: 17.5 + 96 * i })) : undefined,
        });
        // Half the deals are thrown into where the real window will open.
        if (withOrigins) show.aim([{ x: w * 0.4, y: 8, width: w * 0.8, height: h }], 36);
        const end = show.END / show.PACE;
        for (let t = 0; t <= end + 40; t += 8) { show.render(t); frames++; }
        show.fail();
        show.render(end);
      }
    }
  }
}
console.log("frames", frames);
"""


def test_every_frame_of_the_scene_renders(page: str, tmp_path) -> None:
    """The frame loop catches a throw and ends the scene, which keeps the app
    working but hides the bug: a renamed variable once ended every deal at the
    first card. Here every frame of every layout and size is drawn in node
    against a stand-in DOM, and a throw fails the test."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    engine = page.split("  const SHUFFLE_FROM = 6;", 1)[1].split("  // Lays the stage over the register", 1)[0]
    script = tmp_path / "scene.js"
    script.write_text(_SCENE_HARNESS.replace("%ENGINE%", engine), encoding="utf-8")
    ran = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr.strip()
    assert ran.stdout.startswith("frames ")


_PLACE_HARNESS = r"""
const made = [];
const fake = () => {
  const el = {
    style: { setProperty() {} }, dataset: {}, textContent: "", innerHTML: "", className: "",
    clientWidth: 0, clientHeight: 0,
    classList: { set: new Set(), contains(c) { return this.set.has(c); },
                 toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); } },
    setAttribute() {}, getAttribute: () => "", appendChild() {}, querySelector: () => fake(),
    querySelectorAll: () => [fake(), fake()], remove() { this.gone = true; },
  };
  el.classList.add = (c) => el.classList.set.add(c);
  let first = null, last = null;
  Object.defineProperty(el, "firstChild", { get: () => (first = first || fake()) });
  Object.defineProperty(el, "lastChild", { get: () => (last = last || fake()) });
  made.push(el);
  return el;
};
globalThis.document = { createElement: fake };
globalThis.requestAnimationFrame = () => 0;
let now = 1000;
globalThis.performance = { now: () => now };
const stillness = () => false;
%ENGINE%
const make = (layout, lead) => {
  const host = fake();
  host.clientWidth = 902; host.clientHeight = 398;
  const agents = [...Array(10)].map(() => "claude-code");
  return startShuffle(host, { count: 10, layout, figure: "", agents, names: agents.map((a, i) => `s${i}`),
                              below: 48, lead });
};
const spot = [{ x: 420, y: 8, width: 700, height: 500 }];
const out = {};
const tabs = make("tabs", 250);
out.launch = tabs.LAUNCH;
out.wait = tabs.wait();
out.room = tabs.room;
out.aimed = tabs.aim(spot, 36);
out.again = tabs.aim(spot, 36);
// The window it was dealt into, and where every card ended up.
const frame = made.find((el) => el.className === "sh-win" && el.classList.contains("sh-real"));
tabs.render(2698);
const parse = (el) => (el.style.transform.match(/translate\(([-\d.]+)px, ([-\d.]+)px\)/) || []).slice(1).map(Number);
out.frame = parse(frame);
out.cards = made.filter((el) => el.className === "sh-card").map((el) => parse(el).map((v, i) => v + (i ? 26 : 35)));
const own = make("windows", 250);
out.windows = own.aim(spot, 36);
const late = make("tabs", 250);
now += 3200;
out.late = late.aim(spot, 36);
out.nolead = make("tabs", undefined).LAUNCH;
console.log(JSON.stringify(out));
"""


def test_the_cards_are_thrown_into_where_the_real_window_opens(page: str, tmp_path) -> None:
    """Told where the real window will open before the destination shows, the
    scene draws it there and every card lands in its tab strip. Told too late,
    or for windows of their own, it keeps its stand-in."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    engine = page.split("  const SHUFFLE_FROM = 6;", 1)[1].split("  // Lays the stage over the register", 1)[0]
    script = tmp_path / "place.js"
    script.write_text(_PLACE_HARNESS.replace("%ENGINE%", "  const SHUFFLE_FROM = 6;" + engine), encoding="utf-8")
    ran = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr.strip()
    seen = json.loads(ran.stdout)
    # Ten cards land at 2698 scene ms, 5396 real; the window is due then, and
    # the request goes out half a second early with the rest of the wait.
    assert seen["launch"] == 5396 - 250 - 500
    assert seen["nolead"] == 5396 - 500
    assert 5390 - 250 <= seen["wait"] <= 5396 - 250
    assert seen["room"]["right"] < 420, "the window starts clear of the figure"
    assert seen["aimed"] is True and seen["again"] is False
    assert seen["windows"] is False and seen["late"] is False
    x, y = seen["frame"]
    assert (x, y) == (420, 8)
    assert len(seen["cards"]) == 10
    for cx, cy in seen["cards"]:
        assert x < cx < 902, "every card lands on the strip the stage shows"
        assert y < cy < y + 36


def test_no_top_level_name_is_declared_twice(page: str) -> None:
    """node --check catches a second const, but a second function declaration
    silently replaces the first, which is worse."""
    script = "\n".join(re.findall(r"<script>(.*?)</script>", page, re.S | re.I))
    names = re.findall(r"^  (?:const|let|var)\s+([A-Za-z_$][\w$]*)", script, re.M)
    names += re.findall(r"^  (?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", script, re.M)
    twice = sorted({n for n in names if names.count(n) > 1})
    assert not twice, f"declared more than once at the top level: {twice}"


def test_the_shuffling_figure_is_the_icon_with_its_eyes_shut(page: str) -> None:
    """Derived from FIGURE at load, so each replacement has to find its mark:
    a silent miss would leave the speed lines on, or no shut eyes to swap in."""
    figure = page.split("const FIGURE = `", 1)[1].split("`;", 1)[0]
    assert figure.count('class="rev"') == 1
    assert re.search(r'<g>\s*<path class="streak"[\s\S]*?</g>', figure)
    assert len(re.findall(r'<path class="shade"[^>]*/>', figure)) == 1
    assert len(re.findall(r'<path class="sheen"[^>]*/>', figure)) == 1
    assert figure.count('<g class="eyes">') == 1
    assert len(re.findall(r'<circle class="pupil" cx="32"[^>]*/>', figure)) == 1
    assert len(re.findall(r'<ellipse class="mouth"[^>]*/>', figure)) == 1
    derived = page.split("const SHUFFLE_FIGURE = FIGURE", 1)[1].split(";\n", 1)[0]
    for part in ("sh-figure", "sh-bliss", "sh-sleep", "sh-face", "sh-o", "sh-line"):
        assert part in derived, f"{part} is not put into the shuffling figure"


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



# --------------------------------------------------------------------------- #
# a revival's loose ends
# --------------------------------------------------------------------------- #
def test_escape_during_a_revival_skips_the_show_and_launches(page: str) -> None:
    """Escape used to close the window during the wait and lose the revival."""
    assert 'if (revival) revival.hurry();\n      else $("shut").click();' in page
    body = _revive_handler(page)
    assert "hurry: () => { hurry(); if (show) show.stop(); }," in body
    ended = body.split("finished();", 1)[1]
    assert "(show ? show.ended : Promise.resolve()).then(() => {" in ended
    assert ended.index('el.raise.dataset.busy = "false";') > ended.index("show.ended")
    assert "if (revival === mine) revival = null;" in ended, "Escape skips until the cards land"


def test_closing_during_a_revival_sends_it_first(page: str) -> None:
    shut = page.split('$("shut").addEventListener("click", async () => {', 1)[1].split("\n  });", 1)[0]
    assert shut.index("revival.hurry();") < shut.index("bridge.destroy()")
    assert "setTimeout(done, 8000)" in shut, "a hung request cannot keep the window open for good"


def test_every_request_gives_up_in_the_end(page: str) -> None:
    """A hung service used to leave REVIVE reading RAISING for good."""
    assert "const quit = new AbortController();" in page
    assert "signal: quit.signal" in page
    body = _revive_handler(page)
    assert "}, 60000);" in body
    assert 'e.name === "AbortError"' in body


def test_a_session_raised_a_moment_ago_is_not_marked_again(page: str) -> None:
    """Until its new process registers as live it looks dead, and a reload used to
    mark it again for a second click to open twice."""
    assert "chosen = new Set(sessions.filter((s) => !s.live && !freshlyRaised(s.sessionId))" in page
    success = _revive_handler(page).split("if (result.ok) {", 1)[1].split("} else {", 1)[0]
    assert "raised.set(id, Date.now());" in success
    assert "for (const id of result.raised || ids) {" in success, "only what was actually raised"


def test_the_deal_draws_what_the_terminal_really_opens(page: str) -> None:
    """A console with no tabs opens windows; the switch and the deal both say so."""
    assert "opens = data.layouts || {};" in page
    assert "node.dataset.demoted = String(short);" in page
    assert ".seg[data-demoted=true]{" in page
    dealer = _dealer(page)
    assert "layout: drawn(layout)," in dealer
    assert 'word(drawn(layout) === "tabs"' in dealer



def test_the_deal_does_not_let_clicks_through_to_hidden_rows(page: str) -> None:
    """Marking a row nobody can see would change a revival already under way."""
    stage = page.split(".sh-stage{", 1)[1].split("}", 1)[0]
    assert "pointer-events:none" not in stage
    assert 'el.raise.dataset.busy === "true" ? "RAISING"' in page



_MOUTH_HARNESS = r"""
const made = [];
const fake = () => {
  const el = {
    style: { setProperty() {} }, dataset: {}, textContent: "", innerHTML: "", attrs: {}, kids: {},
    clientWidth: 902, clientHeight: 398,
    classList: { set: new Set(), contains(c) { return this.set.has(c); },
                 toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); } },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] || ""; },
    appendChild() {}, querySelector(sel) { return (this.kids[sel] = this.kids[sel] || fake()); },
    querySelectorAll: () => [fake(), fake()],
  };
  let first = null;
  Object.defineProperty(el, "firstChild", { get: () => (first = first || fake()) });
  made.push(el);
  return el;
};
globalThis.document = { createElement: fake };
globalThis.requestAnimationFrame = () => 0;
const stillness = () => false;
%ENGINE%
const host = fake();
const show = startShuffle(host, { count: 8, layout: "tabs", figure: "", agents: Array(8).fill("claude-code"),
                                  names: Array(8).fill("x"), below: 48 });
const figure = made.find((el) => el.className === "sh-ghost");
const o = figure.kids[".sh-o"], line = figure.kids[".sh-line"];
const at = (t) => { show.render(t); return { rx: Number(o.attrs.rx), ry: Number(o.attrs.ry),
                                            line: Number(line.style.opacity), d: line.attrs.d }; };
console.log(JSON.stringify({ wait: at(200), sing: at(700), deal: at(1800), landed: at(2700) }));
"""


def test_the_mouth_goes_level_then_o_then_smile(page: str, tmp_path) -> None:
    """A level line while it waits, an O while it nods, a smile once it deals."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    engine = page.split("  const SHUFFLE_FROM = 6;", 1)[1].split("  // Lays the stage over the register", 1)[0]
    script = tmp_path / "mouth.js"
    script.write_text(_MOUTH_HARNESS.replace("%ENGINE%", engine), encoding="utf-8")
    ran = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr.strip()
    import json
    seen = json.loads(ran.stdout)

    def bend(d: str) -> float:
        numbers = [float(n) for n in re.findall(r"-?[\d.]+", d)]
        return numbers[3] - numbers[1]  # control point below the ends is a smile

    assert seen["wait"]["rx"] == 0 and seen["wait"]["line"] == 1 and abs(bend(seen["wait"]["d"])) < 0.01
    assert seen["sing"]["rx"] > 1.5 and seen["sing"]["line"] < 0.05
    assert seen["deal"]["rx"] < 0.05 and seen["deal"]["line"] == 1 and bend(seen["deal"]["d"]) > 1
    assert bend(seen["landed"]["d"]) > bend(seen["deal"]["d"]), "wider when the cards have landed"


def test_the_dealing_figure_keeps_the_icons_three_colours(page: str) -> None:
    """Orange, black and white, as in the icon: no gradients, no fourth colour."""
    derived = page.split("const SHUFFLE_FIGURE = FIGURE", 1)[1].split(";\n", 1)[0]
    assert "Gradient" not in derived and "<defs" not in derived
    styles = page.split("/* ── the shuffle", 1)[1].split("/* ── states", 1)[0]
    figure_rules = re.findall(r"(\.sh-(?:ghost|o|line|bliss)(?![\w-])[^{]*)\{([^}]*)\}", styles)
    assert figure_rules
    allowed = {"currentcolor", "#000", "#fff", "#f4efe6", "#191715", "none", "var(--ember)"}
    for selector, body in figure_rules:
        for prop, value in re.findall(r"(fill|stroke|color|background)\s*:\s*([^;]+)", body):
            assert value.strip().lower() in allowed, f"{selector.strip()} {prop}: {value}"


_RISE_HARNESS = r"""
const made = [];
const fake = (tag) => {
  const el = {
    tag, style: { setProperty() {} }, dataset: {}, textContent: "", innerHTML: "", attrs: {}, kids: {},
    clientWidth: 902, clientHeight: 398, className: "",
    classList: { set: new Set(), contains(c) { return this.set.has(c); },
                 toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); } },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] || ""; },
    appendChild() {}, querySelector(sel) { return (this.kids[sel] = this.kids[sel] || fake()); },
    querySelectorAll: () => [fake(), fake()],
  };
  let first = null;
  Object.defineProperty(el, "firstChild", { get: () => (first = first || fake()) });
  made.push(el);
  return el;
};
globalThis.document = { createElement: fake };
globalThis.requestAnimationFrame = () => 0;
const stillness = () => false;
%ENGINE%
const host = fake();
const show = startShuffle(host, { count: 10, layout: "tabs", figure: "", agents: Array(10).fill("claude-code"),
                                  names: Array(10).fill("x"), below: 48 });
const figure = made.find((el) => el.className === "sh-ghost");
const at = (el) => (el.style.transform.match(/translate\(([-\d.]+)px, ([-\d.]+)px\)/) || []).slice(1).map(Number);
const order = (cls) => made.findIndex((el) => el.className.startsWith(cls));
const hazes = made.filter((el) => el.className.startsWith("sh-mist"));
const sleep = figure.kids[".sh-sleep"], body = figure.kids[".body"];
const out = { order: { mist: order("sh-mist"), figure: order("sh-ghost"), card: order("sh-card"),
                       lastMist: made.lastIndexOf(hazes[hazes.length - 1]) },
              widths: hazes.map((el) => parseFloat(el.style.width)) };
show.render(20);
out.start = { y: at(figure)[1], mask: figure.style.maskImage, asleep: sleep.style.display !== "none" };
show.render(200);
out.mid = { mist: Math.max(...hazes.map((el) => Number(el.style.opacity))), mask: figure.style.maskImage };
show.render(520);
out.up = { y: at(figure)[1], mask: figure.style.maskImage, asleep: sleep.style.display !== "none",
           body: body.attrs.d };
show.render(1400);
out.later = { mist: Math.max(...hazes.map((el) => Number(el.style.opacity))) };
// a hit on the beat pulls the hem out of shape, and it is the drawing again at rest
let bent = 0;
for (let t = 520; t < 1400; t += 8) {
  show.render(t);
  const corner = Number((body.attrs.d.match(/^M([-\d.]+) 42/) || [])[1]);
  bent = Math.max(bent, Math.abs(corner - 4));
}
out.bent = bent;
show.fail();
show.render(show.END / show.PACE);
out.gone = Math.max(...hazes.map((el) => Number(el.style.opacity)));
console.log(JSON.stringify(out));
"""


def test_the_figure_rises_out_of_grave_mist(page: str, tmp_path) -> None:
    """It used to pop out of a puff smaller than itself, so it was seen appearing
    behind it. Now it comes up out of the floor through mist wider than it is,
    asleep, and wakes as it clears it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    engine = page.split("  const SHUFFLE_FROM = 6;", 1)[1].split("  // Lays the stage over the register", 1)[0]
    script = tmp_path / "rise.js"
    script.write_text(_RISE_HARNESS.replace("%ENGINE%", engine), encoding="utf-8")
    ran = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr.strip()
    seen = json.loads(ran.stdout)
    # Behind the figure and in front of it. The cards gathering from the rows
    # pass behind both on purpose, so none flies across its face; dealt cards
    # go over everything.
    assert seen["order"]["mist"] < seen["order"]["figure"] < seen["order"]["lastMist"] < seen["order"]["card"]
    layers = {name: int(z) for name, z in re.findall(r"\.sh-(ghost|mist\.sh-near)\{[^}]*z-index:(\d+)", page)}
    assert 20 + 12 < layers["ghost"] < layers["mist.sh-near"] < 60
    body_width = 36 * 150 / 58
    banks = [w for w in seen["widths"] if w > 200]
    assert len(banks) >= 4 and min(banks) > 2 * body_width, "every bank is wider than the figure"
    # Down in the ground at first, fading out into the mist, and asleep.
    assert seen["up"]["y"] - seen["start"]["y"] < -70
    assert "linear-gradient" in seen["start"]["mask"] and "linear-gradient" in seen["mid"]["mask"]
    assert seen["start"]["asleep"] is True
    # Out, with nothing left cutting it off, awake.
    assert seen["up"]["mask"] == "" and seen["up"]["asleep"] is False
    corner = float(re.match(r"M([-\d.]+) 42", seen["up"]["body"]).group(1))
    assert abs(corner - 4) < 0.1, "at rest the sheet hangs as drawn"
    # Stirred up while it rises, a thin ground mist after, gone when the scene is.
    assert seen["mid"]["mist"] > 0.5
    assert 0.1 < seen["later"]["mist"] < seen["mid"]["mist"] * 0.6
    assert seen["gone"] == 0
    # The sheet trails the head on the beat, and not by much.
    assert 0.3 < seen["bent"] < 2.5


def test_the_mist_is_drawn_while_the_page_is_idle(page: str) -> None:
    """All four textures together are a long task, which would be dropped frames
    on the click, so the page draws one each time it is idle."""
    assert "requestIdleCallback(fn, { timeout: 5000 })" in page
    later = page.split("const mistLater = () => {", 1)[1].split("};", 1)[0]
    assert "if (stillness() || mists.length >= MIST_PLAN.length) return;" in later
    assert later.count("paintMist(") == 1, "one texture per idle turn"
    assert "const MIST = mist();" in _shuffle_engine(page)
    assert 'const ctx = canvas.getContext ? canvas.getContext("2d") : null;' in page
    assert 'if (!ctx) return "";' in page


_MIST_HARNESS = r"""
%ENGINE%
const out = MIST_PLAN.map((plan) => {
  const [, w, h] = plan;
  const a = mistField(...plan), b = mistField(...plan);
  let edge = 0, most = 0, same = a.length === b.length;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const v = a[y * w + x];
      if (v !== b[y * w + x]) same = false;
      if (x === 0 || y === 0 || x === w - 1 || y === h - 1) edge = Math.max(edge, v);
      most = Math.max(most, v);
    }
  }
  return { w, h, edge, most, same, size: a.length };
});
console.log(JSON.stringify(out));
"""


def test_no_edge_of_the_mist_ever_shows(page: str, tmp_path) -> None:
    """A texture that is not empty at its border shows as a straight edge in the
    fog. The same seed gives the same mist, and there is mist to see."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    engine = page.split("  const SHUFFLE_FROM = 6;", 1)[1].split("  // Lays the stage over the register", 1)[0]
    script = tmp_path / "mist.js"
    script.write_text(_MIST_HARNESS.replace("%ENGINE%", engine), encoding="utf-8")
    ran = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr.strip()
    textures = json.loads(ran.stdout)
    assert len(textures) == 4
    for tex in textures:
        assert tex["size"] == tex["w"] * tex["h"]
        assert tex["edge"] == 0, tex
        assert tex["most"] > 0.3 * 255, tex
        assert tex["same"] is True


def test_the_mist_glow_is_not_the_tab_glow(page: str) -> None:
    """The mist's ember glow was first given the class the tabs already use for
    their flash, which blew every tab's flash up to the glow's size and left the
    glow itself without a colour."""
    styles = page.split("/* ── the shuffle", 1)[1].split("/* ── states", 1)[0]
    assert '<b class="sh-glow"></b>' in page
    assert 'const glow = add("i", "sh-ember");' in page
    ember = [body for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", styles) if ".sh-ember" in sel]
    assert any("radial-gradient" in body for body in ember)
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", styles):
        if ".sh-glow" in sel:
            assert "width" not in body and "height" not in body, sel


def test_enter_revives_even_with_a_row_focused(page: str) -> None:
    """After a click the row has focus, and Enter used to toggle it back instead
    of reviving. Space still marks a row, as it presses any button."""
    row_keys = page.split('node.addEventListener("keydown", (event) => {', 1)[1].split("});", 1)[0]
    assert 'event.key === "Enter"' in row_keys and "el.raise.click()" in row_keys
    assert "event.preventDefault();" in row_keys.split('event.key === "Enter"', 1)[1]


def test_a_row_says_what_the_session_is_called(page: str) -> None:
    """The agent's own name for a session beats its last prompt, which is often
    just "continue". The service already picks the best of the two."""
    assert "escape(s.summary || s.lastPrompt || s.firstPrompt" in page
    assert "Every session in this window is still running" not in page, "running sessions are listed, held"


def test_a_recorded_frame_never_asks_for_a_revival(page: str) -> None:
    """The README's recording of the deal is made by asking the page for frozen
    frames. Whatever the page does in that mode, it must not press REVIVE or
    talk to the service, or recording the demo would open terminals."""
    body = page.split("  function freeze(at) {", 1)[1].split("\n  }\n", 1)[0]
    assert "window.requestAnimationFrame = () => 0;" in body, "the scene's own clock never starts"
    for forbidden in ("api(", "el.raise", ".click(", "/api/"):
        assert forbidden not in body, forbidden
    assert "deal([...chosen], null)" in body, "no window to place, so no /api/place either"
    assert "if (FRAME === null) guard = setTimeout" in page
    build = (PAGE.parents[1] / "assets" / "src" / "build.py").read_text(encoding="utf-8")
    assert "backend.revive = refuse" in build, "the recording's service refuses a revival outright"
