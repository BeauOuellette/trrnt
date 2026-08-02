"""Junk selection, the plan store, and completion-time filing."""

import asyncio
import json

import pytest

from torrentcli.download import DownloadStatus
from torrentcli.organize import (
    DEFAULT_JUNK_EXTENSIONS,
    OrganizeRecord,
    OrganizeStore,
    apply_pending_selection,
    cleanup_wrapper,
    effective_junk,
    find_orphan_records,
    organize_download,
    plan_output_names,
    plan_selection,
)


def run(coro):
    """Drive a coroutine — the suite has no pytest-asyncio dependency."""
    return asyncio.run(coro)


def _files(*specs):
    """(name, length) tuples → aria2 getFiles-shaped dicts, 1-indexed."""
    return [
        {"index": i + 1, "path": f"/dl/Release/{name}", "length": length,
         "selected": True}
        for i, (name, length) in enumerate(specs)
    ]


# ── effective_junk ────────────────────────────────────────────────────────────

def test_video_categories_use_the_full_junk_list():
    assert ".txt" in effective_junk("tv")
    assert ".jpg" in effective_junk("movies")


def test_music_keeps_cover_art():
    junk = effective_junk("music")
    assert ".jpg" not in junk
    assert ".nfo" in junk


def test_ebooks_keep_txt():
    junk = effective_junk("ebooks")
    assert ".txt" not in junk
    assert ".sfv" in junk


def test_unknown_category_only_drops_unambiguous_junk():
    junk = effective_junk("other")
    assert ".nfo" in junk
    assert ".txt" not in junk
    assert ".jpg" not in junk


# ── plan_selection ────────────────────────────────────────────────────────────

def test_junk_is_deselected_and_indices_are_aria2s_own():
    files = _files(
        ("Show.S01E01.mkv", 4_000_000_000),
        ("release.nfo", 4_000),
        ("Torrent downloaded from site.txt", 100),
        ("Show.S01E02.mkv", 4_000_000_000),
    )
    assert plan_selection(files, effective_junk("tv")) == [1, 4]


def test_sample_videos_are_junk():
    files = _files(
        ("Movie.2026.2160p.mkv", 8_000_000_000),
        ("movie.2026.sample.mkv", 40_000_000),
    )
    assert plan_selection(files, effective_junk("movies")) == [1]


def test_a_full_size_video_named_sample_is_kept():
    # "The Sample" could be an actual episode title; size is the tiebreak.
    files = _files(("Show.S01E01.Sample.mkv", 3_000_000_000),)
    assert plan_selection(files, effective_junk("tv")) is None


def test_nothing_junk_means_no_change():
    files = _files(("a.mkv", 1), ("b.mkv", 1))
    assert plan_selection(files, effective_junk("tv")) is None


def test_everything_junk_means_no_change():
    # A torrent that is all .txt is content we misjudged, not all junk.
    files = _files(("book.txt", 1), ("notes.txt", 1))
    assert plan_selection(files, effective_junk("tv")) is None


# ── plan_output_names ─────────────────────────────────────────────────────────

def _torrent_files(dest, wrapper, *specs):
    """(relpath-under-wrapper, length) → aria2 getFiles dicts under dest."""
    return [
        {"index": i + 1, "path": f"{dest}/{wrapper}/{rel}" if wrapper else f"{dest}/{rel}",
         "length": length, "selected": True}
        for i, (rel, length) in enumerate(specs)
    ]


def test_single_video_and_subs_map_to_the_clean_name(tmp_path):
    files = _torrent_files(
        tmp_path, "www.Site.com - Show S02E03 2160p GROUP",
        ("Show.S02E03.2160p.GROUP.mkv", 9_000_000_000),
        ("Show.S02E03.srt", 40_000),
        ("release.nfo", 4_000),
    )
    out = plan_output_names(files, "The Agency S02E03", effective_junk("tv"), tmp_path)
    assert out == {1: "The Agency S02E03.mkv", 2: "The Agency S02E03.srt"}


