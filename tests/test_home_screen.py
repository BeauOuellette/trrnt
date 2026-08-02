"""The landing screen: when it shows, when it steps aside, what it hands off.

Mounted for real, like the other TUI tests. The skip rule is the part that
earns the harness — it depends on the aria2 probe, the input's live value,
and the dismissal callback agreeing about why the screen went away. The
bubbling guard matters just as much: Input.Submitted travels past the
screen's handler to the app's, and without the stop every home search would
fire twice.
"""

import asyncio
import time

from textual.widgets import DataTable, Input, Static

from trrnt.branding import MASCOT
from trrnt.tui import HomeScreen, TGetApp

from test_downloads_table import FakeConfig


def _app(*, stat=None, stat_gate=None, stat_error=False, home=True):
    display = {"max_results": 50}
    if not home:
        display["home"] = False
    app = TGetApp(FakeConfig({
        "vpn": {"enabled": False},
        "security": {"scan_on_complete": False, "clamav_enabled": False,
                     "quarantine_dir": "/tmp/tget-test-quarantine"},
        "plex": {"enabled": False},
        "jackett": {"url": "http://localhost:9117", "api_key": ""},
        "aria2": {"rpc_url": "http://localhost:6800/jsonrpc"},
        "display": display,
        "categories": {},
    }))
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None

    async def fake_stat():
        if stat_gate is not None:
            await stat_gate.wait()
        if stat_error:
            raise ConnectionError("aria2 unreachable")
        return stat or {"numActive": "0", "numWaiting": "0"}

    async def fake_clam():
        return {"installed": True, "daemon_running": True, "version": "test"}

    app.aria2.get_global_stat = fake_stat
    app.security.check_clamav_available = fake_clam

    searches = []
    app.run_search = searches.append
    app.searches = searches
    return app


def _run(app, body):
    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return await body(app, pilot)
    return asyncio.run(go())


async def _settle(pilot, cond, timeout=2.0):
    """Pump the app until cond() holds or the timeout runs out."""
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.02)
    return cond()


def test_idle_launch_lands_on_home():
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        # Hold a few ticks: the screen must also *stay* — nothing is moving.
        await asyncio.sleep(0.1)
        await pilot.pause()
        return isinstance(app.screen, HomeScreen)

    assert _run(_app(), body)


def test_disabled_by_config_boots_straight_to_work():
    async def body(app, pilot):
        return isinstance(app.screen, HomeScreen)

    assert not _run(_app(home=False), body)


def test_stands_its_ground_when_downloads_are_moving():
    """The screen used to step aside here. It must not any more.

    A seeding torrent counts as active, so with any seed ratio set this was
    nearly every launch — the home screen was effectively unreachable.
    """
    app = _app(stat={"numActive": "2", "numWaiting": "0"})

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await asyncio.sleep(0.2)  # several probe ticks, any of which used to skip
        await pilot.pause()
        return isinstance(app.screen, HomeScreen)

    assert _run(app, body)


def test_escape_lands_on_the_downloads_table_when_something_is_moving():
    app = _app(stat={"numActive": "2", "numWaiting": "0"})

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await _settle(pilot, lambda: app.screen._moving > 0)
        await pilot.press("escape")
        await _settle(pilot, lambda: not isinstance(app.screen, HomeScreen))
        return app.focused

    focused = _run(app, body)
    assert isinstance(focused, DataTable)
    assert focused.id == "downloads-table"


def test_stays_put_when_aria2_is_down():
    async def body(app, pilot):
        await asyncio.sleep(0.1)
        await pilot.pause()
        home = app.screen
        part, moving = await home._probe_aria2()
        return isinstance(home, HomeScreen), part, moving

    still_home, part, moving = _run(_app(stat_error=True), body)
    assert still_home
    assert "down" in part
    assert moving == 0


def test_enter_hands_the_query_to_exactly_one_search():
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await pilot.press("d", "u", "n", "e", "enter")
        await _settle(pilot, lambda: not isinstance(app.screen, HomeScreen))
        return app.query_one("#search-input", Input).value

    value = _run(app, body)
    # One search, not two: the home handler stops the Submitted event before
    # it bubbles to the app's handler.
    assert app.searches == ["dune"]
    assert value == "dune"


