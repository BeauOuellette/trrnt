"""Settings validation, aria2 translation, and the tiers that keep it honest.

The tier split is the load-bearing part: aria2 answers "OK" to every option
here regardless of whether it applied, so nothing but this table stops the UI
from claiming a listen-port change took effect on a running daemon.
"""

import pytest

from trrnt import settings
from trrnt.daemon import _parse_etime, download_flags


# ── Parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("", "0"), ("0", "0"), ("500K", "500K"), ("2M", "2M"),
    ("1048576", "1048576"), ("1.5M", "1.5M"), (" 2M ", "2M"),
])
def test_rate_accepts_aria2_notation(text, expected):
    assert settings.parse_rate(text) == expected


@pytest.mark.parametrize("text", ["fast", "-1", "2G", "2 M", "M"])
def test_rate_rejects_nonsense(text):
    with pytest.raises(settings.InvalidSetting):
        settings.parse_rate(text)


def test_seed_time_keeps_blank_and_zero_distinct():
    """The two values mean opposite things; collapsing them inverts behaviour.

    "" is no time limit, "0" is never seed. As floats both are falsey, which
    is exactly how this gets implemented backwards.
    """
    assert settings.parse_seed_time("") == ""
    assert settings.parse_seed_time("0") == "0"
    assert settings.parse_seed_time("30") == "30"


def test_seed_ratio_zero_is_legal():
    """0 means seed forever to aria2 — a validator that rejected it as
    "unlimited means blank" would remove the only way to say it."""
    assert settings.parse_ratio("0") == 0.0
    assert settings.parse_ratio("2.5") == 2.5


def test_ratio_rejects_negative():
    with pytest.raises(settings.InvalidSetting):
        settings.parse_ratio("-1")


@pytest.mark.parametrize("text", ["6881", "6881-6999", "6881,6885", "6881-6890,7000"])
def test_port_accepts_aria2_syntax(text):
    assert settings.parse_port(text) == text


@pytest.mark.parametrize("text", ["", "80", "1023", "70000", "6999-6881", "abc"])
def test_port_rejects_unusable(text):
    with pytest.raises(settings.InvalidSetting):
        settings.parse_port(text)


def test_parse_all_reports_every_bad_field_at_once():
    with pytest.raises(settings.InvalidSetting) as e:
        settings.parse_all({
            "max_download_rate": "quick",
            "listen_port": "80",
            "seed_ratio": "2.0",
        })
    message = str(e.value)
    assert "Max download" in message
    assert "Listen port" in message
    assert "Seed ratio" not in message


# ── Translation to aria2 ─────────────────────────────────────────────────────

VALUES = {
    "max_download_rate": "1M",
    "max_upload_rate": "500K",
    "max_concurrent": 4,
    "seed_ratio": 2.0,
    "seed_time": "",
    "encryption": "require",
    "listen_port": "6881-6999",
    "enable_lpd": False,
}


def test_live_tier_is_only_the_global_throttles():
    options = settings.aria2_options(VALUES, tiers=(settings.TIER_LIVE,))
    assert options == {
        "max-overall-download-limit": "1M",
        "max-overall-upload-limit": "500K",
        "max-concurrent-downloads": "4",
    }


def test_restart_options_never_leak_into_a_live_push():
    """aria2 would answer OK and change nothing — the silent no-op this
    whole tier split exists to prevent."""
    options = settings.aria2_options(
        VALUES, tiers=(settings.TIER_LIVE, settings.TIER_NEW)
    )
    assert "listen-port" not in options
    assert "dht-listen-port" not in options
    assert "bt-enable-lpd" not in options


def test_blank_seed_time_is_omitted_not_sent_as_zero():
    """Sending seed-time=0 for "no time limit" would stop seeding instantly."""
    options = settings.aria2_options(VALUES, tiers=(settings.TIER_NEW,))
    assert "seed-time" not in options

    never = settings.aria2_options({**VALUES, "seed_time": "0"},
                                   tiers=(settings.TIER_NEW,))
    assert never["seed-time"] == "0"


