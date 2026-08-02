"""Predicting where a running download will be filed.

A torrent cannot be redirected once aria2 has created it — aria2 binds the
file paths at add time, and changeOption(dir) afterwards only changes what
tellStatus reports while the data keeps landing in the original folder
(verified against a real local swarm, both mid-download and on a download
added paused before any file existed). So this is reporting only; the actual
filing happens when the download completes.
"""

import asyncio

import pytest

from trrnt.download import DownloadStatus, predict_category


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
    def __init__(self, files):
        self._files = files

    async def get_files(self, gid):
        if isinstance(self._files, Exception):
            raise self._files
        return self._files


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


def test_a_comic_heading_into_movies_is_flagged(config, tmp_path):
    aria2 = FakeAria2([str(tmp_path / "Movies/Release/issue-01.cbr")])

    assert run(predict_category(aria2, config, _dl(tmp_path))) == "comics"


def test_an_episode_filename_is_flagged_as_tv(config, tmp_path):
    """The case release titles kept getting wrong."""
    aria2 = FakeAria2([str(tmp_path / "Movies/Show/Show.S01E07.1080p.mkv")])

    assert run(predict_category(aria2, config, _dl(tmp_path))) == "tv"


def test_a_correctly_filed_download_is_not_flagged(config, tmp_path):
    aria2 = FakeAria2([str(tmp_path / "Comics/Release/issue-01.cbr")])
    dl = _dl(tmp_path, subdir="Comics")

    assert run(predict_category(aria2, config, dl)) is None


def test_a_plain_movie_is_not_flagged(config, tmp_path):
    """No episode number, so movie-vs-TV can't be settled — leave the guess."""
    aria2 = FakeAria2([str(tmp_path / "Movies/Film/Film.2024.1080p.mkv")])

    assert run(predict_category(aria2, config, _dl(tmp_path))) is None


def test_unresolved_metadata_is_a_no_op(config, tmp_path):
    """A magnet mid-handshake has no file list yet; caller retries."""
    aria2 = FakeAria2(RuntimeError("no metadata"))

    assert run(predict_category(aria2, config, _dl(tmp_path))) is None


def test_empty_file_list_is_a_no_op(config, tmp_path):
    assert run(predict_category(FakeAria2([]), config, _dl(tmp_path))) is None