def test_season_pack_maps_per_episode(tmp_path):
    files = _torrent_files(
        tmp_path, "Show.S02.Pack.2160p",
        ("Show.S02E01.2160p.mkv", 1_000_000_000),
        ("Show.S02E02.2160p.mkv", 1_000_000_000),
        ("site.txt", 100),
    )
    out = plan_output_names(files, "Show Season 2", effective_junk("tv"), tmp_path)
    assert out == {1: "Show S02E01.mkv", 2: "Show S02E02.mkv"}


def test_single_file_torrent_maps_flat(tmp_path):
    files = _torrent_files(
        tmp_path, "",
        ("Backrooms.2026.2160p.WEB.mkv", 8_000_000_000),
    )
    out = plan_output_names(files, "Backrooms (2026)", effective_junk("movies"), tmp_path)
    assert out == {1: "Backrooms (2026).mkv"}


def test_existing_target_is_not_mapped(tmp_path):
    (tmp_path / "Show S02E01.mkv").write_bytes(b"old")
    files = _torrent_files(
        tmp_path, "Show.S02.Repack",
        ("Show.S02E01.Repack.mkv", 1_000_000_000),
        ("Show.S02E02.Repack.mkv", 1_000_000_000),
    )
    out = plan_output_names(files, "Show Season 2", effective_junk("tv"), tmp_path)
    assert out == {2: "Show S02E02.mkv"}  # E01 stays put for the mover to report


def test_duplicate_targets_keep_only_the_first(tmp_path):
    files = _torrent_files(
        tmp_path, "Pack",
        ("CD1/track.flac", 1_000),
        ("CD2/track.flac", 1_000),
    )
    out = plan_output_names(files, "Some Album", effective_junk("music"), tmp_path)
    assert out == {1: "CD1/track.flac", 2: "CD2/track.flac"}


def test_subfolder_files_keep_their_shape(tmp_path):
    files = _torrent_files(
        tmp_path, "Show.S01E01.1080p",
        ("Show.S01E01.1080p.mkv", 1_000_000_000),
        ("Subs/eng.srt", 40_000),
    )
    out = plan_output_names(files, "Show S01E01", effective_junk("tv"), tmp_path)
    assert out[1] == "Show S01E01.mkv"
    assert out[2] == "Subs/eng.srt"


def test_foreign_paths_map_nothing(tmp_path):
    files = [{"index": 1, "path": "/somewhere/else/file.mkv", "length": 1, "selected": True}]
    assert plan_output_names(files, "Name", effective_junk("tv"), tmp_path) == {}


# ── cleanup_wrapper ───────────────────────────────────────────────────────────

def test_wrapper_with_only_junk_placeholders_is_removed(tmp_path):
    wrapper = tmp_path / "www.Site.com - Show"
    wrapper.mkdir()
    (wrapper / "release.nfo").write_bytes(b"")
    (wrapper / "site.txt").write_bytes(b"")
    assert cleanup_wrapper(wrapper, effective_junk("tv")) is True
    assert not wrapper.exists()


def test_wrapper_with_real_content_is_left_alone(tmp_path):
    wrapper = tmp_path / "Show.S01"
    wrapper.mkdir()
    (wrapper / "straggler.mkv").write_bytes(b"data")
    assert cleanup_wrapper(wrapper, effective_junk("tv")) is False
    assert (wrapper / "straggler.mkv").exists()


def test_missing_wrapper_counts_as_cleaned(tmp_path):
    assert cleanup_wrapper(tmp_path / "gone", effective_junk("tv")) is True


# ── OrganizeStore ─────────────────────────────────────────────────────────────

def _store(tmp_path):
    return OrganizeStore(tmp_path / "organize.json")


def test_records_round_trip_through_disk(tmp_path):
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="a" * 16, dir="/x", category="tv", name="Show S01E01"))

    fresh = _store(tmp_path)
    rec = fresh.match("a" * 16, "")
    assert rec is not None
    assert rec.name == "Show S01E01"
    assert rec.created  # stamped on add


