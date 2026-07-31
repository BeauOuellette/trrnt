"""The downloads table renders through the real TUI.

_update_downloads swallows every exception so a broken aria2 doesn't crash
the app — which also means a column/row mismatch would silently blank the
table instead of raising. Mounting it for real is the only way to catch that.
"""

import asyncio

import pytest

from torrentcli.download import DownloadStatus
from torrentcli.tui import TGetApp


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
    """Serves one active torrent and nothing else."""

    download_dir = "/tmp"

    def __init__(self, active):
        self._active = active

    async def get_active(self):
        return self._active

    async def get_waiting(self, *a, **k):
        return []

    async def get_stopped(self, *a, **k):
        return []

    async def get_global_stat(self):
        return {"downloadSpeed": "0", "uploadSpeed": "0"}

    async def get_files(self, gid):
        return []


ACTIVE = DownloadStatus(
    gid="abc123", status="active", name="Some Torrent",
    total_bytes=1_000_000, completed_bytes=500_000,
    download_speed=1024, seeders=9, connections=12,
)


def _app():
    # scan_on_complete off so the refresh doesn't try to scan anything.
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
    app.aria2 = FakeAria2([ACTIVE])
    # These mount-time workers shell out (clamd probe, VPN check); neither is
    # under test and their subprocesses outlive the event loop.
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    return app


def test_the_seeds_column_reaches_the_rendered_row():
    async def go():
        app = _app()
        async with app.run_test():
            await app._update_downloads()
            table = app.query_one("#downloads-table")
            assert table.row_count == 1
            row = [str(c) for c in table.get_row_at(0)]
            # Name, Size, Progress, Seeds, Speed, ETA, Status
            assert row[3] == "9/12"
            return row

    row = asyncio.run(go())
    assert row[0] == "Some Torrent"


def test_every_column_gets_a_value():
    """A mismatch here would be swallowed and blank the table."""
    async def go():
        app = _app()
        async with app.run_test():
            table = app.query_one("#downloads-table")
            columns = len(table.columns)
            await app._update_downloads()
            return columns, table.row_count, len(table.get_row_at(0))

    columns, rows, cells = asyncio.run(go())
    assert rows == 1, "row was dropped — column count and row width disagree"
    assert cells == columns == 7
