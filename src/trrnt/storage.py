"""Destination resolution — keep downloads off drives that aren't connected.

Media categories normally point at an external drive. When that drive is
unplugged the configured path isn't merely missing, it's unwritable: /Volumes
is root-owned, so aria2 accepts the torrent, fetches metadata, and only then
fails on directory creation. And if a stale /Volumes/<name> folder is ever
left behind, downloads land on the boot disk instead — which also makes the
real drive mount alongside it as "<name> 1" the next time it's plugged in.

Every site that picks a download directory goes through resolve_destination()
so an offline drive degrades to a local path with a notice, rather than
failing halfway through.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_FALLBACK = "~/Downloads/torrents"

# Directories whose immediate children are mount points for removable or
# network volumes. Anything below one of these needs its volume present.
_MOUNT_PARENTS = ("/Volumes",) if sys.platform == "darwin" else ("/mnt", "/media")


class DestinationUnavailable(RuntimeError):
    """The target drive is offline and `on_unavailable: abort` is configured."""


@dataclass(frozen=True)
class Destination:
    """Where a download should go, and whether that was the original plan."""

    path: str
    configured: str
    volume: str = ""

    @property
    def redirected(self) -> bool:
        return self.path != self.configured

    @property
    def notice(self) -> str:
        """Short explanation of the redirect. Empty when nothing moved."""
        if not self.redirected:
            return ""
        return f"{self.volume or self.configured} offline → {shorten(self.path)}"


def shorten(path: str | Path) -> str:
    """Render a path with ~ standing in for the home directory."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def volume_root(path: str | Path) -> Path | None:
    """The mount point a path depends on, or None if it lives on the boot disk.

    /Volumes/Media HDD/Media/Movies -> /Volumes/Media HDD
    """
    parts = Path(path).expanduser().parts
    for parent in _MOUNT_PARENTS:
        prefix = Path(parent).parts
        if len(parts) > len(prefix) and parts[: len(prefix)] == prefix:
            return Path(*parts[: len(prefix) + 1])
    return None


def is_available(path: str | Path, require_mount: bool = True) -> bool:
    """Is this path backed by storage we can write to right now?"""
    root = volume_root(path)
    if root is None:
        return True  # boot disk — the directory gets created on demand
    if not root.exists():
        return False
    # An existing folder that isn't a mount point is the leftover shell of an
    # unplugged drive. Writing into it fills the boot disk silently.
    return os.path.ismount(root) if require_mount else True


def resolve_destination(config, category: str, default_dir: str = "") -> Destination:
    """Pick the download directory for a category, honouring drive availability.

    Raises DestinationUnavailable when the drive is offline and the configured
    policy is "abort".
    """
    categories = config.get("categories", default={}) or {}
    # A category defined but left blank falls through to default_dir, not to
    # "other" — matching how these paths have always resolved.
    cat_config = categories.get(category)
    if cat_config is None:
        cat_config = categories.get("other") or {}
    configured = str(
        Path(cat_config.get("path") or default_dir or DEFAULT_FALLBACK).expanduser()
    )

    policy = config.get("destinations", default={}) or {}
    require_mount = policy.get("require_mount", True)

    if is_available(configured, require_mount):
        return Destination(path=configured, configured=configured)

    root = volume_root(configured)
    volume = root.name if root else ""

    if policy.get("on_unavailable", "fallback") == "abort":
        raise DestinationUnavailable(f"{volume or configured} is not connected")

    fallback = Path(policy.get("fallback_path") or DEFAULT_FALLBACK).expanduser()
    if not is_available(fallback, require_mount):
        # The configured fallback is itself on a missing drive.
        fallback = Path(DEFAULT_FALLBACK).expanduser()

    return Destination(
        path=str(fallback / category),
        configured=configured,
        volume=volume,
    )