def test_match_by_followup_gid(tmp_path):
    store = _store(tmp_path)
    rec = OrganizeRecord(gid="a" * 16, dir="/x", active_gid="b" * 16)
    store.add(rec)
    assert store.match("b" * 16, "/other") is rec


def test_dir_fallback_only_matches_when_unambiguous(tmp_path):
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="a" * 16, dir="/movies"))
    assert store.match("zzz", "/movies").gid == "a" * 16

    store.add(OrganizeRecord(gid="c" * 16, dir="/movies"))
    assert store.match("zzz", "/movies") is None


def test_old_records_are_pruned_on_load(tmp_path):
    path = tmp_path / "organize.json"
    path.write_text(json.dumps([
        {"gid": "a" * 16, "dir": "/x", "created": "2020-01-01T00:00:00"},
        {"gid": "b" * 16, "dir": "/y", "created": "2999-01-01T00:00:00"},
    ]))
    store = OrganizeStore(path)
    assert store.match("a" * 16, "") is None
    assert store.match("b" * 16, "") is not None


def test_corrupt_store_is_an_empty_store(tmp_path):
    path = tmp_path / "organize.json"
    path.write_text("{not json")
    assert OrganizeStore(path).pending() == []


def test_records_from_before_the_remapped_field_still_load(tmp_path):
    path = tmp_path / "organize.json"
    path.write_text(json.dumps([{
        "gid": "a" * 16, "dir": "/x", "category": "tv", "name": "Show",
        "selection_done": True, "active_gid": "", "created": "2999-01-01T00:00:00",
    }]))
    rec = OrganizeStore(path).match("a" * 16, "")
    assert rec is not None
    assert rec.remapped is False


# ── apply_pending_selection ───────────────────────────────────────────────────

class FakeConfig:
    def get(self, *keys, default=None):
        return default


class FakeAria2:
    """Metadata parent that resolved into a paused follow-up with junk."""

    def __init__(self):
        self.changed: list[tuple[str, dict]] = []
        self.unpaused: list[str] = []
        self.follow_status = "paused"

    async def get_status(self, gid):
        if gid == "parent0000000001":
            return DownloadStatus(gid=gid, status="complete",
                                  followed_by=["follow0000000001"])
        return DownloadStatus(gid=gid, status=self.follow_status)

    async def get_files_detailed(self, gid):
        return _files(
            ("Show.S01E01.mkv", 4_000_000_000),
            ("release.nfo", 4_000),
        )

    async def change_option(self, gid, options):
        self.changed.append((gid, options))

    async def unpause(self, gid):
        self.unpaused.append(gid)


def test_selection_flows_to_the_followup_and_unpauses(tmp_path):
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="parent0000000001", dir="/x", category="tv"))
    aria2 = FakeAria2()

    run(apply_pending_selection(aria2, store, FakeConfig()))

    assert ("follow0000000001", {"select-file": "1"}) in aria2.changed
    assert ("follow0000000001", {"pause-metadata": "false"}) in aria2.changed
    assert aria2.unpaused == ["follow0000000001"]
    rec = store.match("follow0000000001", "")
    assert rec.selection_done
    # And the applier leaves settled records alone on the next tick.
    run(apply_pending_selection(aria2, store, FakeConfig()))
    assert len(aria2.unpaused) == 1


def test_a_named_plan_remaps_output_paths(tmp_path):
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="parent0000000001", dir="/dl", category="tv",
                             name="Show S01E01"))
    aria2 = FakeAria2()

    run(apply_pending_selection(aria2, store, FakeConfig()))

    remaps = [opts for gid, opts in aria2.changed if "index-out" in opts]
    assert remaps == [{"index-out": ["1=Show S01E01.mkv"]}]
    rec = store.match("follow0000000001", "")
    assert rec.remapped is True
    assert rec.wrapper == "Release"  # captured for the orphan sweep


