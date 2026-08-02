"""The tget → trrnt path fallback.

Renaming the command must not orphan a configured machine. These pin the one
rule that makes that true: trrnt wins, unless it is absent or empty and tget
is real.
"""

from trrnt.paths import resolve


# ── Config dir: config.yaml is what makes a directory real ────────────────────

def test_fresh_machine_gets_the_trrnt_path(tmp_path):
    assert resolve(tmp_path, "config.yaml") == tmp_path / "trrnt"


def test_existing_tget_config_keeps_being_used(tmp_path):
    (tmp_path / "tget").mkdir()
    (tmp_path / "tget" / "config.yaml").write_text("jackett: {}\n")

    assert resolve(tmp_path, "config.yaml") == tmp_path / "tget"


def test_trrnt_config_wins_once_it_exists(tmp_path):
    for name in ("tget", "trrnt"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.yaml").write_text("jackett: {}\n")

    assert resolve(tmp_path, "config.yaml") == tmp_path / "trrnt"


def test_empty_trrnt_dir_does_not_beat_a_configured_tget(tmp_path):
    # The failure this guards: a stray mkdir (or a half-finished wizard) leaves
    # ~/.config/trrnt empty, and the user silently loses their real config.
    (tmp_path / "trrnt").mkdir()
    (tmp_path / "tget").mkdir()
    (tmp_path / "tget" / "config.yaml").write_text("jackett: {}\n")

    assert resolve(tmp_path, "config.yaml") == tmp_path / "tget"


# ── State dir: any of four files can make it real, so the dir itself is ────────

def test_state_dir_falls_back_on_directory_alone(tmp_path):
    # No marker file: a tget state dir holding only aria2.pid must still win,
    # or a running daemon's pidfile is stranded and it never gets reaped.
    (tmp_path / "tget").mkdir()
    (tmp_path / "tget" / "aria2.pid").write_text("4242\n")

    assert resolve(tmp_path) == tmp_path / "tget"


def test_state_dir_prefers_trrnt_when_both_exist(tmp_path):
    (tmp_path / "tget").mkdir()
    (tmp_path / "trrnt").mkdir()

    assert resolve(tmp_path) == tmp_path / "trrnt"


def test_state_dir_on_a_fresh_machine(tmp_path):
    assert resolve(tmp_path) == tmp_path / "trrnt"


def test_a_file_named_tget_is_not_a_state_dir(tmp_path):
    (tmp_path / "tget").write_text("not a directory")

    assert resolve(tmp_path) == tmp_path / "trrnt"


# ── Version reporting ─────────────────────────────────────────────────────────

def test_version_is_reported_not_swallowed():
    """The home screen's version must be the real one.

    branding.VERSION used to come from importlib.metadata keyed on the
    distribution name, with a "dev" fallback for running out of a checkout.
    Renaming the distribution made that lookup miss, and the fallback made the
    miss look intentional — no test failed, the home screen just said "vdev".
    """
    from trrnt import __version__
    from trrnt.branding import VERSION

    assert VERSION == __version__
    assert VERSION != "dev"


def test_cli_version_matches_the_package():
    from click.testing import CliRunner

    from trrnt import __version__
    from trrnt.main import cli

    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
