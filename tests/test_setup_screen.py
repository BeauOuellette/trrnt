"""The setup wizard's riskiest steps, mounted for real.

Each test drives one step coroutine directly (run_wizard is neutered) with
a real Config against a tmp file — the wizard's whole job is writing that
file correctly, so a FakeConfig would test nothing. The Jackett admin is a
fake injected through the screen's factory seam; the step logic, widget
plumbing, and the answer-Future dance are the code under test.
"""

import asyncio
import time

from textual.widgets import Input, RadioButton, SelectionList

from trrnt import onboard
from trrnt.config import Config
from trrnt.onboard import JackettAdminError
from trrnt.tui import SetupScreen, TGetApp


class FakeAdmin:
    def __init__(self, catalog=None, configured=(), needs_password=False,
                 cloudflare=(), broken=()):
        self.catalog_data = catalog or []
        self.configured = set(configured)
        self.needs_password = needs_password
        self.cloudflare = set(cloudflare)
        self.broken = set(broken)
        self.added = []
        self.tested = []
        self.closed = False
        self.flaresolverr = None

    async def set_flaresolverr(self, url, max_timeout_ms=None):
        self.flaresolverr = (url, max_timeout_ms)

    async def wait_until_up(self, timeout=45.0):
        return True

    async def test_indexer(self, indexer_id):
        self.tested.append(indexer_id)
        if indexer_id in self.cloudflare:
            return "cloudflare", "needs FlareSolverr"
        if indexer_id in self.broken:
            return "error", "site down"
        return "ok", ""

    async def login(self, password=None):
        if self.needs_password and password is None:
            raise JackettAdminError("password required")

    async def catalog(self):
        return self.catalog_data

    async def configured_ids(self):
        return set(self.configured)

    async def add_indexer(self, indexer_id):
        self.added.append(indexer_id)

    async def close(self):
        self.closed = True


CATALOG = [
    # curated and reachable bare
    {"id": "thepiratebay", "name": "TPB", "type": "public", "configured": False},
    {"id": "therarbg", "name": "TheRARBG", "type": "public", "configured": False},
    # curated but Cloudflare-gated (in NEEDS_SOLVER)
    {"id": "1337x", "name": "1337x", "type": "public", "configured": False},
    # public but not curated
    {"id": "obscure", "name": "Obscure", "type": "public", "configured": False},
    # never offered
    {"id": "priv", "name": "Priv", "type": "private", "configured": False},
]


class NullAria2:
    """An aria2 with nothing in it.

    These tests need a *real* Config — they assert on what gets written to
    config.yaml — and a real Config points at the real aria2 with
    security.scan_on_complete on. That combination had the suite polling a
    live daemon and running the completion scan over genuinely downloading
    files, which ends in scanner.quarantine() moving them. Tests do not get
    to touch the user's downloads.
    """

    download_dir = "/tmp"

    async def get_active(self):
        return []

    async def get_waiting(self, *a, **k):
        return []

    async def get_stopped(self, *a, **k):
        return []

    async def get_global_stat(self):
        return {"downloadSpeed": "0", "uploadSpeed": "0",
                "numActive": "0", "numWaiting": "0"}

    async def check_connection(self):
        return False


def _app(tmp_path):
    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    config.reload()
    app = TGetApp(config)
    app.aria2 = NullAria2()
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    app.push_home_screen = lambda *a, **k: None
    app._run_kill_switch = lambda *a, **k: None
    return app


def _screen(admin=None):
    screen = SetupScreen(admin_factory=(lambda url: admin) if admin else None)
    screen.run_wizard = lambda *a, **k: None  # steps are driven by the tests
    return screen


