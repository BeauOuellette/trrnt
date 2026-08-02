"""Excluding indexers from searches.

A blocklist, not an allowlist: `indexers` pins the search to exactly what it
names, so a tracker added later would be silently ignored. Excluding is the
common case — one indexer sits behind Cloudflare and errors on every search.
"""

import asyncio

import pytest

from trrnt.search import JackettSearch


class Recorder(JackettSearch):
    """Records which indexers a search actually fanned out to."""

    def __init__(self, config, available):
        super().__init__(config)
        self._available = available
        self.queried: list[str] = []

    async def _list_indexers(self, client):
        return list(self._available)

    async def _search_indexer(self, client, query, indexer, quality_exclude):
        self.queried.append(indexer)
        return []


def _run(config, available, **kw):
    search = Recorder(config, available)
    asyncio.run(search.search("anything", **kw))
    return search.queried


BASE = {"url": "http://localhost:9117", "api_key": "k"}


def test_no_exclusions_searches_everything():
    assert sorted(_run(BASE, ["1337x", "thepiratebay"])) == ["1337x", "thepiratebay"]


def test_excluded_indexer_is_never_queried():
    config = {**BASE, "exclude_indexers": ["1337x"]}
    assert _run(config, ["1337x", "thepiratebay"]) == ["thepiratebay"]


def test_exclusion_is_case_insensitive():
    """Jackett ids are lowercase, but a hand-edited config may not be."""
    config = {**BASE, "exclude_indexers": ["1337X", " ThePirateBay "]}
    assert _run(config, ["1337x", "thepiratebay", "eztv"]) == ["eztv"]


def test_excluding_everything_is_ignored():
    """A config that excludes every indexer would look like 'no results'
    forever with nothing on screen to explain it. Treat it as a mistake."""
    config = {**BASE, "exclude_indexers": ["1337x", "thepiratebay"]}
    assert sorted(_run(config, ["1337x", "thepiratebay"])) == ["1337x", "thepiratebay"]


def test_exclusion_applies_to_a_pinned_indexer_list():
    config = {**BASE, "indexers": ["1337x", "eztv"], "exclude_indexers": ["1337x"]}
    assert _run(config, ["ignored"]) == ["eztv"]


def test_unknown_exclusion_is_harmless():
    config = {**BASE, "exclude_indexers": ["nolongerexists"]}
    assert _run(config, ["thepiratebay"]) == ["thepiratebay"]


# ── the repeated-warning problem ──────────────────────────────────────────────

def test_broken_indexer_only_toasts_once(tmp_path):
    """A permanently broken indexer must not toast on every search.

    Cloudflare-gated trackers fail forever, and a warning on every single
    search is how people learn to ignore warnings. The info bar still says
    it every time; only the interruption is deduped.
    """
    import sys
    sys.path.insert(0, "tests")
    from test_downloads_table import FakeConfig
    from trrnt.tui import TGetApp

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

    toasts = []
    errors = [("1337x", "cloudflare")]

    async def fake_search(query, **kw):
        app.jackett.last_errors = list(errors)
        return []

    async def go():
        async with app.run_test(size=(110, 40)) as pilot:
            app.jackett.search = fake_search
            app.notify = lambda msg, **kw: toasts.append(msg)
            # run_search also toasts "Searching: ..." each time; only the
            # skip warning is under test here.
            skips = lambda: [t for t in toasts if t.startswith("Skipping")]
            for _ in range(3):
                await app.run_search.__wrapped__(app, "query")
            first_round = len(skips())
            # A *different* failure is news again.
            errors.append(("eztv", "cloudflare"))
            await app.run_search.__wrapped__(app, "query")
            second_round = len(skips())
            info = str(app.query_one("#info-bar").render())
        return first_round, second_round, info

    first, second, info = asyncio.run(go())
    assert first == 1, f"warned {first} times for the same broken indexer"
    assert second == 2, "a newly broken indexer should warn again"
    assert "1337x" in info and "eztv" in info, "info bar must still list them"


# ── error reasons ─────────────────────────────────────────────────────────────

def test_a_torznab_error_in_a_400_is_read_not_discarded():
    """Jackett wraps a solver outage in a 400 whose body says what happened.

    Reporting "HTTP 400" reads as a dead tracker; the body says the solver is
    simply not running, which is a one-keystroke fix the caller can offer.
    """
    from trrnt.search import _torznab_error

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<error code="900" description="Jackett.Common.IndexerException: '
        'Exception (kickasstorrents-to): Error connecting to FlareSolverr '
        'server: Connection refused (127.0.0.1:8191)" />'
    )

    reason = _torznab_error(body)

    assert reason is not None
    assert "FlareSolverr" in reason


def test_a_non_error_body_yields_no_reason():
    from trrnt.search import _torznab_error

    assert _torznab_error("<rss><channel></channel></rss>") is None
    assert _torznab_error("not xml at all") is None
