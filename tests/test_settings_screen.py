"""The settings screen, mounted for real.

Against a real Config on disk, because the round-trip through the
comment-preserving YAML writer is the part that a unit test of the validators
cannot see — and the part that would silently drop a key.
"""

import asyncio
import shutil
from pathlib import Path

from textual.widgets import Checkbox, Input, RadioSet

from trrnt.config import Config
from trrnt.tui import SettingsScreen, TGetApp, _format_uptime


EXAMPLE = Path(__file__).parent.parent / "src" / "trrnt" / "config.example.yaml"


class FakeConfig:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, *keys, default=None):
        obj = self._data
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
                if obj is None:
                    return default
            else:
                return default
        return obj

    @property
    def data(self):
        return self._data


class FakeAria2:
    """Records what the screen pushes, so the tier split can be asserted on."""

    def __init__(self, alive=True, interface="utun4"):
        self.alive = alive
        self.interface = interface
        self.pushed = []

    async def check_connection(self):
        return self.alive

    async def get_global_option(self):
        return {"interface": self.interface} if self.interface else {}

    async def change_global_option(self, options):
        self.pushed.append(options)
        return "OK"


class FakeDaemon:
    def __init__(self, owns=True, uptime=225.0):
        self.owns_daemon = owns
        self._uptime = uptime

    def uptime_seconds(self):
        return self._uptime


def _app(aria2=None):
    app = TGetApp(FakeConfig({
        "vpn": {"enabled": False},
        "security": {"scan_on_complete": False, "clamav_enabled": False,
                     "quarantine_dir": "/tmp/tget-test-quarantine"},
        "plex": {"enabled": False},
        "jackett": {"url": "http://localhost:9117", "api_key": ""},
        "aria2": {"rpc_url": "http://localhost:6800/jsonrpc"},
        "display": {"max_results": 50},
        "categories": {},
    }))
    app.aria2 = aria2 or FakeAria2()
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    app.push_home_screen = lambda *a, **k: None
    return app


def _config(tmp_path) -> Config:
    path = tmp_path / "config.yaml"
    shutil.copy(EXAMPLE, path)
    return Config(path)


def _run(body, aria2=None):
    async def go():
        app = _app(aria2)
        app.notices = []
        app.notify = lambda msg, **kw: app.notices.append(str(msg))
        async with app.run_test(size=(120, 45)) as pilot:
            return await body(app, pilot)
    return asyncio.run(go())


def test_prefills_every_field_from_config(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        return {
            "down": screen.query_one("#set-max_download_rate", Input).value,
            "ratio": screen.query_one("#set-seed_ratio", Input).value,
            "port": screen.query_one("#set-listen_port", Input).value,
            "lpd": screen.query_one("#set-enable_lpd", Checkbox).value,
            "enc": screen.query_one("#set-encryption", RadioSet).pressed_index,
        }

    values = _run(body)
    assert values["down"] == "0"
    assert values["ratio"] == "2.0"
    assert values["port"] == "6881-6999"
    assert values["lpd"] is False
    assert values["enc"] == 1  # "prefer"


def test_save_round_trips_through_yaml(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#set-max_upload_rate", Input).value = "500K"
        screen.query_one("#set-seed_time", Input).value = "30"
        await pilot.pause()
        screen._save()
        await pilot.pause()

    _run(body)

    reread = Config(tmp_path / "config.yaml")
    assert reread.get("aria2", "max_upload_rate") == "500K"
    assert reread.get("aria2", "seed_time") == "30"
    # Untouched neighbours survive the line-based writer.
    assert reread.get("aria2", "listen_port") == "6881-6999"
    assert reread.get("aria2", "download_dir")


def test_comments_survive_a_save(tmp_path):
    """The config comments are the documentation — a dump-and-rewrite eats them."""
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#set-max_download_rate", Input).value = "2M"
        await pilot.pause()
        screen._save()
        await pilot.pause()

    _run(body)
    text = (tmp_path / "config.yaml").read_text()
    assert "seed_ratio: 0   seed FOREVER" in text
    assert "# Jackett settings" in text


def test_invalid_port_blocks_the_save(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#set-listen_port", Input).value = "80"
        await pilot.pause()
        screen._save()
        await pilot.pause()
        return app.notices

    notices = _run(body)
    assert any("Listen port" in n for n in notices)
    # Nothing was written, so the file still holds the original.
    assert Config(tmp_path / "config.yaml").get("aria2", "listen_port") == "6881-6999"


def test_live_push_omits_restart_tier_options(tmp_path):
    """aria2 would answer OK to listen-port and change nothing — sending it
    would turn a no-op into a false success message."""
    config = _config(tmp_path)
    aria2 = FakeAria2()

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#set-max_upload_rate", Input).value = "1M"
        screen.query_one("#set-listen_port", Input).value = "6881"
        await pilot.pause()
        screen._save()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    _run(body, aria2=aria2)

    assert aria2.pushed, "expected the live tier to be pushed"
    pushed = aria2.pushed[0]
    assert pushed["max-overall-upload-limit"] == "1M"
    assert "listen-port" not in pushed
    assert "dht-listen-port" not in pushed


def test_restart_tier_change_says_so(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#set-listen_port", Input).value = "6881"
        await pilot.pause()
        screen._save()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return app.notices

    notices = _run(body)
    assert any("relaunch" in n for n in notices)


def test_unchanged_save_pushes_nothing(tmp_path):
    config = _config(tmp_path)
    aria2 = FakeAria2()

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        screen._save()
        await pilot.pause()
        await app.workers.wait_for_complete()

    _run(body, aria2=aria2)
    assert aria2.pushed == []


def test_seed_note_tracks_the_typed_values(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        note = screen.query_one("#settings-seed-note")

        screen.query_one("#set-seed_ratio", Input).value = "0"
        await pilot.pause()
        forever = str(note.content)

        screen.query_one("#set-seed_time", Input).value = "0"
        await pilot.pause()
        never = str(note.content)
        return forever, never

    forever, never = _run(body)
    assert "forever" in forever
    assert "Never seeds" in never


def test_header_reports_binding_uptime_and_ownership(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon(owns=True, uptime=225.0))
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return (
            str(screen.query_one("#settings-daemon").content),
            str(screen.query_one("#settings-ownership").content),
        )

    header, ownership = _run(body)
    assert "aria2 running" in header
    assert "utun4" in header
    assert "up 3m" in header
    assert "stops when you quit" in ownership


def test_header_calls_out_a_dead_daemon(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon())
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return str(screen.query_one("#settings-daemon").content)

    header = _run(body, aria2=FakeAria2(alive=False))
    assert "not responding" in header


def test_adopted_daemon_says_it_is_left_running(tmp_path):
    config = _config(tmp_path)

    async def body(app, pilot):
        screen = SettingsScreen(config, FakeDaemon(owns=False))
        app.push_screen(screen)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return str(screen.query_one("#settings-ownership").content)

    assert "left running" in _run(body)


def test_format_uptime():
    assert _format_uptime(None) == ""
    assert _format_uptime(45) == "45s"
    assert _format_uptime(225) == "3m"
    assert _format_uptime(3600) == "1h"
    assert _format_uptime(7860) == "2h 11m"
