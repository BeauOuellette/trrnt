"""aria2 JSON-RPC download manager."""

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

console = Console()


def discard_partial(old_dir: Path, name: str) -> None:
    """Delete what aria2 left behind in a folder it was redirected away from.

    Only removes a partial that still has its .aria2 control file beside it —
    that marks it as an unfinished aria2 download, and stops this from ever
    touching a completed file that happens to share the name.
    """
    if not name:
        return
    control = old_dir / f"{name}.aria2"
    if not control.exists():
        return
    control.unlink(missing_ok=True)
    stale = old_dir / name
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)
    else:
        stale.unlink(missing_ok=True)


async def reroute_in_flight(client: "Aria2Client", config, dl) -> str | None:
    """Send a running download to the folder its contents call for.

    aria2 knows a torrent's file list as soon as its metadata resolves, and
    accepts a `dir` change on an active download — it continues into the new
    folder rather than starting over. Correcting the destination here costs
    the few megabytes already fetched, instead of a full move at the end that
    would also have to break seeding.

    Returns the corrected category, or None when nothing needed changing.
    Raises whatever aria2 raises if the directory change itself fails.
    """
    from .security import detect_category_from_names
    from .storage import DestinationUnavailable, resolve_destination

    try:
        names = await client.get_files(dl.gid)
    except Exception:
        return None  # metadata not resolved yet — caller retries

    category = detect_category_from_names(names)
    if not category:
        return None

    try:
        target = Path(resolve_destination(config, category).path)
    except DestinationUnavailable:
        return None

    old_dir = Path(dl.dir)
    if target == old_dir:
        return None  # the search-time guess was right

    target.mkdir(parents=True, exist_ok=True)
    await client.change_dir(dl.gid, str(target))
    discard_partial(old_dir, dl.name)
    return category


