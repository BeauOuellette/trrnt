"""Parsing scene release titles into clean names and library folders.

The example titles here are real shapes from the library this feature files
into — site-prefix spam, dotted separators, per-service tags — because the
parser only earns its keep against what actually arrives.
"""

from pathlib import Path

from torrentcli.naming import (
    ParsedRelease,
    parse_release_name,
    recommend_folder,
    suggest_name,
)


# ── parse_release_name ────────────────────────────────────────────────────────

def test_the_motivating_example():
    p = parse_release_name("www.Torrenting.com.The.Agency.S02E03.2160p.SKIZ.DDP5.1.x265")
    assert p.title == "The Agency"
    assert (p.season, p.episode) == (2, 3)


def test_bracketed_site_prefix_is_stripped():
    p = parse_release_name("[ www.UIndex.org ] - The.Agency.S02E03.1080p.WEB")
    assert p.title == "The Agency"
    assert (p.season, p.episode) == (2, 3)


def test_bare_domain_with_separator_is_stripped():
    p = parse_release_name("Torrenting.com - Dark Winds S03E01 720p")
    assert p.title == "Dark Winds"


def test_a_bracketed_title_is_not_mistaken_for_a_site():
    # "[REC]" is a movie; only bracket groups containing a dot are spam.
    p = parse_release_name("[REC] 2007 1080p BluRay")
    assert p.title == "REC"
    assert p.year == 2007


def test_episode_title_words_are_dropped():
    p = parse_release_name(
        "FROM.S04E08.Heavy.Is.the.Head.2160p.AMZN.WEB-DL.DDP5.1.H.265-FLUX"
    )
    assert p.title == "From"
    assert (p.season, p.episode) == (4, 8)


def test_small_words_stay_lowercase():
    p = parse_release_name("House.Of.The.Dragon.S02E04.2160p.10bit.HDR.WEBRip")
    assert p.title == "House of the Dragon"


def test_season_pack_with_both_season_forms():
    p = parse_release_name(
        "Foundation (2021) Season 3 S03 (2160p ATVP WEB-DL x265 HEVC 10bit DDP 5.1 Vyndros)"
    )
    assert p.title == "Foundation"
    assert p.season == 3
    assert p.episode is None
    assert p.year == 2021


def test_cross_notation_episode():
    p = parse_release_name("Archer 7x05 HDTV x264")
    assert p.title == "Archer"
    assert (p.season, p.episode) == (7, 5)


def test_movie_with_year():
    p = parse_release_name("Backrooms.2026.2160p.iT.WEB-DL.DDP5.1.Atmos.H.265-RDNYB")
    assert p.title == "Backrooms"
    assert p.year == 2026
    assert not p.is_episodic


def test_year_as_opening_token_is_title():
    p = parse_release_name("1917 2019 1080p BluRay x264")
    assert p.title == "1917"
    assert p.year == 2019


def test_last_year_wins_so_titles_keep_their_own_number():
    p = parse_release_name("Blade Runner 2049 2017 2160p WEB-DL")
    assert p.title == "Blade Runner 2049"
    assert p.year == 2017


def test_no_markers_passes_through():
    p = parse_release_name("F1 The Movie")
    assert p.title == "F1 The Movie"
    assert p.year is None


def test_complete_series_reads_as_tv_shaped_title():
    p = parse_release_name("The.Wire.Complete.Series.1080p.BluRay")
    assert p.title == "The Wire"
    assert p.season is None


def test_degenerate_title_still_offers_something():
    p = parse_release_name("S02E03.1080p.WEB")
    assert (p.season, p.episode) == (2, 3)


# ── suggest_name ──────────────────────────────────────────────────────────────

def test_episode_name_format():
    p = ParsedRelease(title="The Agency", season=2, episode=3)
    assert suggest_name(p, "tv") == "The Agency S02E03"


def test_season_pack_name_format():
    p = ParsedRelease(title="Foundation", season=3)
    assert suggest_name(p, "tv") == "Foundation Season 3"


def test_movie_name_carries_year():
    p = ParsedRelease(title="Backrooms", year=2026)
    assert suggest_name(p, "movies") == "Backrooms (2026)"


def test_non_movie_name_skips_year():
    p = ParsedRelease(title="Some Album", year=2020)
    assert suggest_name(p, "music") == "Some Album"


# ── recommend_folder ──────────────────────────────────────────────────────────

def test_tv_episode_lands_in_show_season_folder(tmp_path):
    p = parse_release_name("The.Agency.S02E03.2160p.WEB")
    assert recommend_folder(p, "tv", tmp_path) == tmp_path / "The Agency" / "Season 2"


def test_existing_show_folder_spelling_is_reused(tmp_path):
    (tmp_path / "From").mkdir()
    p = parse_release_name("FROM.S04E09.The.Calm.Before.2160p.AMZN.WEB-DL")
    assert recommend_folder(p, "tv", tmp_path) == tmp_path / "From" / "Season 4"


def test_movies_stay_flat_in_the_root(tmp_path):
    p = parse_release_name("Backrooms.2026.2160p.WEB-DL")
    assert recommend_folder(p, "movies", tmp_path) == tmp_path


def test_tv_without_season_stops_at_the_show(tmp_path):
    p = ParsedRelease(title="Dark Winds")
    assert recommend_folder(p, "tv", tmp_path) == tmp_path / "Dark Winds"


def test_other_categories_get_a_named_folder(tmp_path):
    p = ParsedRelease(title="Some Book")
    assert recommend_folder(p, "audiobooks", tmp_path) == tmp_path / "Some Book"


def test_typed_name_with_episode_recommends_season_folder(tmp_path):
    # The live-recompute path: the user types a clean name into the prompt
    # and the folder tracks what they typed.
    p = parse_release_name("The Agency s02e03")
    assert recommend_folder(p, "tv", tmp_path) == tmp_path / "The Agency" / "Season 2"
