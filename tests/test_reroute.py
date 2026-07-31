"""Correcting a download's destination while it is still running."""

import asyncio

import pytest

from torrentcli.download import DownloadStatus, discard_partial, reroute_in_flight


def run(coro):
    """Drive a coroutine — the suite has no pytest-asyncio dependency."""
    return asyncio.run(coro)


class FakeConfig:
    def __init__(self, data):
        self._data = data

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


class FakeAria2:
    """Records change_dir calls instead of talking to a daemon."""

    def __init__(self, files, fail_on_change=False):
        self._files = files
        self.changed_to = None
        self.fail_on_change = fail_on_change

    async def get_files(self, gid):
        if isinstance(self._files, Exception):
            raise self._files
        return self._files

    async def change_dir(self, gid, download_dir):
        if self.fail_on_change:
            raise RuntimeError("aria2 said no")
        self.changed_to = download_dir
        return "OK"


@pytest.fixture
def config(tmp_path):
    return FakeConfig(
        {
            "categories": {
                "movies": {"path": str(tmp_path / "Movies")},
                "tv": {"path": str(tmp_path / "TV")},
                "comics": {"path": str(tmp_path / "Comics")},
                "other": {"path": str(tmp_path / "torrents")},
            },
            "destinations": {"fallback_path": str(tmp_path / "torrents")},
        }
    )


def _dl(tmp_path, name="Some Release", subdir="Movies"):
    return DownloadStatus(
        gid="abc123", status="active", name=name,
        total_bytes=1000, completed_bytes=100,
        dir=str(tmp_path / subdir),
    )


def test_a_comic_downloading_into_movies_is_redirected(config, tmp_path):
    aria2 = FakeAria2([str(tmp_path / "Movies/Release/issue-01.cbr")])

    category = run(reroute_in_flight(aria2, config, _dl(tmp_path)))

    assert category == "comics"
    assert aria2.changed_to == str(tmp_path / "Comics")


def test_an_episode_filename_redirects_to_tv(config, tmp_path):
    """The case the release title kept getting wrong."""
    aria2 = FakeAria2([str(tmp_path / "Movies/Show/Show.S01E07.1080p.mkv")])

    category = run(reroute_in_flight(aria2, config, _dl(tmp_path)))

    assert category == "tv"
    assert aria2.changed_to == str(tmp_path / "TV")


def test_a_correctly_filed_download_is_left_alone(config, tmp_path):
    aria2 = FakeAria2([str(tmp_path / "Comics/Release/issue-01.cbr")])
    dl = _dl(tmp_path, subdir="Comics")

    assert run(reroute_in_flight(aria2, config, dl)) is None
    assert aria2.changed_to is None


def test_a_plain_movie_is_not_touched(config, tmp_path):
    """No episode number, so movie-vs-TV can't be settled — leave the guess."""
    aria2 = FakeAria2([str(tmp_path / "Movies/Film/Film.2024.1080p.mkv")])

    assert run(reroute_in_flight(aria2, config, _dl(tmp_path))) is None
    assert aria2.changed_to is None


def test_unresolved_metadata_is_a_no_op(config, tmp_path):
    """A magnet mid-handshake has no file list yet; caller retries."""
    aria2 = FakeAria2(RuntimeError("no metadata"))

    assert run(reroute_in_flight(aria2, config, _dl(tmp_path))) is None
    assert aria2.changed_to is None


def test_empty_file_list_is_a_no_op(config, tmp_path):
    aria2 = FakeAria2([])

    assert run(reroute_in_flight(aria2, config, _dl(tmp_path))) is None


def test_a_failed_dir_change_propagates(config, tmp_path):
    """Callers must hear about it rather than believe the file moved."""
    aria2 = FakeAria2(
        [str(tmp_path / "Movies/Release/issue-01.cbr")], fail_on_change=True
    )

    with pytest.raises(RuntimeError, match="aria2 said no"):
        run(reroute_in_flight(aria2, config, _dl(tmp_path)))


def test_the_partial_left_behind_is_cleaned_up(config, tmp_path):
    old = tmp_path / "Movies"
    old.mkdir()
    (old / "Some Release").mkdir()
    (old / "Some Release" / "part.cbr").write_bytes(b"x")
    (old / "Some Release.aria2").write_bytes(b"ctrl")
    aria2 = FakeAria2([str(old / "Some Release/part.cbr")])

    run(reroute_in_flight(aria2, config, _dl(tmp_path)))

    assert not (old / "Some Release").exists()
    assert not (old / "Some Release.aria2").exists()


# ── discard_partial safety ────────────────────────────────────────────────────

def test_a_finished_file_without_a_control_file_is_never_deleted(tmp_path):
    """No .aria2 beside it means it isn't an in-progress download — a
    completed file that happens to share the name must survive."""
    (tmp_path / "Some Release").mkdir()
    (tmp_path / "Some Release" / "keeper.mkv").write_bytes(b"x")

    discard_partial(tmp_path, "Some Release")

    assert (tmp_path / "Some Release" / "keeper.mkv").exists()


def test_a_single_file_partial_is_removed(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"x")
    (tmp_path / "movie.mkv.aria2").write_bytes(b"ctrl")

    discard_partial(tmp_path, "movie.mkv")

    assert not (tmp_path / "movie.mkv").exists()
    assert not (tmp_path / "movie.mkv.aria2").exists()


def test_an_empty_name_is_ignored(tmp_path):
    discard_partial(tmp_path, "")  # must not raise
