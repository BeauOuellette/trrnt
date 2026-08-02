"""Category detection: at search time by title/Jackett tag, and after a
download completes by what the files actually are."""

import pytest

from trrnt.search import _detect_category
from trrnt.security import (
    detect_category_from_names,
    detect_content_category,
    is_comic_dir,
    is_ebook_dir,
    is_audiobook_dir,
)


# ── search-time detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "jackett_cat,expected",
    [
        ("7030", "comics"),   # Books/Comics
        ("7020", "ebooks"),   # Books/EBook
        ("7000", "ebooks"),   # Books
        ("7040", "ebooks"),   # Books/Technical
        ("7060", "ebooks"),   # Books/Foreign
        ("2040", "movies"),
        ("5030", "tv"),
        ("3010", "music"),
        ("3030", "audiobooks"),
    ],
)
def test_jackett_category_ids_map_to_folders(jackett_cat, expected):
    assert _detect_category("Some Release Name", [jackett_cat]) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Saga Volume 1 (2013) (Digital) (cbz)", "comics"),
        ("Batman Year One CBR", "comics"),
        ("The Sandman Graphic Novel Collection", "comics"),
        ("Project Hail Mary - Andy Weir (epub)", "ebooks"),
        ("Dune Frank Herbert MOBI", "ebooks"),
        ("Some Title AZW3", "ebooks"),
        ("Complete Ebook Collection 2024", "ebooks"),
    ],
)
def test_book_formats_are_detected_from_the_title(title, expected):
    assert _detect_category(title, None) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        # An SxxExx tag wins — comic adaptations are still TV shows.
        ("The Sandman S01E05 1080p WEB-DL", "tv"),
        ("Marvels Daredevil S03E01 720p", "tv"),
        # Bare "comic"/"manga" must not hijack a video release.
        ("Stand Up Comic Special 2023 1080p", "movies"),
        ("Comic Book Movie 2160p BluRay", "movies"),
    ],
)
def test_video_releases_are_not_mistaken_for_books(title, expected):
    assert _detect_category(title, None) == expected


def test_audiobook_still_takes_precedence_over_music():
    assert _detect_category("Some Book - Unabridged Audiobook MP3", ["3010"]) == "audiobooks"


# ── content-based detection ───────────────────────────────────────────────────

def _make(tmp_path, name, *files):
    d = tmp_path / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_bytes(b"x")
    return d


def test_comic_archive_folder_is_detected(tmp_path):
    d = _make(tmp_path, "pack", "issue-01.cbz", "issue-02.cbz")
    assert is_comic_dir(d)
    assert detect_content_category(d) == "comics"


def test_ebook_folder_is_detected(tmp_path):
    d = _make(tmp_path, "book", "novel.epub")
    assert is_ebook_dir(d)
    assert detect_content_category(d) == "ebooks"


def test_audiobook_folder_still_wins(tmp_path):
    d = _make(tmp_path, "book", "part1.m4b", "cover.epub")
    assert detect_content_category(d) == "audiobooks"


def test_comics_beat_a_stray_ebook(tmp_path):
    d = _make(tmp_path, "pack", "issue-01.cbr", "reading-order.epub")
    assert detect_content_category(d) == "comics"


def test_a_bare_file_is_classified_too(tmp_path):
    f = tmp_path / "single.cbz"
    f.write_bytes(b"x")
    assert detect_content_category(f) == "comics"


def test_nested_files_are_found(tmp_path):
    d = tmp_path / "series" / "vol1" / "chapters"
    d.mkdir(parents=True)
    (d / "ch1.cbz").write_bytes(b"x")
    assert detect_content_category(tmp_path / "series") == "comics"


def test_pdf_alone_is_not_enough(tmp_path):
    """PDFs ride along with almost anything — too weak a signal to re-file on."""
    d = _make(tmp_path, "movie", "movie.mkv", "booklet.pdf")
    assert detect_content_category(d) is None


def test_mp3_alone_is_not_an_audiobook(tmp_path):
    d = _make(tmp_path, "album", "01.mp3", "02.mp3")
    assert detect_content_category(d) is None
    assert not is_audiobook_dir(d)


def test_a_video_download_is_left_alone(tmp_path):
    d = _make(tmp_path, "movie", "movie.mkv", "movie.srt")
    assert detect_content_category(d) is None


def test_missing_path_is_not_a_crash(tmp_path):
    assert detect_content_category(tmp_path / "gone") is None


# ── classification from aria2's file list, before anything is on disk ─────────

def test_the_release_that_slipped_through_is_caught_by_its_file_list():
    """The real case: title said nothing, but aria2 knew it was a .cbr."""
    title = "Supergirl - The World (2026) (digital) (Son of Ultron-Empire)"
    names = [
        "/Users/me/Downloads/torrents/movies/"
        "Supergirl - The World (2026) (digital) (Son of Ultron-Empire).cbr"
    ]
    assert detect_category_from_names(names) == "comics"
    # ...and the title heuristic now catches it too, via the (digital) marker.
    assert _detect_category(title, None) == "comics"


def test_digital_marker_is_not_triggered_by_video_wording():
    assert _detect_category("Some Movie 2024 1080p Digital Copy", None) == "movies"
    assert _detect_category("Artist - Album (Digital Deluxe) FLAC", None) == "music"


def test_file_list_classification_by_extension():
    assert detect_category_from_names(["a/b/book.epub"]) == "ebooks"
    assert detect_category_from_names(["a/part1.m4b", "a/part2.m4b"]) == "audiobooks"
    assert detect_category_from_names(["movie.mkv", "sub.srt"]) is None
    assert detect_category_from_names([]) is None


def test_file_list_keeps_the_same_precedence_as_on_disk():
    assert detect_category_from_names(["x.cbz", "y.epub"]) == "comics"
    assert detect_category_from_names(["x.m4b", "y.cbz"]) == "audiobooks"