@dataclass
class DownloadStatus:
    """Status of a single download."""
    gid: str = ""
    status: str = ""  # active, waiting, paused, error, complete, removed
    name: str = ""
    total_bytes: int = 0
    completed_bytes: int = 0
    download_speed: int = 0
    upload_speed: int = 0
    seeders: int = 0
    connections: int = 0
    error_message: str = ""
    dir: str = ""

    @property
    def progress(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return (self.completed_bytes / self.total_bytes) * 100

    @property
    def speed_human(self) -> str:
        speed = self.download_speed
        for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
            if speed < 1024:
                return f"{speed:.1f} {unit}"
            speed /= 1024
        return f"{speed:.1f} TB/s"

    @property
    def size_human(self) -> str:
        size = self.total_bytes
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    @property
    def eta(self) -> str:
        if self.download_speed == 0 or self.status != "active":
            return "∞"
        remaining = self.total_bytes - self.completed_bytes
        seconds = remaining / self.download_speed
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        else:
            hours = int(seconds // 3600)
            mins = int((seconds % 3600) // 60)
            return f"{hours}h {mins}m"


_PUBLIC_TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://movies.zsw.ca:6969/announce",
])


class Aria2Client:
    """JSON-RPC client for aria2."""

    def __init__(self, config: dict):
        self.rpc_url = config.get("rpc_url", "http://localhost:6800/jsonrpc")
        self.secret = config.get("rpc_secret", "")
        self.download_dir = config.get("download_dir", str(Path.home() / "Downloads" / "torrents"))
        self.max_concurrent = config.get("max_concurrent", 3)
        self.seed_ratio = config.get("seed_ratio", 2.0)
        self.bt_interface = config.get("bt_interface", "")

    async def _call(self, method: str, params: list | None = None) -> Any:
        """Make a JSON-RPC call to aria2."""
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": f"aria2.{method}",
            "params": [],
        }

        # Add secret token if configured
        if self.secret:
            payload["params"].append(f"token:{self.secret}")

        if params:
            payload["params"].extend(params)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self.rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                raise RuntimeError(f"aria2 error: {data['error'].get('message', data['error'])}")

            return data.get("result")

    async def add_magnet(
        self,
        magnet: str,
        download_dir: str | None = None,
        category: str = "",
    ) -> str:
        """Add a magnet link. Returns GID."""
        options: dict[str, str] = {
            "dir": download_dir or self.download_dir,
            "bt-tracker": _PUBLIC_TRACKERS,
        }

        if self.seed_ratio > 0:
            options["seed-ratio"] = str(self.seed_ratio)

        if self.bt_interface:
            options["interface"] = self.bt_interface

        # Create category subdirectory
        if category:
            cat_dir = str(Path(options["dir"]) / category)
            Path(cat_dir).mkdir(parents=True, exist_ok=True)
            options["dir"] = cat_dir

        result = await self._call("addUri", [[magnet], options])
        return result

    async def add_torrent_url(
        self,
        url: str,
        download_dir: str | None = None,
        category: str = "",
    ) -> str:
        """Add a .torrent URL. Returns GID."""
        options: dict[str, str] = {
            "dir": download_dir or self.download_dir,
            "bt-tracker": _PUBLIC_TRACKERS,
        }

        if self.seed_ratio > 0:
            options["seed-ratio"] = str(self.seed_ratio)

        if self.bt_interface:
            options["interface"] = self.bt_interface

        if category:
            cat_dir = str(Path(options["dir"]) / category)
            Path(cat_dir).mkdir(parents=True, exist_ok=True)
            options["dir"] = cat_dir

        result = await self._call("addUri", [[url], options])
        return result

    async def change_dir(self, gid: str, download_dir: str) -> str:
        """Redirect an in-flight download into a different folder.

        aria2 accepts `dir` on an active download and continues into the new
        location without restarting — but it leaves the partial file and its
        .aria2 control file behind in the old one, so callers must clean up.
        """
        return await self._call("changeOption", [gid, {"dir": download_dir}])

    async def get_files(self, gid: str) -> list[str]:
        """File paths inside a download, available once metadata resolves.

        Empty while a magnet is still fetching its metadata.
        """
        result = await self._call("getFiles", [gid])
        return [f.get("path", "") for f in (result or []) if f.get("path")]

    async def get_status(self, gid: str) -> DownloadStatus:
        """Get status of a specific download."""
        keys = [
            "gid", "status", "totalLength", "completedLength",
            "downloadSpeed", "uploadSpeed", "numSeeders", "connections",
            "errorMessage", "dir", "bittorrent",
        ]
        result = await self._call("tellStatus", [gid, keys])
        return self._parse_status(result)

    async def get_active(self) -> list[DownloadStatus]:
        """Get all active downloads."""
        result = await self._call("tellActive")
        return [self._parse_status(r) for r in (result or [])]

    async def get_waiting(self, offset: int = 0, count: int = 100) -> list[DownloadStatus]:
        """Get waiting/queued downloads."""
        result = await self._call("tellWaiting", [offset, count])
        return [self._parse_status(r) for r in (result or [])]

    async def get_stopped(self, offset: int = 0, count: int = 20) -> list[DownloadStatus]:
        """Get stopped/completed/errored downloads."""
        result = await self._call("tellStopped", [offset, count])
        return [self._parse_status(r) for r in (result or [])]

    async def pause(self, gid: str) -> str:
        """Pause a download."""
        return await self._call("pause", [gid])

    async def unpause(self, gid: str) -> str:
        """Resume a download."""
        return await self._call("unpause", [gid])

    async def pause_all(self) -> str:
        """Pause all downloads (used by kill switch)."""
        return await self._call("pauseAll")

    async def unpause_all(self) -> str:
        """Resume all downloads."""
        return await self._call("unpauseAll")

    async def remove(self, gid: str) -> str:
        """Remove a download."""
        return await self._call("remove", [gid])

    async def force_remove(self, gid: str) -> str:
        """Force remove a download (works on any state including paused)."""
        return await self._call("forceRemove", [gid])

    async def purge_completed(self) -> str:
        """Remove completed/errored/removed download results."""
        return await self._call("purgeDownloadResult")

    async def remove_result(self, gid: str) -> str:
        """Remove a single completed/errored download result."""
        return await self._call("removeDownloadResult", [gid])

    async def get_global_stat(self) -> dict:
        """Get global download/upload stats."""
        return await self._call("getGlobalStat")

    async def check_connection(self) -> bool:
        """Verify aria2 RPC is reachable."""
        try:
            result = await self._call("getVersion")
            return "version" in result
        except Exception:
            return False

    def _parse_status(self, data: dict) -> DownloadStatus:
        """Parse aria2 status response into DownloadStatus."""
        bt_info = data.get("bittorrent", {})
        name = ""
        if bt_info and "info" in bt_info:
            name = bt_info["info"].get("name", "")

        return DownloadStatus(
            gid=data.get("gid", ""),
            status=data.get("status", "unknown"),
            name=name or data.get("gid", "unknown"),
            total_bytes=int(data.get("totalLength", 0)),
            completed_bytes=int(data.get("completedLength", 0)),
            download_speed=int(data.get("downloadSpeed", 0)),
            upload_speed=int(data.get("uploadSpeed", 0)),
            seeders=int(data.get("numSeeders", 0)),
            connections=int(data.get("connections", 0)),
            error_message=data.get("errorMessage", ""),
            dir=data.get("dir", ""),
        )