async def _reply_when_asked(pilot, screen, value, timeout=5.0, previous=None):
    """Wait for the step to park on a *new* answer Future, then resolve it.

    Identity matters: the just-answered Future is still on the screen for a
    tick after it resolves, so a plain "is not None" check would fire at the
    old one, _reply would no-op on a done Future, and the next prompt would
    wait forever. Pass the previous Future to wait past it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = screen._answer
        if current is not None and current is not previous:
            screen._reply(value)
            return current
        await pilot.pause()
        await asyncio.sleep(0.02)
    raise AssertionError("step never asked for input")


def _run(tmp_path, body):
    async def go():
        app = _app(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            return await body(app, pilot)
    return asyncio.run(go())


# ── indexers step ─────────────────────────────────────────────────────────────

def test_quickpick_preselects_only_indexers_that_work_bare(tmp_path):
    """Cloudflare-gated picks are offered but off by default.

    Ticking 1337x by default would hand a first-run user an indexer that
    returns nothing until they install FlareSolverr — the exact silent
    failure this wizard exists to prevent.
    """
    admin = FakeAdmin(catalog=CATALOG)

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.ensure_future(screen._step_indexers())
            first = await _reply_when_asked(pilot, screen, ("button", "setup-add"))
            picker = screen.query_one(SelectionList)
            selected = list(picker.selected)
            labels = [str(o.prompt) for o in picker._options]
            # the health report pauses on a Continue button
            await _reply_when_asked(pilot, screen, ("button", "setup-continue"),
                                    previous=first)
            mark = await asyncio.wait_for(task, timeout=5)
            return mark, selected, labels
        return run()

    mark, selected, labels = _run(tmp_path, body)
    assert mark == "✓"
    assert set(selected) == {"thepiratebay", "therarbg"}
    assert set(admin.added) == {"thepiratebay", "therarbg"}
    # 1337x is still offered, just labelled and unticked.
    assert any("1337x" in l and "FlareSolverr" in l for l in labels)
    # The private indexer is never a choice at all.
    assert not any("Priv" in l for l in labels)
    assert admin.closed


def test_health_report_names_cloudflare_indexers(tmp_path):
    """After adding, the user is told what actually answers."""
    admin = FakeAdmin(catalog=CATALOG, cloudflare={"therarbg"})

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.ensure_future(screen._step_indexers())
            first = await _reply_when_asked(pilot, screen, ("button", "setup-add"))
            await _reply_when_asked(pilot, screen, ("button", "setup-continue"),
                                    previous=first)
            await asyncio.wait_for(task, timeout=5)
            return str(screen.query_one("#setup-detail").render())
        return run()

    detail = _run(tmp_path, body)
    assert set(admin.tested) == {"thepiratebay", "therarbg"}
    assert "1 of 2 responding" in detail
    assert "therarbg" in detail and "Cloudflare" in detail


def test_indexers_already_configured_short_circuits(tmp_path):
    admin = FakeAdmin(catalog=CATALOG, configured={"1337x"})

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            return await asyncio.wait_for(screen._step_indexers(), timeout=5)
        return run()

    assert _run(tmp_path, body) == "✓"
    assert admin.added == []


def test_password_gate_can_be_skipped(tmp_path):
    admin = FakeAdmin(catalog=CATALOG, needs_password=True)

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.ensure_future(screen._step_indexers())
            await _reply_when_asked(pilot, screen, ("button", "setup-skip"))
            return await asyncio.wait_for(task, timeout=5)
        return run()

    assert _run(tmp_path, body) == "–"
    assert admin.added == []


# ── vpn step ──────────────────────────────────────────────────────────────────

class FakeVPN:
    enabled = True

    def __init__(self, iface=None):
        self._iface = iface

    def find_vpn_interface(self):
        return self._iface

    async def check(self):
        class Status:
            connected = bool(self._iface)
            interface = self._iface
            vpn_ip = "185.0.0.1" if self._iface else ""
            error = ""
        return Status()

    def on_vpn_drop(self, cb):
        pass


def test_vpn_keep_writes_enforcement_on(tmp_path):
    def body(app, pilot):
        async def run():
            app.vpn = FakeVPN("utun4")
            screen = _screen()
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.ensure_future(screen._step_vpn())
            await _reply_when_asked(pilot, screen, ("button", "setup-continue"))
            return await asyncio.wait_for(task, timeout=5)
        return run()

    assert _run(tmp_path, body) == "✓"
    text = (tmp_path / "config.yaml").read_text()
    assert "  enabled: true" in text[text.index("vpn:"):]
    assert "bt_interface: \"none\"" not in text


def test_vpn_opt_out_writes_none_binding(tmp_path):
    def body(app, pilot):
        async def run():
            app.vpn = FakeVPN(None)
            screen = _screen()
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.ensure_future(screen._step_vpn())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                buttons = screen.query(RadioButton)
                if buttons:
                    break
                await pilot.pause()
                await asyncio.sleep(0.02)
            screen.query_one("#setup-vpn-off", RadioButton).value = True
            await pilot.pause()
            await _reply_when_asked(pilot, screen, ("button", "setup-continue"))
            return await asyncio.wait_for(task, timeout=5)
        return run()

    assert _run(tmp_path, body) == "✓"
    text = (tmp_path / "config.yaml").read_text()
    vpn_part = text[text.index("vpn:"):]
    assert "  enabled: false" in vpn_part.split("plex:")[0] or "  enabled: false" in vpn_part
    aria_part = text[text.index("aria2:"):]
    assert 'bt_interface: "none"' in aria_part


# ── api key step ──────────────────────────────────────────────────────────────

def test_api_key_auto_read_writes_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        onboard, "read_jackett_server_config",
        lambda: {"APIKey": "k123", "Port": 9200},
    )

    def body(app, pilot):
        async def run():
            screen = _screen()
            app.push_screen(screen)
            await pilot.pause()
            return await asyncio.wait_for(screen._step_api_key(), timeout=5)
        return run()

    assert _run(tmp_path, body) == "✓"
    text = (tmp_path / "config.yaml").read_text()
    jackett_part = text[text.index("jackett:"):text.index("aria2:")]
    assert '  api_key: "k123"' in jackett_part
    assert '  url: "http://localhost:9200"' in jackett_part


# ── solver step ───────────────────────────────────────────────────────────────

class FakeSolverProcess:
    """Stands in for a real FlareSolverr, without the 190MB or the browser."""

    started = 0

    def __init__(self, port=8191):
        self.url = f"http://127.0.0.1:{port}"

    async def __aenter__(self):
        FakeSolverProcess.started += 1
        return self

    async def __aexit__(self, *_exc):
        return None


def _fake_solver(monkeypatch, installed=True):
    from trrnt import solver as real
    import trrnt.tui as tui

    FakeSolverProcess.started = 0
    monkeypatch.setattr(tui.solver, "installed", lambda: installed)
    monkeypatch.setattr(tui.solver, "SolverProcess", FakeSolverProcess)
    monkeypatch.setattr(tui.solver, "install", _unexpected_install)
    return real


async def _unexpected_install(on_line=None):
    raise AssertionError("install must not run when the solver is present")


def test_solver_step_skips_when_nothing_is_gated(tmp_path, monkeypatch):
    """A setup with only bare-reachable indexers should not be sold a browser."""
    _fake_solver(monkeypatch)
    admin = FakeAdmin(configured={"thepiratebay", "therarbg"})

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            return await asyncio.wait_for(screen._step_solver(), timeout=5)
        return run()

    assert _run(tmp_path, body) == "–"
    assert admin.flaresolverr is None
    assert FakeSolverProcess.started == 0


def test_solver_step_falls_back_to_known_gated_indexers(tmp_path, monkeypatch):
    """The indexers step short-circuits on an existing setup and measures nothing.

    The offer still has to appear, so it comes from the list of trackers known
    to sit behind Cloudflare rather than from a measurement that never ran.
    """
    _fake_solver(monkeypatch)
    admin = FakeAdmin(configured={"thepiratebay", "1337x"})

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.create_task(screen._step_solver())
            await _reply_when_asked(pilot, screen, ("button", "setup-solver-yes"))
            await _reply_when_asked(pilot, screen, ("button", "setup-continue"),
                                    previous=screen._answer)
            return await asyncio.wait_for(task, timeout=10)
        return run()

    assert _run(tmp_path, body) == "✓"
    assert admin.tested == ["1337x"]
    assert FakeSolverProcess.started == 1


def test_declining_the_solver_leaves_jackett_untouched(tmp_path, monkeypatch):
    _fake_solver(monkeypatch)
    admin = FakeAdmin(configured={"1337x"})

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            task = asyncio.create_task(screen._step_solver())
            await _reply_when_asked(pilot, screen, ("button", "setup-skip"))
            return await asyncio.wait_for(task, timeout=10)
        return run()

    assert _run(tmp_path, body) == "–"
    assert admin.flaresolverr is None
    assert admin.tested == []
    assert FakeSolverProcess.started == 0


def test_accepting_the_solver_wires_jackett_and_records_it(tmp_path, monkeypatch):
    _fake_solver(monkeypatch)
    admin = FakeAdmin(configured={"1337x"})

    def body(app, pilot):
        async def run():
            screen = _screen(admin)
            app.push_screen(screen)
            await pilot.pause()
            screen._blocked_indexers = ["1337x"]
            task = asyncio.create_task(screen._step_solver())
            await _reply_when_asked(pilot, screen, ("button", "setup-solver-yes"))
            await _reply_when_asked(pilot, screen, ("button", "setup-continue"),
                                    previous=screen._answer)
            return await asyncio.wait_for(task, timeout=10)
        return run()

    assert _run(tmp_path, body) == "✓"
    # Jackett has to be told where the solver is, and given a ceiling high
    # enough that a slow cold solve is not cut off one second early.
    url, timeout_ms = admin.flaresolverr
    assert url.startswith("http://127.0.0.1:")
    assert timeout_ms == 120_000
    text = (tmp_path / "config.yaml").read_text()
    assert "solver:" in text
    assert "  enabled: true" in text


# ── leaving ───────────────────────────────────────────────────────────────────

def test_escape_leaves_the_wizard(tmp_path):
    def body(app, pilot):
        async def run():
            screen = _screen()
            app.push_screen(screen)
            await pilot.pause()
            assert isinstance(app.screen, SetupScreen)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(app.screen, SetupScreen)
        return run()

    assert not _run(tmp_path, body)