def test_an_unpaused_followup_is_not_paused_or_unpaused(tmp_path):
    # pause-metadata not honored (foreign daemon): selection still applies,
    # but we never touch the pause state of a running download.
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="parent0000000001", dir="/x", category="tv"))
    aria2 = FakeAria2()
    aria2.follow_status = "active"

    run(apply_pending_selection(aria2, store, FakeConfig()))

    assert aria2.unpaused == []
    assert store.pending() == []


def test_error_downloads_drop_their_record(tmp_path):
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="e" * 16, dir="/x"))

    class ErrAria2(FakeAria2):
        async def get_status(self, gid):
            return DownloadStatus(gid=gid, status="error")

    run(apply_pending_selection(ErrAria2(), store, FakeConfig()))
    assert store.pending() == []


# ── find_orphan_records ───────────────────────────────────────────────────────

class ProbeAria2:
    """get_status scripted per GID: 'gone', 'alive', 'busy', or 'down'."""

    def __init__(self, verdicts):
        self.verdicts = verdicts

    async def get_status(self, gid):
        verdict = self.verdicts.get(gid, "gone")
        if verdict == "alive":
            return DownloadStatus(gid=gid, status="active")
        if verdict == "busy":
            raise RuntimeError("aria2 error: some other complaint")
        if verdict == "down":
            raise ConnectionError("connection refused")
        # Real aria2 1.37 wording, verified against a live daemon.
        raise RuntimeError(f"aria2 error: GID {gid} is not found")


def _settled(tmp_path, **kw):
    defaults = dict(gid="a" * 16, dir=str(tmp_path), category="tv",
                    name="Show S01E01", selection_done=True,
                    active_gid="b" * 16, remapped=True)
    defaults.update(kw)
    return OrganizeRecord(**defaults)


def test_dead_record_with_no_wrapper_is_dropped(tmp_path):
    store = _store(tmp_path)
    store.add(_settled(tmp_path, wrapper=""))

    result = run(find_orphan_records(ProbeAria2({}), store))

    assert result == []
    assert store.settled() == []


def test_dead_record_with_wrapper_on_disk_is_returned_for_filing(tmp_path):
    (tmp_path / "Release").mkdir()
    store = _store(tmp_path)
    rec = _settled(tmp_path, wrapper="Release")
    store.add(rec)

    result = run(find_orphan_records(ProbeAria2({}), store))

    assert result == [rec]
    assert store.settled() == [rec]  # kept until the completion pass files it


def test_live_downloads_are_never_touched(tmp_path):
    store = _store(tmp_path)
    store.add(_settled(tmp_path, wrapper=""))

    run(find_orphan_records(ProbeAria2({"b" * 16: "alive"}), store))

    assert len(store.settled()) == 1


def test_transport_trouble_reads_as_alive(tmp_path):
    # aria2 being unreachable must not condemn a still-running download.
    store = _store(tmp_path)
    store.add(_settled(tmp_path, wrapper=""))

    run(find_orphan_records(ProbeAria2({"b" * 16: "down", "a" * 16: "down"}), store))

    assert len(store.settled()) == 1


def test_other_aria2_errors_read_as_alive(tmp_path):
    store = _store(tmp_path)
    store.add(_settled(tmp_path, wrapper=""))

    run(find_orphan_records(ProbeAria2({"b" * 16: "busy", "a" * 16: "busy"}), store))

    assert len(store.settled()) == 1


def test_pending_records_are_not_swept(tmp_path):
    # An unsettled record's download may not exist YET — that's the
    # applier's business, not the sweep's.
    store = _store(tmp_path)
    store.add(OrganizeRecord(gid="a" * 16, dir=str(tmp_path), category="tv"))

    result = run(find_orphan_records(ProbeAria2({}), store))

    assert result == []
    assert len(store.pending()) == 1


# ── organize_download ─────────────────────────────────────────────────────────

@pytest.fixture
def junk():
    return effective_junk("tv", DEFAULT_JUNK_EXTENSIONS)


