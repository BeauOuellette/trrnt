"""Destination resolution when the media drive isn't connected.

These tests must never depend on what is actually mounted on the machine
running them, so every "external volume" lives under a temp directory that
stands in for /Volumes.
"""

from pathlib import Path

import pytest

from trrnt import storage
from trrnt.storage import (
    Destination,
    DestinationUnavailable,
    is_available,
    resolve_destination,
    volume_root,
)


class FakeConfig:
    """Stands in for Config — only .get() is used by resolve_destination."""

    def __init__(self, data: dict):
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


@pytest.fixture(autouse=True)
def darwin_mount_parents(monkeypatch):
    """Pin the /Volumes layout so path parsing means the same thing anywhere."""
    monkeypatch.setattr(storage, "_MOUNT_PARENTS", ("/Volumes",))


@pytest.fixture
def volumes(monkeypatch, tmp_path):
    """A mount-point root with nothing mounted under it.

    Using the real /Volumes here would make these tests pass or fail based on
    whether a drive happened to be plugged in.
    """
    root = tmp_path / "Volumes"
    root.mkdir()
    monkeypatch.setattr(storage, "_MOUNT_PARENTS", (str(root),))
    return root


@pytest.fixture
def offline_drive(volumes):
    """Path to a drive that is not mounted — the folder does not exist."""
    return volumes / "Media HDD"


@pytest.fixture
def config(tmp_path, offline_drive):
    """Mirrors the real config: media on an external drive, other local."""
    return FakeConfig(
        {
            "categories": {
                "movies": {"path": str(offline_drive / "Media" / "Movies")},
                "audiobooks": {"path": str(offline_drive / "Media" / "Audiobooks")},
                "other": {"path": str(tmp_path / "torrents")},
            },
            "destinations": {
                "on_unavailable": "fallback",
                "fallback_path": str(tmp_path / "torrents"),
                "require_mount": True,
            },
        }
    )


# ── volume_root ───────────────────────────────────────────────────────────────

def test_external_path_reports_its_volume():
    assert volume_root("/Volumes/Media HDD/Media/Movies") == Path("/Volumes/Media HDD")


def test_volume_root_of_the_mount_point_itself():
    assert volume_root("/Volumes/Media HDD") == Path("/Volumes/Media HDD")


def test_home_paths_have_no_volume():
    assert volume_root("~/Downloads/torrents") is None
    assert volume_root("/Users/someone/Media/Movies") is None


def test_bare_volumes_dir_has_no_volume():
    assert volume_root("/Volumes") is None


# ── is_available ──────────────────────────────────────────────────────────────

def test_local_path_is_always_available(tmp_path):
    # The directory need not exist yet — aria2 creates it on demand.
    assert is_available(tmp_path / "not" / "yet" / "created")


def test_unplugged_drive_is_unavailable(offline_drive):
    assert not is_available(offline_drive / "Media")


def test_stale_folder_left_by_an_unplugged_drive_is_unavailable(volumes):
    """The empty /Volumes/<name> shell must not absorb downloads."""
    stale = volumes / "Media HDD"
    stale.mkdir()

    assert not is_available(stale / "Media" / "Movies")
    # ...unless the operator has explicitly opted out of the check.
    assert is_available(stale / "Media" / "Movies", require_mount=False)


def test_real_mount_point_is_available(monkeypatch, volumes):
    mounted = volumes / "Media HDD"
    mounted.mkdir()
    monkeypatch.setattr(storage.os.path, "ismount", lambda p: Path(p) == mounted)

    assert is_available(mounted / "Media" / "Movies")


# ── resolve_destination ───────────────────────────────────────────────────────

def test_connected_drive_is_used_as_configured(config, monkeypatch, offline_drive):
    monkeypatch.setattr(storage, "is_available", lambda *a, **k: True)

    dest = resolve_destination(config, "movies")

    assert dest.path == str(offline_drive / "Media" / "Movies")
    assert not dest.redirected
    assert dest.notice == ""


def test_offline_drive_falls_back_locally_under_the_category(
    config, tmp_path, offline_drive
):
    dest = resolve_destination(config, "movies")

    assert dest.path == str(tmp_path / "torrents" / "movies")
    assert dest.redirected
    assert dest.configured == str(offline_drive / "Media" / "Movies")


def test_fallback_notice_names_the_missing_drive(config):
    assert resolve_destination(config, "movies").notice.startswith("Media HDD offline →")


def test_each_category_gets_its_own_fallback_subdirectory(config):
    assert resolve_destination(config, "movies").path.endswith("/movies")
    assert resolve_destination(config, "audiobooks").path.endswith("/audiobooks")


def test_local_category_is_untouched_by_the_missing_drive(config, tmp_path):
    dest = resolve_destination(config, "other")

    assert dest.path == str(tmp_path / "torrents")
    assert not dest.redirected


def test_unknown_category_uses_the_other_category(config, tmp_path):
    assert resolve_destination(config, "documentaries").path == str(tmp_path / "torrents")


def test_abort_policy_refuses_instead_of_redirecting(config):
    config._data["destinations"]["on_unavailable"] = "abort"

    with pytest.raises(DestinationUnavailable, match="Media HDD"):
        resolve_destination(config, "movies")


def test_a_fallback_on_a_missing_drive_degrades_to_the_default(config, volumes):
    config._data["destinations"]["fallback_path"] = str(volumes / "Also Missing" / "dump")

    dest = resolve_destination(config, "movies")

    assert dest.path == str(Path(storage.DEFAULT_FALLBACK).expanduser() / "movies")


def test_defaults_apply_when_the_destinations_block_is_absent(offline_drive):
    """Existing configs written before this feature keep working."""
    config = FakeConfig(
        {"categories": {"movies": {"path": str(offline_drive / "Media" / "Movies")}}}
    )

    dest = resolve_destination(config, "movies")

    assert dest.redirected
    assert dest.path == str(Path(storage.DEFAULT_FALLBACK).expanduser() / "movies")


def test_require_mount_can_be_disabled(config, volumes):
    """Opting out lets a stale folder be used, for anyone who wants that."""
    stale = volumes / "Media HDD" / "Media" / "Movies"
    stale.mkdir(parents=True)
    config._data["categories"]["movies"]["path"] = str(stale)
    config._data["destinations"]["require_mount"] = False

    dest = resolve_destination(config, "movies")

    assert not dest.redirected
    assert dest.path == str(stale)


def test_category_without_a_path_uses_the_supplied_default(config, tmp_path):
    config._data["categories"]["music"] = {}

    dest = resolve_destination(config, "music", str(tmp_path / "aria2-default"))

    assert dest.path == str(tmp_path / "aria2-default")


# ── notice formatting ─────────────────────────────────────────────────────────

def test_notice_abbreviates_the_home_directory():
    dest = Destination(
        path=str(Path.home() / "Downloads" / "torrents" / "movies"),
        configured="/Volumes/Media HDD/Media/Movies",
        volume="Media HDD",
    )

    assert dest.notice == "Media HDD offline → ~/Downloads/torrents/movies"
