"""Repairing Cloudflare-gated indexers from the search screen.

The rule under test is which failures are worth offering to repair. A tracker
behind Cloudflare comes back in one keystroke; a tracker that is simply down
does not, and promising otherwise trains people to ignore the offer.
"""

import asyncio

from trrnt import onboard
from trrnt.tui import TGetApp


def _app():
    from test_downloads_table import FakeConfig

    app = TGetApp(FakeConfig({
        "vpn": {"enabled": False},
        "security": {"scan_on_complete": False, "clamav_enabled": False,
                     "quarantine_dir": "/tmp/tget-test-quarantine"},
        "plex": {"enabled": False},
        "jackett": {"url": "http://localhost:9117", "api_key": ""},
        "solver": {"enabled": True, "port": 8191},
        "aria2": {"rpc_url": "http://localhost:6800/jsonrpc"},
        "display": {"max_results": 50, "home": False},
        "categories": {},
    }))
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    app.push_home_screen = lambda *a, **k: None
    app._run_kill_switch = lambda *a, **k: None
    return app


def _run(body):
    async def go():
        app = _app()
        async with app.run_test(size=(110, 40)) as pilot:
            return await body(app, pilot)
    return asyncio.run(go())


# ── Which failures are repairable ─────────────────────────────────────────────

def test_cloudflare_failures_are_repairable():
    async def body(app, _pilot):
        app.jackett.last_errors = [("1337x", "FlareSolverr is not configured")]
        return app._repairable()

    assert _run(body) == ["1337x"]


def test_a_timeout_counts_as_repairable():
    """A gated tracker often shows up as a stall, not as a stated error.

    Jackett sits on the request while a solve it cannot perform drags on, and
    trrnt's per-indexer ceiling fires first.
    """
    async def body(app, _pilot):
        app.jackett.last_errors = [("kickasstorrents-ws", "timed out after 30s")]
        return app._repairable()

    assert _run(body) == ["kickasstorrents-ws"]


def test_known_gated_trackers_are_repairable_whatever_the_reason():
    async def body(app, _pilot):
        app.jackett.last_errors = [(next(iter(onboard.NEEDS_SOLVER)), "HTTP 503")]
        return app._repairable()

    assert len(_run(body)) == 1


def test_a_dead_tracker_is_not_offered_as_repairable():
    """0magnet answers with a parse error because the site is broken.

    No amount of Cloudflare clearance fixes that, so it must not appear in an
    offer to repair.
    """
    async def body(app, _pilot):
        app.jackett.last_errors = [("0magnet", "Parse error")]
        return app._repairable()

    assert _run(body) == []


def test_repairable_and_dead_failures_are_separated():
    async def body(app, _pilot):
        app.jackett.last_errors = [
            ("1337x", "FlareSolverr is not configured"),
            ("0magnet", "Parse error"),
        ]
        return app._repairable()

    assert _run(body) == ["1337x"]


# ── What the toast offers ─────────────────────────────────────────────────────

def _toast_for(errors, installed):
    async def body(app, _pilot):
        import trrnt.tui as tui

        toasts = []
        app.notify = lambda msg, **kw: toasts.append(msg)
        original, tui.solver.installed = tui.solver.installed, lambda: installed
        try:
            async def fake_search(query, **kw):
                app.jackett.last_errors = list(errors)
                return []

            app.jackett.search = fake_search
            await app.run_search.__wrapped__(app, "query")
        finally:
            tui.solver.installed = original
        return [t for t in toasts if t.startswith("Skipping")]

    return _run(body)


def test_a_gated_indexer_offers_the_repair_key():
    toasts = _toast_for([("1337x", "FlareSolverr is not configured")], installed=True)

    assert len(toasts) == 1
    assert "^y to repair" in toasts[0]


def test_without_a_solver_installed_the_toast_stays_generic():
    """Offering ^y with nothing behind it is worse than not offering."""
    toasts = _toast_for([("1337x", "FlareSolverr is not configured")], installed=False)

    assert len(toasts) == 1
    assert "^y" not in toasts[0]
    assert "^n" in toasts[0]


def test_a_dead_tracker_points_at_the_indexers_screen():
    toasts = _toast_for([("0magnet", "Parse error")], installed=True)

    assert len(toasts) == 1
    assert "^n" in toasts[0]


# ── The repair action ─────────────────────────────────────────────────────────

def test_repair_without_a_solver_says_how_to_get_one():
    async def body(app, _pilot):
        import trrnt.tui as tui

        toasts = []
        app.notify = lambda msg, **kw: toasts.append(msg)
        original, tui.solver.installed = tui.solver.installed, lambda: False
        try:
            await app._repair_indexers.__wrapped__(app)
        finally:
            tui.solver.installed = original
        return toasts

    toasts = _run(body)
    assert any("trrnt setup" in t for t in toasts)