def test_single_file_torrent_is_renamed_in_place(tmp_path, junk):
    dest = tmp_path / "TV" / "The Agency" / "Season 2"
    dest.mkdir(parents=True)
    f = dest / "www.Site.com.The.Agency.S02E03.2160p.SKIZ.mkv"
    f.write_bytes(b"x")

    msgs = organize_download(f, dest, "The Agency S02E03", junk)

    assert (dest / "The Agency S02E03.mkv").exists()
    assert not f.exists()
    assert msgs == []


def test_season_pack_files_are_renamed_per_episode(tmp_path, junk):
    dest = tmp_path / "TV" / "From" / "Season 4"
    wrapper = dest / "From.S04.2160p.AMZN.WEB-DL-FLUX"
    wrapper.mkdir(parents=True)
    (wrapper / "From.S04E01.Long.Day.2160p.mkv").write_bytes(b"x")
    (wrapper / "From.S04E02.Night.2160p.mkv").write_bytes(b"x")
    (wrapper / "release.nfo").write_bytes(b"x")
    (wrapper / "info.txt").write_bytes(b"x")

    organize_download(wrapper, dest, "From Season 4", junk)

    assert (dest / "From S04E01.mkv").exists()
    assert (dest / "From S04E02.mkv").exists()
    assert not wrapper.exists()  # emptied wrapper is removed
    assert not (dest / "release.nfo").exists()


def test_single_video_and_subs_take_the_clean_name(tmp_path, junk):
    dest = tmp_path / "Movies"
    wrapper = dest / "Backrooms.2026.2160p.WEB-RDNYB"
    wrapper.mkdir(parents=True)
    (wrapper / "Backrooms.2026.2160p.WEB-RDNYB.mkv").write_bytes(b"x")
    (wrapper / "Backrooms.2026.srt").write_bytes(b"x")
    (wrapper / "sample.mkv").write_bytes(b"x")

    organize_download(wrapper, dest, "Backrooms (2026)", junk)

    assert (dest / "Backrooms (2026).mkv").exists()
    assert (dest / "Backrooms (2026).srt").exists()
    assert not (dest / "sample.mkv").exists()
    assert not wrapper.exists()


def test_subfolders_move_and_junk_dirs_die(tmp_path, junk):
    dest = tmp_path / "TV" / "Show" / "Season 1"
    wrapper = dest / "Show.S01.1080p"
    (wrapper / "Subs").mkdir(parents=True)
    (wrapper / "Sample").mkdir()
    (wrapper / "Subs" / "eng.srt").write_bytes(b"x")
    (wrapper / "Sample" / "s.mkv").write_bytes(b"x")
    (wrapper / "Show.S01E01.1080p.mkv").write_bytes(b"x")

    organize_download(wrapper, dest, "Show Season 1", junk)

    assert (dest / "Subs" / "eng.srt").exists()
    assert not (dest / "Sample").exists()
    assert not wrapper.exists()


def test_collisions_are_reported_not_clobbered(tmp_path, junk):
    dest = tmp_path / "TV" / "Show" / "Season 1"
    dest.mkdir(parents=True)
    (dest / "Show S01E01.mkv").write_bytes(b"old")
    wrapper = dest / "Show.S01.Repack"
    wrapper.mkdir()
    (wrapper / "Show.S01E01.Repack.mkv").write_bytes(b"new")

    msgs = organize_download(wrapper, dest, "Show Season 1", junk)

    assert (dest / "Show S01E01.mkv").read_bytes() == b"old"
    assert wrapper.exists()  # the unplaced file keeps its wrapper
    assert any("Exists" in m for m in msgs)


def test_aria2_control_file_is_cleaned_up(tmp_path, junk):
    dest = tmp_path / "Movies"
    wrapper = dest / "Movie.2026"
    wrapper.mkdir(parents=True)
    (wrapper / "Movie.2026.mkv").write_bytes(b"x")
    ctrl = dest / "Movie.2026.aria2"
    ctrl.write_bytes(b"x")

    organize_download(wrapper, dest, "Movie (2026)", junk)

    assert not ctrl.exists()
