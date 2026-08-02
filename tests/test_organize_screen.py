"""The organize prompt: prefill, live folder tracking, and dismissal.

Mounted for real, like the downloads-table tests — the input plumbing
(Changed events re-deriving the folder, the touched-folder guard) is exactly
the part a unit test of the parser can't see.
"""

import asyncio

from textual.widgets import Input

from trrnt.search import TorrentResult
from trrnt.tui import OrganizeChoice, OrganizeScreen, TGetApp


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


RESULT = TorrentResult(
    title="www.Torrenting.com.The.Agency.S02E03.2160p.WEB.DDP5.1.SKIZ",
    magnet="magnet:?xt=urn:btih:00",
    category="tv",
)


def _app():
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
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    # These tests live on the working screen; the landing page has its own.
    app.push_home_screen = lambda *a, **k: None
    return app


def _run(body):
    async def go():
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            return await body(app, pilot)
    return asyncio.run(go())


def test_prompt_prefills_clean_name_and_folder(tmp_path):
    async def body(app, pilot):
        screen = OrganizeScreen(RESULT, str(tmp_path))
        app.push_screen(screen)
        await pilot.pause()
        return (
            screen.query_one("#organize-name", Input).value,
            screen.query_one("#organize-folder", Input).value,
        )

    name, folder = _run(body)
    assert name == "The Agency S02E03"
    assert folder == str(tmp_path / "The Agency" / "Season 2")


def test_folder_tracks_the_typed_name(tmp_path):
    async def body(app, pilot):
        screen = OrganizeScreen(RESULT, str(tmp_path))
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#organize-name", Input).value = "The Agency s03e01"
        await pilot.pause()
        return screen.query_one("#organize-folder", Input).value

    assert _run(body) == str(tmp_path / "The Agency" / "Season 3")


def test_hand_edited_folder_stops_tracking(tmp_path):
    async def body(app, pilot):
        screen = OrganizeScreen(RESULT, str(tmp_path))
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#organize-folder", Input).value = "/somewhere/else"
        await pilot.pause()
        screen.query_one("#organize-name", Input).value = "Renamed Entirely"
        await pilot.pause()
        return screen.query_one("#organize-folder", Input).value

    assert _run(body) == "/somewhere/else"


def test_enter_saves_and_escape_skips(tmp_path):
    async def body(app, pilot):
        outcomes = []
        first = OrganizeScreen(RESULT, str(tmp_path))
        app.push_screen(first, outcomes.append)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        second = OrganizeScreen(RESULT, str(tmp_path))
        app.push_screen(second, outcomes.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        return outcomes

    saved, skipped = _run(body)
    assert isinstance(saved, OrganizeChoice)
    assert saved.organize
    assert saved.name == "The Agency S02E03"
    assert saved.folder == str(tmp_path / "The Agency" / "Season 2")
    assert skipped is None
