"""The placeholder rows a search stands up while it is in flight.

The panel used to keep the last query's rows on screen for the whole search,
with only "Searching..." in the info bar to say otherwise — so results for
"game of thrones" sat there looking like results for "super mario galaxy".
The stand-ins exist so nothing on screen claims to be current when it isn't.
"""

import asyncio
import sys

import pytest
from textual.coordinate import Coordinate

from trrnt.search import TorrentResult
from trrnt.tui import _SKELETON_FILL, TGetApp, skeleton_widths

sys.path.insert(0, "tests")
from test_downloads_table import FakeConfig  # noqa: E402


# ── widths ────────────────────────────────────────────────────────────────────

def test_widths_are_ragged():
    """Equal bars read as a rendering fault, not as a list of titles."""
    widths = skeleton_widths(6, 80)
    assert len(set(widths)) > 1


def test_widths_fit_the_budget():
    assert all(0 < w <= 80 for w in skeleton_widths(10, 80))


def test_widths_are_the_same_on_every_search():
    assert skeleton_widths(8, 60) == skeleton_widths(8, 60)


def test_narrow_window_still_gets_a_visible_bar():
    """The floor matters: at a 12-column name budget the shortest fraction
    would otherwise round down to a bar you cannot see."""
    assert all(w >= 4 for w in skeleton_widths(10, 12))


def test_row_count_matches_the_request():
    assert len(skeleton_widths(3, 80)) == 3


# ── the panel ─────────────────────────────────────────────────────────────────

def _app():
    app = TGetApp(FakeConfig({
        "vpn": {"enabled": False},
        "security": {"scan_on_complete": False, "clamav_enabled": False,
                     "quarantine_dir": "/tmp/tget-test-quarantine"},
        "plex": {"enabled": False},
        "jackett": {"url": "http://localhost:9117", "api_key": ""},
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


def _seed(app):
    """Put a previous query's results in the panel."""
    app.search_results = [
        TorrentResult(title="Game of Thrones S03E01", size_bytes=2_400_000_000,
                      seeders=426, leechers=937, indexer="TheRARBG"),
    ]
    app._render_results()


def _name(table, row):
    return str(table.get_cell_at(Coordinate(row, 2)))


def _label(app):
    return str(app.query_one("#results-label").render())


async def _during_search(app, land, query="super mario galaxy"):
    """Run a search that hangs until the panel has been read, then lands.

    `land` is called for the search's return value once the mid-flight
    snapshot is taken — it returns results, or raises to fail the search.
    Returns (mid-flight snapshot, post-search snapshot).
    """
    gate = asyncio.Event()

    async def hangs(q, **kw):
        await gate.wait()
        return land()

    async with app.run_test(size=(110, 40)) as pilot:
        app.notify = lambda *a, **k: None
        _seed(app)
        app.jackett.search = hangs
        task = asyncio.create_task(app.run_search.__wrapped__(app, query))
        await pilot.pause()
        table = app.query_one("#results-table")
        mid = {"rows": table.row_count, "first": _name(table, 0),
               "label": _label(app), "ticking": app._skeleton_timer is not None}
        gate.set()
        try:
            await task
        except RuntimeError:
            pass
        await pilot.pause()
        after = {"rows": table.row_count,
                 "first": _name(table, 0) if table.row_count else "",
                 "label": _label(app), "ticking": app._skeleton_timer is not None}
        return mid, after


def test_placeholder_replaces_the_previous_results():
    def land():
        return [TorrentResult(title="Super Mario Galaxy", size_bytes=4_000_000_000,
                              seeders=12, leechers=3, indexer="1337x")]

    mid, after = asyncio.run(_during_search(_app(), land))
    assert _SKELETON_FILL in mid["first"], "the old query's title was still on screen"
    assert mid["rows"] > 1, "the panel should fill with stand-ins, not one row"
    assert after["first"] == "Super Mario Galaxy"


def test_label_names_the_query_while_it_runs():
    mid, after = asyncio.run(
        _during_search(_app(), lambda: [], query="dune part three"))
    assert "dune part three" in mid["label"]
    assert mid["ticking"], "nothing is animating the stand-ins"
    assert after["label"] == "Results", "the label kept the spinner after landing"
    assert not after["ticking"], "the timer outlived the search"


def test_a_failed_search_does_not_leave_the_placeholder_ticking():
    """A stand-in still shading over a search that will never land is worse
    than no stand-in at all."""

    def falls_over():
        raise RuntimeError("jackett fell over")

    mid, after = asyncio.run(_during_search(_app(), falls_over))
    assert _SKELETON_FILL in mid["first"]
    assert not after["ticking"]
    assert after["label"] == "Results"


def test_a_superseded_search_does_not_take_down_the_new_placeholder():
    """Searching again while one is in flight cancels the first worker, which
    reaches its teardown after the second has already put its own stand-ins
    up. The id is what keeps it from tearing down the wrong one."""

    async def go():
        app = _app()
        async with app.run_test(size=(110, 40)) as pilot:
            app.notify = lambda *a, **k: None
            stale = app._start_skeleton("first query", 50)
            app._start_skeleton("second query", 50)
            app._stop_skeleton(stale)
            await pilot.pause()
            return app._skeleton_timer is not None, _label(app)

    ticking, label = asyncio.run(go())
    assert ticking, "the newer search's placeholder was torn down"
    assert "second query" in label


def test_placeholder_never_promises_more_rows_than_the_search_returns():
    """A 40-line window has room for far more stand-ins than a max_results of
    5 can fill, and a panel that empties out on arrival reads as a failure."""

    async def go():
        app = _app()
        async with app.run_test(size=(110, 40)) as pilot:
            app._start_skeleton("query", 5)
            await pilot.pause()
            rows = app.query_one("#results-table").row_count
            app._stop_skeleton(app._skeleton_id)
            return rows

    assert asyncio.run(go()) == 5


def test_resize_refits_the_placeholder_rather_than_restoring_stale_rows():
    async def go():
        app = _app()
        async with app.run_test(size=(110, 40)) as pilot:
            app.notify = lambda *a, **k: None
            _seed(app)
            app._start_skeleton("query", 50)
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            table = app.query_one("#results-table")
            first = _name(table, 0)
            app._stop_skeleton(app._skeleton_id)
            return first

    assert _SKELETON_FILL in asyncio.run(go())
