"""Where trrnt keeps its config and state on disk.

The command was `tget` before it was `trrnt`, and its files landed in
`~/.config/tget` and `~/.local/state/tget`. A fresh install now uses the
trrnt paths, but a machine that already has the tget ones keeps using them.

That fallback is the whole point: renaming the command must not orphan a
working setup, and it must not hand the user a migration step either. There
is nothing to move and nothing to explain — the old paths simply stay
authoritative on the machines that have them.

Resolution happens once at import, which is also when the old constants were
evaluated. A dir that appears mid-run does not change where this run looks.
"""

from pathlib import Path

APP_NAME = "trrnt"
LEGACY_APP_NAME = "tget"


def resolve(base: Path, marker: str = "") -> Path:
    """Return the trrnt dir under `base`, or the tget one if only it is live.

    `marker` is the file whose presence proves a directory is real rather than
    an empty leftover. Pass "" to test the directory itself.
    """
    current = base / APP_NAME
    legacy = base / LEGACY_APP_NAME

    def populated(d: Path) -> bool:
        return (d / marker).exists() if marker else d.is_dir()

    if not populated(current) and populated(legacy):
        return legacy
    return current


# config.yaml is the artifact that decides which config dir is the real one —
# an empty ~/.config/trrnt left by a stray mkdir must not win over a tget dir
# that holds an actual configured setup.
CONFIG_DIR = resolve(Path.home() / ".config", "config.yaml")
CONFIG_PATH = CONFIG_DIR / "config.yaml"

# State follows the XDG convention. Any of aria2.pid, aria2.session,
# organize.json or update-check.json makes a state dir worth keeping, so the
# directory's own existence is the test — splitting these across two homes
# would strand a running daemon's pidfile.
STATE_DIR = resolve(Path.home() / ".local" / "state")