def test_escape_browses_without_searching():
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await pilot.press("escape")
        await _settle(pilot, lambda: not isinstance(app.screen, HomeScreen))
        return app.focused

    focused = _run(app, body)
    assert app.searches == []
    assert isinstance(focused, Input)
    assert focused.id == "search-input"


def test_a_mid_type_query_survives_the_probe():
    # The probe used to be able to yank the screen away mid-keystroke. It no
    # longer dismisses at all, so the typed query must simply still be there.
    gate = asyncio.Event()
    app = _app(stat={"numActive": "2", "numWaiting": "0"}, stat_gate=gate)

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await pilot.press("d", "u")
        gate.set()
        await asyncio.sleep(0.1)
        await pilot.pause()
        return (isinstance(app.screen, HomeScreen),
                app.screen.query_one("#home-search", Input).value)

    still_home, typed = _run(app, body)
    assert still_home
    assert typed == "du"


def test_organize_style_inputs_never_reach_the_search():
    # The app-level guard: a Submitted event from a foreign input (the
    # organize prompt's fields bubble exactly like this) must not search.
    app = _app(home=False)

    async def body(app, pilot):
        search = app.query_one("#search-input", Input)
        foreign = Input(id="organize-name")
        await app.screen.mount(foreign)
        foreign.value = "The Agency Season 2"
        foreign.focus()
        await pilot.press("enter")
        await pilot.pause()
        search.value = "real query"
        search.focus()
        await pilot.press("enter")
        await pilot.pause()
        return None

    _run(app, body)
    assert app.searches == ["real query"]


def test_home_meta_names_the_jackett_host():
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        return str(app.screen.query_one("#home-meta").render())

    meta = _run(_app(), body)
    assert "localhost:9117" in meta


# ── update check ──────────────────────────────────────────────────────────────

def _home(app):
    return app.screen


