"""Managing indexers after setup: exclude, test, remove.

The screen exists because a Cloudflare-gated tracker fails on every single
search, and the fix used to live in Jackett's web UI or a YAML file. The
load-bearing behaviour is that a toggle actually reaches config.yaml *and*
the live search client — a change that only takes effect next launch would
send people right back to the web UI.
"""

import asyncio
import time

import yaml
from textual.widgets import SelectionList

from trrnt.config import Config
from trrnt.onboard import JackettAdminError
from trrnt.tui import IndexersScreen, TGetApp

from test_setup_screen import NullAria2


class FakeAdmin:
    def __init__(self, configured=(), cloudflare=(), fail_login=False):
        self.configured = list(configured)
        self.cloudflare = set(cloudflare)
        self.fail_login = fail_login
        self.deleted = []
        self.tested = []

    async def login(self, password=None):
        if self.fail_login:
            raise JackettAdminError("unreachable")

    async def catalog(self):
        return [
            {"id": i, "name": i.upper(), "type": "public", "configured": True}
            for i in self.configured
        ]

    async def configured_ids(self):
        return set(self.configured)

    async def test_indexer(self, indexer_id):
        self.tested.append(indexer_id)
        if indexer_id in self.cloudflare:
            return "cloudflare", "needs FlareSolverr"
        return "ok", ""

    async def delete_indexer(self, indexer_id):
        self.deleted.append(indexer_id)
        self.configured = [i for i in self.configured if i != indexer_id]

    async def close(self):
        pass


def _app(tmp_path):
    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    config.reload()
    app = TGetApp(config)
    # A real Config points at the real aria2; without this the suite polls a
    # live daemon and can run the completion scan over the user's actual
    # downloads. See NullAria2's docstring in test_setup_screen.
    app.aria2 = NullAria2()
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    app.push_home_screen = lambda *a, **k: None
    app._run_kill_switch = lambda *a, **k: None
    return app


def _run(tmp_path, admin, body):
    async def go():
        app = _app(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            screen = IndexersScreen(admin_factory=lambda url: admin)
            app.push_screen(screen)
            await pilot.pause()
            deadline = time.monotonic() + 3
            while not screen._rows and time.monotonic() < deadline:
                await pilot.pause()
                await asyncio.sleep(0.02)
            return await body(app, pilot, screen)
    return asyncio.run(go())


def _excluded_in_file(tmp_path):
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    return data["jackett"].get("exclude_indexers", [])


def test_lists_only_configured_indexers(tmp_path):
    admin = FakeAdmin(configured=["1337x", "thepiratebay"])

    def body(app, pilot, screen):
        async def run():
            return [r["id"] for r in screen._rows]
        return run()

    assert sorted(_run(tmp_path, admin, body)) == ["1337x", "thepiratebay"]


def test_unticking_writes_exclude_list_and_updates_live_client(tmp_path):
    """The toggle must reach both config.yaml and the running search client."""
    admin = FakeAdmin(configured=["1337x", "thepiratebay"])

    def body(app, pilot, screen):
        async def run():
            picker = screen.query_one(SelectionList)
            picker.deselect(next(o for o in picker._options if o.value == "1337x"))
            await pilot.pause()
            return app.jackett.exclude_indexers
        return run()

    live = _run(tmp_path, admin, body)
    assert _excluded_in_file(tmp_path) == ["1337x"]
    # Without this the change would only apply on the next launch.
    assert live == {"1337x"}


def test_reticking_clears_the_exclusion(tmp_path):
    admin = FakeAdmin(configured=["1337x", "thepiratebay"])

    def body(app, pilot, screen):
        async def run():
            picker = screen.query_one(SelectionList)
            option = next(o for o in picker._options if o.value == "1337x")
            picker.deselect(option)
            await pilot.pause()
            picker.select(option)
            await pilot.pause()
            return None
        return run()

    _run(tmp_path, admin, body)
    assert _excluded_in_file(tmp_path) == []


def test_previously_excluded_indexers_start_unticked(tmp_path):
    admin = FakeAdmin(configured=["1337x", "thepiratebay"])
    config = Config(tmp_path / "config.yaml")
    config.ensure_config_exists()
    from trrnt.onboard import write_config_values
    write_config_values(tmp_path / "config.yaml",
                        {("jackett", "exclude_indexers"): ["1337x"]})

    def body(app, pilot, screen):
        async def run():
            return list(screen.query_one(SelectionList).selected)
        return run()

    assert _run(tmp_path, admin, body) == ["thepiratebay"]


def test_test_all_marks_cloudflare(tmp_path):
    admin = FakeAdmin(configured=["1337x", "thepiratebay"],
                      cloudflare={"1337x"})

    def body(app, pilot, screen):
        async def run():
            screen.action_test_all()
            deadline = time.monotonic() + 3
            while (screen._health.get("1337x", ("", ""))[0] != "cloudflare"
                   and time.monotonic() < deadline):
                await pilot.pause()
                await asyncio.sleep(0.02)
            return screen._health, str(screen.query_one("#indexers-status").render())
        return run()

    health, status = _run(tmp_path, admin, body)
    assert health["1337x"][0] == "cloudflare"
    assert health["thepiratebay"][0] == "ok"
    assert "1 behind Cloudflare" in status


def test_remove_deletes_from_jackett(tmp_path):
    admin = FakeAdmin(configured=["1337x", "thepiratebay"])

    def body(app, pilot, screen):
        async def run():
            picker = screen.query_one(SelectionList)
            picker.highlighted = 0  # rows sort by name: 1337X first
            target = screen._rows[0]["id"]
            screen.action_remove_indexer()
            deadline = time.monotonic() + 3
            while admin.deleted == [] and time.monotonic() < deadline:
                await pilot.pause()
                await asyncio.sleep(0.02)
            await pilot.pause()
            return target, [r["id"] for r in screen._rows]
        return run()

    target, remaining = _run(tmp_path, admin, body)
    assert admin.deleted == [target]
    assert target not in remaining


def test_unreachable_jackett_says_so(tmp_path):
    admin = FakeAdmin(configured=["1337x"], fail_login=True)

    def body(app, pilot, screen):
        async def run():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                text = str(screen.query_one("#indexers-status").render())
                if "Cannot reach" in text:
                    return text
                await pilot.pause()
                await asyncio.sleep(0.02)
            return str(screen.query_one("#indexers-status").render())
        return run()

    assert "Cannot reach Jackett" in _run(tmp_path, admin, body)
