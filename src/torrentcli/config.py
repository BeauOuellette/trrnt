"""Configuration loading and defaults."""

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "tget" / "config.yaml"

DEFAULTS: dict[str, Any] = {
    "jackett": {
        "url": "http://localhost:9117",
        "api_key": "",
        "indexers": [],
        "timeout": 30,
    },
    "aria2": {
        "rpc_url": "http://localhost:6800/jsonrpc",
        "rpc_secret": "",
        "download_dir": str(Path.home() / "Downloads" / "torrents"),
        "max_concurrent": 3,
        "seed_ratio": 2.0,
        "bt_interface": "",
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
        "other": {"path": "~/Downloads/torrents"},
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
    "display": {"max_results": 50, "default_sort": "seeders"},
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
