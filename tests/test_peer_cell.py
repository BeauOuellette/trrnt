"""The Seeds column: seeders connected over total peers connected."""

from torrentcli.download import DownloadStatus
from torrentcli.tui import SEED_GOOD, SEED_NONE, SEED_WARN, _peer_cell


def _dl(status="active", seeders=0, connections=0):
    return DownloadStatus(
        gid="abc", status=status, seeders=seeders, connections=connections
    )


def test_seeders_over_peers_is_shown():
    assert _peer_cell(_dl(seeders=9, connections=12)).plain == "9/12"


def test_seeders_present_reads_as_healthy():
    assert _peer_cell(_dl(seeders=3, connections=5)).style == SEED_GOOD


def test_peers_but_no_seeders_reads_as_partial():
    """Connected, but nobody has the complete file yet."""
    assert _peer_cell(_dl(seeders=0, connections=4)).style == SEED_WARN


def test_nothing_connected_reads_as_dead():
    """The state that eventually gets a magnet abandoned."""
    cell = _peer_cell(_dl(seeders=0, connections=0))
    assert cell.plain == "0/0"
    assert cell.style == SEED_NONE


def test_a_finished_or_errored_download_shows_a_dash():
    for status in ("complete", "error", "paused", "removed"):
        assert _peer_cell(_dl(status=status, seeders=2, connections=2)).plain == "—"


def test_a_queued_download_still_reports_peers():
    assert _peer_cell(_dl(status="waiting", seeders=1, connections=2)).plain == "1/2"
