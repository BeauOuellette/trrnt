"""Configuration loading and defaults."""

import os
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_PATH


DEFAULT_CONFIG_PATH = CONFIG_PATH

DEFAULTS: dict[str, Any] = {
    "jackett": {
        "url": "http://localhost:9117",
        "api_key": "",
        "indexers": [],
        "exclude_indexers": [],
        "timeout": 30,
    },
    "aria2": {
        "rpc_url": "http://localhost:6800/jsonrpc",
        "rpc_secret": "",
        "download_dir": str(Path.home() / "Downloads" / "torrents"),
        "max_concurrent": 3,
        # Global throttles, in aria2's notation: bare bytes/sec or a K/M
        # suffix. "0" is unlimited.
        "max_download_rate": "0",
        "max_upload_rate": "0",
        # Seeding. These two zeroes mean opposite things, which is aria2's
        # design, not ours: ratio 0 seeds forever, seed_time 0 never seeds.
        # Blank seed_time means no time limit.
        "seed_ratio": 2.0,
        "seed_time": "",
        # BitTorrent listen port. A range is fine; pin a single port if your
        # VPN forwards one, since that is what makes you connectable.
        "listen_port": "6881-6999",
        # MSE/PE peer encryption: "off", "prefer", or "require".
        "encryption": "prefer",
        # Local Peer Discovery broadcasts your torrents to the LAN, which is
        # outside the tunnel. aria2's own default is off and so is ours.
        "enable_lpd": False,
        "bt_interface": "",
        # aria2 event backend. Empty = aria2's own default (kqueue on macOS).
        # Set to "poll" or "select" if aria2c ever starts spinning a core while
        # idle; some aria2 builds have busy-loop bugs in specific backends.
        "event_poll": "",
    },
    "vpn": {
        "enabled": True,
        "interface_prefix": "utun",
        "real_ip": "",
        "ip_check_url": "https://ipinfo.io/ip",
        "kill_switch_interval": 5,
    },
    "plex": {
        "enabled": True,
        "url": "http://localhost:32400",
        "token": "",
        "library_sections": {"movies": 1, "tv": 2},
    },
    "categories": {
        "movies": {"path": "~/Media/Movies", "quality_prefer": ["2160p", "1080p"]},
        "tv": {"path": "~/Media/TV", "quality_prefer": ["1080p"]},
        "music": {"path": "~/Media/Music"},
        "audiobooks": {"path": "~/Media/Audiobooks"},
        "comics": {"path": "~/Media/Comics"},
        "ebooks": {"path": "~/Media/Ebooks"},
        "other": {"path": "~/Downloads/torrents"},
    },
    "destinations": {
        # What to do when a category's drive isn't connected:
        #   "fallback" → download locally instead and say so
        #   "abort"    → refuse the download with a clear error
        "on_unavailable": "fallback",
        # Where redirected downloads land. The category name is appended,
        # so a movie becomes ~/Downloads/torrents/movies.
        "fallback_path": "~/Downloads/torrents",
        # Require paths on external volumes to be real mount points. Without
        # this, the empty folder an unplugged drive leaves behind would
        # quietly absorb downloads onto the boot disk.
        "require_mount": True,
    },
    "organize": {
        # Ask for a clean name + destination in the TUI before each download.
        "rename_prompt": True,
        # Deselect junk files in aria2 before any payload byte downloads.
        "exclude_junk": True,
        # Treated as junk inside torrents. Ambiguous types are exempted per
        # category in organize.effective_junk (cover art for music/audiobooks,
        # .txt for ebooks); subtitles are never junk.
        "junk_extensions": [
            ".nfo", ".sfv", ".srr", ".srs", ".diz", ".url", ".website",
            ".torrent", ".md5", ".sha1", ".sha256",
            ".txt", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
            ".htm", ".html",
        ],
    },
    "quality_exclude": ["CAM", "TS", "HDCAM"],
    "security": {
        "clamav_enabled": True,
        "clamav_socket": "/tmp/clamd.socket",
        "quarantine_dir": "~/Downloads/quarantine",
        "scan_on_complete": True,
        "block_password_protected_archives": True,
        "blocked_extensions": [
            ".exe", ".bat", ".cmd", ".ps1", ".scr", ".vbs", ".vbe",
            ".msi", ".dll", ".com", ".js", ".hta", ".lnk", ".pif",
            ".wsf", ".app",
        ],
    },
    "display": {"max_results": 50, "default_sort": "seeders", "home": True},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Application configuration."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else DEFAULT_CONFIG_PATH
        self._data = DEFAULTS.copy()
        self._load()

    def reload(self) -> None:
        """Re-read config.yaml — the setup wizard writes it mid-session."""
        self._data = DEFAULTS.copy()
        self._load()

    def _load(self):
        if self._path.exists():
            with open(self._path) as f:
                user_config = yaml.safe_load(f) or {}
            self._data = _deep_merge(DEFAULTS, user_config)

        # Expand ~ in paths
        for cat in self._data.get("categories", {}).values():
            if "path" in cat:
                cat["path"] = str(Path(cat["path"]).expanduser())
        self._data["aria2"]["download_dir"] = str(
            Path(self._data["aria2"]["download_dir"]).expanduser()
        )
        if "fallback_path" in self._data.get("destinations", {}):
            self._data["destinations"]["fallback_path"] = str(
                Path(self._data["destinations"]["fallback_path"]).expanduser()
            )
        if "security" in self._data and "quarantine_dir" in self._data["security"]:
            self._data["security"]["quarantine_dir"] = str(
                Path(self._data["security"]["quarantine_dir"]).expanduser()
            )

    def get(self, *keys: str, default: Any = None) -> Any:
        """Dot-path access: config.get('vpn', 'enabled')"""
        obj = self._data
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
                if obj is None:
                    return default
            else:
                return default
        return obj

    @property
    def data(self) -> dict:
        return self._data

    @property
    def path(self) -> Path:
        return self._path

    def ensure_config_exists(self):
        """Create config directory and copy example if missing."""
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            example = Path(__file__).parent / "config.example.yaml"
            if example.exists():
                import shutil
                shutil.copy(example, self._path)
                return True
        return False