@pytest.mark.parametrize("mode,expected", [
    ("off", {"bt-require-crypto": "false", "bt-min-crypto-level": "plain"}),
    ("prefer", {"bt-require-crypto": "false", "bt-min-crypto-level": "arc4"}),
    ("require", {"bt-require-crypto": "true", "bt-min-crypto-level": "arc4"}),
])
def test_encryption_modes_map_to_the_documented_pair(mode, expected):
    options = settings.aria2_options({"encryption": mode}, tiers=(settings.TIER_NEW,))
    assert options == expected


def test_lpd_off_is_sent_explicitly():
    """Omitting it would inherit whatever a user's ~/.aria2/aria2.conf says."""
    options = settings.aria2_options(VALUES, tiers=(settings.TIER_RESTART,))
    assert options["bt-enable-lpd"] == "false"


# ── Change detection ─────────────────────────────────────────────────────────

def test_changed_tiers_only_names_tiers_that_moved():
    before = dict(VALUES)
    assert settings.changed_tiers(before, dict(VALUES)) == set()
    assert settings.changed_tiers(before, {**VALUES, "max_upload_rate": "1M"}) == {
        settings.TIER_LIVE
    }
    assert settings.changed_tiers(before, {**VALUES, "listen_port": "6881"}) == {
        settings.TIER_RESTART
    }


# ── Plain-English seeding ────────────────────────────────────────────────────

@pytest.mark.parametrize("ratio,seed_time,fragment", [
    (2.0, "", "ratio 2"),
    (0.0, "", "forever"),
    (2.0, "0", "Never seeds"),
    (0.0, "0", "Never seeds"),
    (2.0, "30", "whichever comes first"),
])
def test_describe_seeding_states_what_the_zeroes_mean(ratio, seed_time, fragment):
    assert fragment in settings.describe_seeding(ratio, seed_time)


# ── Spawn flags ──────────────────────────────────────────────────────────────

def test_download_flags_carry_config_through_to_aria2():
    flags = download_flags({
        "listen_port": "6881",
        "enable_lpd": True,
        "encryption": "require",
        "max_upload_rate": "500K",
        "seed_ratio": 1.0,
        "seed_time": "60",
        "max_download_rate": "0",
        "max_concurrent": 2,
    })
    assert "--listen-port=6881" in flags
    assert "--dht-listen-port=6881" in flags
    assert "--bt-enable-lpd=true" in flags
    assert "--bt-require-crypto=true" in flags
    assert "--max-overall-upload-limit=500K" in flags
    assert "--seed-time=60" in flags
    assert "--max-concurrent-downloads=2" in flags


def test_download_flags_fall_back_to_trrnt_defaults_not_aria2s():
    """A config.yaml written before these keys existed should keep trrnt's
    behaviour — aria2's own defaults seed to 1.0 and allow plaintext."""
    flags = download_flags({})
    assert "--seed-ratio=2.0" in flags
    assert "--bt-min-crypto-level=arc4" in flags
    assert "--bt-enable-lpd=false" in flags
    assert "--listen-port=6881-6999" in flags


def test_download_flags_keep_the_static_tuning():
    flags = download_flags({})
    assert "--enable-dht=true" in flags
    assert "--enable-peer-exchange=true" in flags
    assert "--bt-max-peers=100" in flags


# ── ps(1) elapsed time ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,seconds", [
    ("      05:12", 312),
    ("01:05:12", 3912),
    ("2-01:05:12", 176712),
    ("00:07", 7),
])
def test_parse_etime(text, seconds):
    assert _parse_etime(text) == seconds


@pytest.mark.parametrize("text", ["", "   ", "not-a-time"])
def test_parse_etime_gives_up_quietly(text):
    assert _parse_etime(text) is None