def test_update_line_shows_outdated_formulas(monkeypatch):
    from trrnt import onboard

    monkeypatch.setattr(onboard, "brew_path", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(onboard, "read_update_cache", lambda: {})
    monkeypatch.setattr(onboard, "write_update_cache", lambda d: None)
    monkeypatch.setattr(
        onboard, "brew_outdated",
        lambda f: ("0.24.1385", "0.24.2307") if f == "jackett" else None,
    )
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        home = app.screen
        await _settle(pilot, lambda: bool(home._outdated))
        return str(home.query_one("#home-update").render())

    line = _run(app, body)
    assert "jackett 0.24.1385 → 0.24.2307" in line
    assert "^u" in line


def test_no_update_line_when_current(monkeypatch):
    from trrnt import onboard

    monkeypatch.setattr(onboard, "brew_path", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(onboard, "read_update_cache", lambda: {})
    monkeypatch.setattr(onboard, "write_update_cache", lambda d: None)
    monkeypatch.setattr(onboard, "brew_outdated", lambda f: None)
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await asyncio.sleep(0.1)
        await pilot.pause()
        return str(app.screen.query_one("#home-update").render())

    assert _run(app, body).strip() == ""


def test_cached_check_does_not_shell_out(monkeypatch):
    """A daily cache is the point — brew must not run on every launch."""
    import time as _time

    from trrnt import onboard

    calls = []
    monkeypatch.setattr(onboard, "brew_path", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(onboard, "read_update_cache", lambda: {
        "checked_at": _time.time(), "outdated": {"aria2": ["1.0", "2.0"]}})
    monkeypatch.setattr(onboard, "write_update_cache", lambda d: None)
    monkeypatch.setattr(onboard, "brew_outdated",
                        lambda f: calls.append(f) or None)
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        home = app.screen
        await _settle(pilot, lambda: bool(home._outdated))
        return str(home.query_one("#home-update").render())

    line = _run(app, body)
    assert calls == [], "ran brew despite a fresh cache"
    assert "aria2 1.0 → 2.0" in line


def test_upgrade_key_runs_upgrade_and_reports(monkeypatch):
    from trrnt import onboard

    upgraded = []
    state = {"outdated": True}

    async def fake_upgrade(formula, on_line=None):
        upgraded.append(formula)
        state["outdated"] = False
        return True

    monkeypatch.setattr(onboard, "brew_path", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(onboard, "read_update_cache", lambda: {})
    monkeypatch.setattr(onboard, "write_update_cache", lambda d: None)
    monkeypatch.setattr(
        onboard, "brew_outdated",
        lambda f: ("0.1", "0.2") if (f == "jackett" and state["outdated"]) else None,
    )
    monkeypatch.setattr(onboard, "upgrade_formula", fake_upgrade)
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        home = app.screen
        await _settle(pilot, lambda: bool(home._outdated))
        await pilot.press("ctrl+u")
        await _settle(pilot, lambda: not home._outdated and not home._upgrading)
        return str(home.query_one("#home-update").render())

    line = _run(app, body)
    assert upgraded == ["jackett"]
    # Re-checked after upgrading rather than assuming success.
    assert line.strip() == ""


def test_ctrl_u_still_clears_the_search_box_when_nothing_to_update(monkeypatch):
    """The binding is priority, so it must not steal Input's clear-line."""
    from trrnt import onboard

    monkeypatch.setattr(onboard, "brew_path", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(onboard, "read_update_cache", lambda: {})
    monkeypatch.setattr(onboard, "write_update_cache", lambda d: None)
    monkeypatch.setattr(onboard, "brew_outdated", lambda f: None)
    app = _app()

    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await pilot.press("d", "u", "n", "e")
        box = app.screen.query_one("#home-search", Input)
        assert box.value == "dune"
        await pilot.press("ctrl+u")
        await pilot.pause()
        return box.value

    assert _run(app, body) == ""


# ── which mark leads the page ─────────────────────────────────────────────────

def _run_sized(app, body, size):
    async def go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await body(app, pilot)
    return asyncio.run(go())


def _logo(screen) -> str:
    return str(screen.query_one("#home-logo", Static).content)


def _is_wordmark(text: str) -> bool:
    """The wordmark is drawn in full blocks; the mask never uses them."""
    return "█" in text


def test_a_roomy_window_leads_with_the_mask():
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        return _logo(app.screen)

    text = _run_sized(_app(), body, (120, 44))
    assert not _is_wordmark(text)
    assert len(text.splitlines()) == len(MASCOT)


def test_a_short_window_falls_back_to_the_wordmark():
    """The mask plus a search box does not fit; a landing page you cannot
    type into is worse than a smaller logo."""
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        return _logo(app.screen)

    assert _is_wordmark(_run_sized(_app(), body, (120, 30)))


def test_a_narrow_window_falls_back_to_the_wordmark():
    """The mask is a fixed 52 columns wide — it cannot be cut down."""
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        return _logo(app.screen)

    assert _is_wordmark(_run_sized(_app(), body, (50, 44)))


def test_resizing_swaps_the_mark():
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        before = _logo(app.screen)
        await pilot.resize_terminal(120, 28)
        await pilot.pause()
        after = _logo(app.screen)
        return before, after

    before, after = _run_sized(_app(), body, (120, 44))
    assert not _is_wordmark(before)
    assert _is_wordmark(after), "shrinking should fall back to the wordmark"


def test_the_title_names_the_product():
    """With the wordmark gone, this line is the only thing that says trrnt."""
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        return str(app.screen.query_one("#home-tagline", Static).content)

    title = _run_sized(_app(), body, (120, 44))
    assert "trrnt" in title
    assert "terminal torrent aggregator" in title


def test_the_mask_never_pushes_the_hints_off_screen():
    """The height gate's whole job. At two rows less the mask still fits but
    the hints line falls off the bottom, which is the failure it prevents."""
    async def body(app, pilot):
        await _settle(pilot, lambda: isinstance(app.screen, HomeScreen), timeout=0.3)
        await asyncio.sleep(0.1)
        await pilot.pause()
        hints = app.screen.query_one("#home-hints", Static)
        return _is_wordmark(_logo(app.screen)), hints.region.y + hints.region.height

    for height in (30, 38, 39, 40, 44, 50):
        wordmark, bottom = _run_sized(_app(), body, (120, height))
        assert bottom <= height, (
            f"hints clipped at {height} lines (ends at {bottom}, "
            f"showing {'wordmark' if wordmark else 'mask'})")
