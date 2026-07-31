"""The tables re-fit themselves to the terminal width.

_update_downloads swallows every exception so a broken aria2 doesn't crash
the app — which also means a column/row mismatch would silently blank the
table instead of raising. Mounting it for real is the only way to catch that.
"""

import asyncio

import pytest

from torrentcli.download import DownloadStatus
from torrentcli.search import TorrentResult
from torrentcli.tui import (
    _FOOTER_LEFT,
    _FOOTER_RIGHT,
    SOURCE_MAX,
    TGetApp,
    fit_name,
    fit_source,
)


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


LONG = "The Agency 2024 S02E02 A Bear in Wolfs Clothing 2160p PMTP WEB-DL DDP5 1 DV"

ACTIVE = DownloadStatus(
    gid="abc123", status="active", name=LONG,
    total_bytes=1_000_000, completed_bytes=500_000,
    download_speed=1024, seeders=9, connections=12,
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
    app.aria2 = FakeAria2([ACTIVE])
    # These mount-time workers shell out (clamd probe, VPN check); neither is
    # under test and their subprocesses outlive the event loop.
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    return app


def _run(size, body):
    async def go():
        app = _app()
        async with app.run_test(size=size):
            return await body(app)
    return asyncio.run(go())


# ── fit_name ──────────────────────────────────────────────────────────────────

def test_fit_name_keeps_the_distinguishing_tail():
    """Releases differ at the end, so a plain right-cut loses the only part
    that separates two rows."""
    out = fit_name(LONG, 52)
    assert len(out) == 52
    assert out.endswith("DDP5 1 DV")
    assert out.startswith("The Agency")
    assert "…" in out


def test_fit_name_leaves_short_names_alone():
    assert fit_name("Short.Release.mkv", 52) == "Short.Release.mkv"


def test_two_near_identical_releases_stay_distinguishable():
    a = fit_name("The Agency 2024 S02E02 A Bear in Wolfs Clothing 2160p PMTP WEB-DL DDP5 1 DV", 52)
    b = fit_name("The Agency 2024 S02E02 A Bear in Wolfs Clothing 2160p AMZN WEB-DL DDP5 1 H", 52)
    assert a != b, "truncation collapsed two different releases into one string"


# ── downloads table ───────────────────────────────────────────────────────────

def test_narrow_window_drops_the_status_column():
    async def body(app):
        await app._update_downloads()
        t = app.query_one("#downloads-table")
        return [str(c.label) for c in t.columns.values()], t.row_count
    cols, rows = _run((102, 40), body)
    assert "Status" not in cols
    assert cols == ["Name", "Size", "Progress", "Seeds", "Speed", "ETA"]
    assert rows == 1, "row was dropped — column count and row width disagree"


def test_wide_window_keeps_the_status_column():
    async def body(app):
        await app._update_downloads()
        t = app.query_one("#downloads-table")
        return [str(c.label) for c in t.columns.values()], t.row_count
    cols, rows = _run((206, 40), body)
    assert cols[-1] == "Status"
    assert rows == 1


def test_the_seeds_column_reaches_the_rendered_row():
    async def body(app):
        await app._update_downloads()
        t = app.query_one("#downloads-table")
        return [str(c) for c in t.get_row_at(0)]
    row = _run((102, 40), body)
    assert row[3] == "9/12"


def test_every_column_gets_a_value_at_both_widths():
    """A mismatch here would be swallowed and blank the table."""
    async def body(app):
        await app._update_downloads()
        t = app.query_one("#downloads-table")
        return len(t.columns), len(t.get_row_at(0))
    for size in ((102, 40), (206, 40)):
        cols, cells = _run(size, body)
        assert cols == cells, f"{size[0]} cols: {cols} columns vs {cells} cells"


def test_the_download_name_is_cut_to_fit_the_window():
    async def body(app):
        await app._update_downloads()
        t = app.query_one("#downloads-table")
        return str(t.get_row_at(0)[0])
    narrow = _run((102, 40), body)
    wide = _run((206, 40), body)
    assert len(narrow) < len(wide), "narrow window should show a shorter name"
    assert len(narrow) <= 60


# ── results table ─────────────────────────────────────────────────────────────

def _result(title):
    return TorrentResult(title=title, size_bytes=5_000_000_000, seeders=49,
                         leechers=117, indexer="The Pirate Bay", magnet="magnet:?x")


def test_results_name_column_grows_with_the_window():
    async def body(app):
        app.search_results = [_result(LONG)]
        app._render_results()
        t = app.query_one("#results-table")
        return str(t.get_row_at(0)[2])
    narrow = _run((102, 40), body)
    wide = _run((206, 40), body)
    assert len(narrow) < len(wide), "wide window should show more of the name"
    assert wide.endswith("DDP5 1 DV")


def test_resizing_refits_the_results_without_losing_the_row():
    async def body(app):
        app.search_results = [_result(LONG), _result("Another Release 1080p")]
        app._render_results()
        before = app.query_one("#results-table").row_count
        await app.on_resize(None)
        return before, app.query_one("#results-table").row_count
    before, after = _run((102, 40), body)
    assert before == after == 2


# ── no horizontal scrolling, ever ─────────────────────────────────────────────

WIDE_SOURCE = "The Pirate Bay"


def _busy_results(n=46):
    """Worst case: long titles, four-digit peer counts, long indexer name."""
    out = []
    for i in range(n):
        r = _result(
            "The Super Mario Galaxy Movie 2026 2160p UHD BluRay REMUX "
            "DV HDR10+ TrueHD Atmos 7.1-UnKn0wn"
        )
        r.seeders, r.leechers = 1534, 1828
        r.indexer = WIDE_SOURCE
        r.size_bytes = 58_800_000_000
        out.append(r)
    return out


@pytest.mark.parametrize("width", [80, 102, 120, 140, 206])
def test_no_horizontal_scrollbar_at_any_width(width):
    """The user never wants to scroll sideways. overflow-x is hidden, so this
    guards the stronger property: the scrollbar is never even wanted."""
    async def body(app):
        app.search_results = _busy_results()
        app._render_results()
        await app._update_downloads()
        out = {}
        for tid in ("#results-table", "#downloads-table"):
            t = app.query_one(tid)
            out[tid] = (t.show_horizontal_scrollbar,
                        t.virtual_size.width, t.size.width)
        return out
    got = _run((width, 40), body)
    for tid, (bar, virtual, visible) in got.items():
        assert bar is False, f"{tid} shows a horizontal scrollbar at {width}"
        assert virtual <= visible, (
            f"{tid} content is {virtual} wide in a {visible} viewport at {width} cols")


def test_source_is_truncated_so_the_row_fits():
    async def body(app):
        app.search_results = _busy_results(3)
        app._render_results()
        t = app.query_one("#results-table")
        return str(t.get_row_at(0)[6])
    src = _run((102, 40), body)
    assert len(src) <= SOURCE_MAX
    assert src == "The Pirate…", "an ellipsis marks it as cut, not broken"


def test_short_indexer_names_are_left_alone():
    assert fit_source("Jackett") == "Jackett"
    assert fit_source("x" * SOURCE_MAX) == "x" * SOURCE_MAX


# ── the key bar ───────────────────────────────────────────────────────────────

def test_footer_order_and_right_aligned_keys():
    """The order is muscle memory, so it is pinned, not left to Textual's
    binding-collection order."""
    async def body(app):
        bar = app.query_one("#key-bar")
        return bar.render().plain
    line = _run((102, 40), body)
    assert line.index("Download") < line.index("Clear Done") < \
           line.index("Remove") < line.index("Quit") < line.index("Keys")
    assert line.rstrip().endswith("^k Keys"), "Keys must sit on the right edge"
    assert line.lstrip().startswith("^d Download")


def test_pause_all_is_not_in_the_footer():
    """It lives in the ^k overlay only. It previously appeared right-aligned
    because ctrl+p is Textual's command-palette key."""
    async def body(app):
        return app.query_one("#key-bar").render().plain
    assert "Pause" not in _run((102, 40), body)


def test_the_command_palette_is_off_so_ctrl_p_is_ours():
    assert TGetApp.ENABLE_COMMAND_PALETTE is False


def test_the_bar_spans_the_window():
    async def body(app):
        return app.query_one("#key-bar").render().plain
    for width in (102, 206):
        line = _run((width, 40), body)
        assert len(line) == width - 1, f"bar is {len(line)} at width {width}"


def test_a_narrow_window_drops_keys_rather_than_wrapping():
    async def body(app):
        return app.query_one("#key-bar").render().plain
    line = _run((40, 20), body)
    assert "Keys" not in line
    assert "Download" in line


def test_every_hidden_binding_is_reachable_from_the_overlay():
    """Anything not in the footer must be listed by ^k, or it is invisible."""
    shown = {a for a in _FOOTER_LEFT} | {_FOOTER_RIGHT}
    listed = {b.action for b in TGetApp.BINDINGS if b.action != "show_keys"}
    hidden = {b.action for b in TGetApp.BINDINGS if not b.show}
    assert hidden <= listed, f"unreachable: {hidden - listed}"
    assert "pause_all" in hidden and "pause_all" in listed