def test_repair_with_everything_healthy_says_so():
    """Pressing ^y on a working setup must not spin up a browser."""
    async def body(app, _pilot):
        import trrnt.tui as tui

        toasts = []
        app.notify = lambda msg, **kw: toasts.append(msg)
        spawned = []

        class FakeAdmin:
            def __init__(self, url):
                pass

            async def login(self, password=None):
                pass

            async def configured_ids(self):
                return set()  # nothing configured at all

            async def close(self):
                pass

        originals = (tui.solver.installed, tui.onboard.JackettAdmin,
                     tui.solver.SolverProcess)
        tui.solver.installed = lambda: True
        tui.onboard.JackettAdmin = FakeAdmin
        tui.solver.SolverProcess = lambda **kw: spawned.append(kw)
        try:
            await app._repair_indexers.__wrapped__(app)
        finally:
            (tui.solver.installed, tui.onboard.JackettAdmin,
             tui.solver.SolverProcess) = originals
        return toasts, spawned

    toasts, spawned = _run(body)
    assert any("Nothing to repair" in t for t in toasts)
    assert spawned == []


# ── readmitting repaired indexers ─────────────────────────────────────────────

def test_a_repaired_indexer_comes_off_the_exclude_list(tmp_path):
    """The one that most needs repair is the one already switched off.

    People exclude a tracker because it kept failing. Fixing it without
    readmitting it means the repair changes nothing they can see.
    """
    from trrnt.config import Config

    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    config.reload()
    from trrnt import onboard as ob
    ob.write_config_values(config.path, {
        ("jackett", "exclude_indexers"): ["1337x", "0magnet"],
    })
    config.reload()

    async def body(app, _pilot):
        app.config = config
        app.notify = lambda msg, **kw: None
        app._readmit(["1337x"])
        return config.get("jackett", "exclude_indexers")

    assert _run(body) == ["0magnet"]


def test_readmitting_leaves_unrelated_exclusions_alone(tmp_path):
    from trrnt.config import Config
    from trrnt import onboard as ob

    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    config.reload()
    ob.write_config_values(config.path, {
        ("jackett", "exclude_indexers"): ["someprivatetracker"],
    })
    config.reload()

    async def body(app, _pilot):
        app.config = config
        app.notify = lambda msg, **kw: None
        app._readmit(["1337x"])  # never excluded in the first place
        return config.get("jackett", "exclude_indexers")

    assert _run(body) == ["someprivatetracker"]


def test_repair_widens_a_ceiling_too_tight_for_a_gated_tracker(tmp_path):
    """A 12s cap repairs 1337x into timing out on every single search.

    Warm — no solving involved — 1337x answered in 14.1s and 14.4s on
    consecutive runs, because the site is slow. Below that floor the repair
    is theatre.
    """
    from trrnt.config import Config
    from trrnt import onboard as ob, solver as sol

    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    config.reload()
    ob.write_config_values(config.path, {
        ("jackett", "exclude_indexers"): ["1337x"],
        ("jackett", "indexer_timeout"): 12,
    })
    config.reload()

    async def body(app, _pilot):
        app.config = config
        app.notify = lambda msg, **kw: None
        app._timeout_raised = None
        app._readmit(["1337x"])
        return config.get("jackett", "indexer_timeout"), app._timeout_raised

    timeout, raised = _run(body)
    assert timeout == sol.MIN_INDEXER_TIMEOUT
    assert raised == (12, sol.MIN_INDEXER_TIMEOUT)


def test_a_generous_ceiling_is_left_alone(tmp_path):
    """Only ever widened to the floor — never narrowed, never overridden."""
    from trrnt.config import Config
    from trrnt import onboard as ob

    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    config.reload()
    ob.write_config_values(config.path, {
        ("jackett", "exclude_indexers"): ["1337x"],
        ("jackett", "indexer_timeout"): 90,
    })
    config.reload()

    async def body(app, _pilot):
        app.config = config
        app.notify = lambda msg, **kw: None
        app._timeout_raised = None
        app._readmit(["1337x"])
        return config.get("jackett", "indexer_timeout"), app._timeout_raised

    assert _run(body) == (90, None)


def test_repair_with_no_prior_failure_checks_every_configured_indexer():
    """A parked domain is on nobody's known-gated list.

    Pressing ^y out of the blue has to look at everything, or the one tracker
    that quietly moved is the one never examined.
    """
    async def body(app, _pilot):
        import trrnt.tui as tui

        app.notify = lambda msg, **kw: None
        primed = []

        class FakeAdmin:
            def __init__(self, url):
                pass

            async def login(self, password=None):
                pass

            async def configured_ids(self):
                return {"thepiratebay", "0magnet", "1337x"}

            async def set_flaresolverr(self, url, max_timeout_ms=None):
                pass

            async def wait_until_up(self, timeout=45.0):
                return True

            async def close(self):
                pass

        class FakeProc:
            url = "http://127.0.0.1:8191"

            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return None

        async def fake_prime(admin, ids, on_result=None):
            primed.extend(ids)
            return [(i, "ok", "") for i in ids]

        originals = (tui.solver.installed, tui.onboard.JackettAdmin,
                     tui.solver.SolverProcess, tui.solver.prime)
        tui.solver.installed = lambda: True
        tui.onboard.JackettAdmin = FakeAdmin
        tui.solver.SolverProcess = FakeProc
        tui.solver.prime = fake_prime
        try:
            await app._repair_indexers.__wrapped__(app)
        finally:
            (tui.solver.installed, tui.onboard.JackettAdmin,
             tui.solver.SolverProcess, tui.solver.prime) = originals
        return sorted(primed)

    assert _run(body) == ["0magnet", "1337x", "thepiratebay"]
