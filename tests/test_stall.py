"""When a stuck magnet counts as dead.

Time alone was the old rule, and it killed torrents that were connected to
peers and still working through the metadata handshake.
"""

import pytest

from trrnt.download import DownloadStatus
from trrnt.tui import _STALL_REMOVE_TICKS, is_dead_magnet


def _stuck(connections=0, total_bytes=0, speed=0):
    """A magnet with no metadata yet."""
    return DownloadStatus(
        gid="abc", status="active",
        total_bytes=total_bytes, completed_bytes=0,
        download_speed=speed, connections=connections,
    )


LONG_ENOUGH = _STALL_REMOVE_TICKS


def test_a_magnet_nobody_answers_is_dead():
    assert is_dead_magnet(_stuck(connections=0), LONG_ENOUGH)


def test_connected_peers_keep_it_alive_indefinitely():
    """The case that killed a real download: 10 peers, metadata still coming."""
    assert not is_dead_magnet(_stuck(connections=10), LONG_ENOUGH)
    assert not is_dead_magnet(_stuck(connections=10), LONG_ENOUGH * 100)


def test_a_single_peer_is_enough_to_wait():
    assert not is_dead_magnet(_stuck(connections=1), LONG_ENOUGH)


def test_nothing_is_removed_before_the_limit():
    assert not is_dead_magnet(_stuck(connections=0), LONG_ENOUGH - 1)
    assert not is_dead_magnet(_stuck(connections=0), 0)


def test_a_download_with_metadata_is_never_touched():
    """Slow or stalled part-way through is not this check's business."""
    assert not is_dead_magnet(_stuck(total_bytes=5_000_000), LONG_ENOUGH)


def test_a_download_making_progress_is_never_touched():
    assert not is_dead_magnet(_stuck(speed=1024), LONG_ENOUGH)
