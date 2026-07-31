"""Interactive TUI powered by Textual."""

import asyncio
import re
import shutil
import subprocess
import platform
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Static,
)

from .config import Config
from .download import Aria2Client, DownloadStatus
from .plex import PlexClient
from .search import JackettSearch, TorrentResult
from .security import SecurityScanner, is_audiobook_dir
from .storage import DestinationUnavailable, resolve_destination
from .vpn import VPNGuard


# Quality tags to extract from torrent titles
_QUALITY_TAGS = [
    "2160p", "1080p", "720p", "480p", "4K", "UHD",
    "HDR", "HDR10", "HDR10+", "DV", "Dolby Vision",
    "REMUX", "BluRay", "Blu-ray", "BrRip", "BDRip", "WEB-DL", "WEBRip", "HDTV",
    "x264", "x265", "H.264", "H264", "H.265", "H265", "HEVC", "AVC", "AV1",
    "AAC", "DTS", "DTS-HD", "DTS-HD MA", "Atmos", "TrueHD", "FLAC", "AC3", "EAC3",
    "DD5.1", "DDP5.1", "7.1", "5.1",
    "IMAX", "10bit", "10Bit",
]


def _extract_quality_tags(title: str) -> list[str]:
    """Extract quality/format tags from a torrent title."""
    found = []
    title_upper = title.upper()
    for tag in _QUALITY_TAGS:
        if tag.upper() in title_upper:
            if tag not in found:
                found.append(tag)
    return found


class InspectScreen(ModalScreen[str]):
    """Modal screen showing torrent result details."""

    CSS = """
    InspectScreen {
        align: center middle;
    }
    #inspect-dialog {
        width: 80;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #inspect-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    .inspect-row {
        height: 1;
        margin: 0;
    }
    .inspect-label {
        color: $text-muted;
        width: 16;
    }
    .inspect-value {
        color: $text;
    }
    #inspect-tags {
        margin-top: 1;
        margin-bottom: 1;
        color: $success;
    }
    #inspect-buttons {
        height: 3;
        margin-top: 1;
        align: center middle;
    }
    #inspect-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("d", "download", "Download"),
        Binding("o", "open_url", "Open URL"),
    ]

    def __init__(self, result: TorrentResult):
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        r = self.result
        tags = _extract_quality_tags(r.title)
        magnet_hash = ""
        if r.magnet:
            m = re.search(r"btih:([a-fA-F0-9]+)", r.magnet)
            if m:
                magnet_hash = m.group(1)[:16] + "..."

        with VerticalScroll(id="inspect-dialog"):
            yield Label(r.title, id="inspect-title")

            yield Static(f"[dim]Source:[/]       {r.indexer}", classes="inspect-row")
            yield Static(f"[dim]Size:[/]         {r.size_human} ({r.size_bytes:,} bytes)", classes="inspect-row")
            yield Static(f"[dim]Seeders:[/]      [green]{r.seeders}[/]", classes="inspect-row")
            yield Static(f"[dim]Leechers:[/]     [red]{r.leechers}[/]", classes="inspect-row")
            yield Static(f"[dim]Category:[/]     {r.category}", classes="inspect-row")
            yield Static(f"[dim]Published:[/]    {r.pub_date or 'unknown'}", classes="inspect-row")
            yield Static(f"[dim]URL type:[/]     {'magnet' if r.magnet else 'torrent file'}", classes="inspect-row")
            if magnet_hash:
                yield Static(f"[dim]Info hash:[/]    [dim]{magnet_hash}[/]", classes="inspect-row")
            if r.info_url and not r.info_url.startswith("magnet:"):
                yield Static(f"[dim]Info URL:[/]     [underline]{r.info_url[:60]}[/]", classes="inspect-row")

            if tags:
                yield Static(f"[dim]Quality:[/]      [green]{' | '.join(tags)}[/]", id="inspect-tags")

            with Horizontal(id="inspect-buttons"):
                yield Button("Download [D]", variant="success", id="btn-download")
                if r.info_url and not r.info_url.startswith("magnet:"):
                    yield Button("Open URL [O]", variant="primary", id="btn-open")
                yield Button("Close [Esc]", variant="default", id="btn-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-download":
            self.dismiss("download")
        elif event.button.id == "btn-open":
            self.dismiss("open")
        else:
            self.dismiss("")

    def action_close(self) -> None:
        self.dismiss("")

    def action_download(self) -> None:
        self.dismiss("download")

    def action_open_url(self) -> None:
        self.dismiss("open")


class StatusBar(Static):
    """Top status bar showing VPN + download state."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.vpn_status = "checking..."
        self.active_downloads = 0
        self.download_speed = "0 B/s"
        self.clamav_status = "..."

    def render(self) -> str:
        return (
            f" VPN: {self.vpn_status}"
            f"  │  Downloads: {self.active_downloads}"
            f"  │  ↓ {self.download_speed}"
            f"  │  AV: {self.clamav_status}"
        )


class TGetApp(App):
    """Main TUI application."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #status-bar {
        dock: top;
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    #search-input {
        dock: top;
        margin: 1 1 0 1;
    }
    #results-table {
        height: 1fr;
        margin: 0 1;
    }
    #downloads-table {
        height: auto;
        max-height: 10;
        margin: 0 1 1 1;
    }
    #info-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    .section-label {
        height: 1;
        margin: 1 1 0 1;
        color: $text-muted;
        text-style: bold;
    }
    LoadingIndicator {
        height: 3;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+s", "focus_search", "Search", priority=True),
        Binding("ctrl+a", "select_all", "Select All", priority=True),
        Binding("ctrl+d", "download_selected", "Download", priority=True),
        Binding("ctrl+p", "pause_all", "Pause All", priority=True),
        Binding("ctrl+r", "refresh_downloads", "Refresh", priority=True),
        Binding("ctrl+x", "clear_finished", "Clear Done", priority=True),
        Binding("ctrl+w", "remove_download", "Remove", priority=True),
        Binding("ctrl+i", "inspect_result", "Inspect", priority=True),
        Binding("ctrl+h", "inspect_download", "Health", priority=True),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.jackett = JackettSearch(config.get("jackett"))
        self.aria2 = Aria2Client(config.get("aria2"))
        self.plex = PlexClient(config.get("plex"))
        self.vpn = VPNGuard(config.get("vpn"))
        self.security = SecurityScanner(config.get("security"))
        self.search_results: list[TorrentResult] = []
        self.selected_indices: set[int] = set()
        self._scanned_gids: set[str] = set()  # Track downloads already scanned
        self._stall_tracker: dict[str, int] = {}  # gid -> consecutive stall checks

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status-bar")
        yield Input(placeholder="Search torrents... (Enter to search)", id="search-input")
        yield Label("Results", classes="section-label")
        yield DataTable(id="results-table")
        yield Label("Downloads", classes="section-label")
        yield DataTable(id="downloads-table")
        yield Static("", id="info-bar")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize tables and start background tasks."""
        # Results table
        results = self.query_one("#results-table", DataTable)
        results.cursor_type = "row"
        results.zebra_stripes = True
        results.add_columns("", "#", "Name", "Size", "S", "L", "Source")

        # Downloads table
        downloads = self.query_one("#downloads-table", DataTable)
        downloads.cursor_type = "row"
        downloads.zebra_stripes = True
        downloads.add_columns("Name", "Size", "Progress", "Speed", "ETA", "Status")

        # Start background tasks
        self.check_vpn_status()
        self.check_clamav_status()
        self.refresh_downloads_loop()

        # Register VPN kill switch
        self.vpn.on_vpn_drop(self._on_vpn_drop)
        if self.config.get("vpn", "enabled"):
            self._run_kill_switch()

    @work(group="kill_switch")
    async def _run_kill_switch(self) -> None:
        """Run VPN kill switch as a Textual worker."""
        await self.vpn.kill_switch_loop()

    async def _on_vpn_drop(self):
        """Kill switch callback — pause all downloads."""
        try:
            await self.aria2.pause_all()
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.vpn_status = "[bold red]DISCONNECTED — downloads paused[/]"
            status_bar.refresh()
            self.notify("VPN dropped! All downloads paused.", severity="error")
        except Exception:
            pass

    @work(exclusive=True, group="vpn")
    async def check_vpn_status(self) -> None:
        """Check VPN status and update status bar."""
        status_bar = self.query_one("#status-bar", StatusBar)
        vpn_status = await self.vpn.check()
        if vpn_status.connected:
            iface = vpn_status.interface
            ip_display = vpn_status.vpn_ip[:16] if vpn_status.vpn_ip else ""
            status_bar.vpn_status = f"[green]●[/] {iface} ({ip_display})"
        else:
            status_bar.vpn_status = f"[red]● DOWN[/] {vpn_status.error}"
        status_bar.refresh()

    @work(exclusive=True, group="clamav")
    async def check_clamav_status(self) -> None:
        """Check ClamAV availability and update status bar."""
        status_bar = self.query_one("#status-bar", StatusBar)
        clam = await self.security.check_clamav_available()
        if clam["installed"]:
            if clam["daemon_running"]:
                status_bar.clamav_status = "[green]●[/]"
            else:
                status_bar.clamav_status = "[yellow]● no daemon[/]"
        else:
            status_bar.clamav_status = "[red]● missing[/]"
        status_bar.refresh()

    @work(exclusive=True, group="downloads_loop")
    async def refresh_downloads_loop(self) -> None:
        """Periodically refresh download status."""
        while True:
            await self._update_downloads()
            await asyncio.sleep(2)

    async def _update_downloads(self) -> None:
        """Fetch and display current download statuses."""
        try:
            active = await self.aria2.get_active()
            waiting = await self.aria2.get_waiting()
            stopped = await self.aria2.get_stopped(count=5)

            # Detect stalled downloads (no progress for 60s = 30 checks at 2s)
            for dl in active:
                if dl.total_bytes == 0 and dl.download_speed == 0:
                    self._stall_tracker[dl.gid] = self._stall_tracker.get(dl.gid, 0) + 1
                    if self._stall_tracker[dl.gid] == 30:  # ~60 seconds
                        self.notify(
                            f"Stalled: {dl.name or dl.gid[:12]} — no metadata after 60s",
                            severity="warning",
                        )
                    elif self._stall_tracker[dl.gid] >= 90:  # ~3 minutes
                        try:
                            await self.aria2._call("forceRemove", [dl.gid])
                            self.notify(
                                f"Removed dead download: {dl.name or dl.gid[:12]}",
                                severity="warning",
                            )
                        except Exception:
                            pass
                        self._stall_tracker.pop(dl.gid, None)
                else:
                    self._stall_tracker.pop(dl.gid, None)

            # Scan newly completed downloads (skip tiny metadata downloads)
            if self.config.get("security", "scan_on_complete"):
                for dl in stopped:
                    if dl.status == "complete" and dl.gid not in self._scanned_gids:
                        self._scanned_gids.add(dl.gid)
                        if dl.dir and dl.total_bytes > 1_000_000:
                            self._scan_completed_download(dl)

            table = self.query_one("#downloads-table", DataTable)
            table.clear()

            status_bar = self.query_one("#status-bar", StatusBar)
            total_speed = sum(d.download_speed for d in active)
            status_bar.active_downloads = len(active) + len(waiting)
            speed = total_speed
            for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
                if speed < 1024:
                    status_bar.download_speed = f"{speed:.1f} {unit}"
                    break
                speed /= 1024
            status_bar.refresh()

            for dl in active + waiting:
                progress_bar = self._make_progress(dl.progress)
                status_color = {
                    "active": "green",
                    "waiting": "yellow",
                    "paused": "dim",
                    "error": "red",
                }.get(dl.status, "white")

                table.add_row(
                    dl.name[:50] or dl.gid,
                    dl.size_human,
                    progress_bar,
                    dl.speed_human if dl.status == "active" else "—",
                    dl.eta if dl.status == "active" else "—",
                    Text(dl.status, style=status_color),
                    key=dl.gid,
                )
        except Exception:
            pass  # aria2 might not be running yet

    @work(group="security_scan")
    async def _scan_completed_download(self, dl: DownloadStatus) -> None:
        """Scan a completed download for threats."""
        from pathlib import Path

        # Find the actual downloaded file/folder inside the download directory
        download_path = None
        dl_dir = Path(dl.dir)

        if dl.name and dl.name != dl.gid:
            # aria2 resolved the torrent name
            candidate = dl_dir / dl.name
            if candidate.exists():
                download_path = candidate

        if download_path is None:
            # Look for the most recently created item in the download dir
            if dl_dir.exists():
                children = [c for c in dl_dir.iterdir() if c.name != ".DS_Store"]
                if children:
                    download_path = max(children, key=lambda p: p.stat().st_mtime)

        if download_path is None or not download_path.exists():
            self.notify(f"Scan skipped: can't locate download for {dl.gid[:12]}", severity="warning")
            return

        self.notify(f"Scanning: {download_path.name}...")

        result = await self.security.full_scan(
            download_path, expected_bytes=dl.total_bytes
        )

        if result.clean:
            self.notify(
                f"✓ Clean: {download_path.name} ({result.scanned_files} files)",
                severity="information",
            )
        elif result.threats or result.blocked_files:
            self.notify(
                f"⚠ THREAT: {download_path.name} — {result.summary}",
                severity="error",
            )
            # Auto-quarantine only for real threats, not scan errors
            self.security.quarantine(download_path, result)
            self.notify(
                f"Quarantined: {download_path.name}",
                severity="warning",
            )
        else:
            # Scan error (e.g. ClamAV issue) but no actual threats — don't quarantine
            self.notify(
                f"Scan warning: {download_path.name} — {result.error}",
                severity="warning",
            )

        # Re-route audiobooks based on file content. Mirrors the CLI
        # watch-mode behavior in main.py.
        if result.clean and is_audiobook_dir(download_path):
            try:
                ab_dest = resolve_destination(self.config, "audiobooks")
            except DestinationUnavailable as e:
                self.notify(f"Audiobook not routed: {e}", severity="warning")
                return
            ab_root = Path(ab_dest.path)
            if ab_root not in download_path.parents:
                ab_root.mkdir(parents=True, exist_ok=True)
                new_path = ab_root / download_path.name
                if new_path.exists():
                    self.notify(
                        f"Audiobook target already exists, leaving in place: {new_path}",
                        severity="warning",
                    )
                else:
                    shutil.move(str(download_path), str(new_path))
                    self.notify(
                        f"🎧 Routed audiobook → {new_path}",
                        severity="information",
                    )

    def _make_progress(self, percent: float) -> Text:
        """Create a text-based progress bar."""
        width = 20
        filled = int(width * percent / 100)
        bar = "█" * filled + "░" * (width - filled)
        return Text(f"{bar} {percent:.1f}%")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle search submission."""
        query = event.value.strip()
        if not query:
            return
        self.run_search(query)

    @work(exclusive=True, group="search")
    async def run_search(self, query: str) -> None:
        """Execute search and populate results table."""
        # Check VPN before searching
        if self.config.get("vpn", "enabled"):
            vpn_status = await self.vpn.check()
            if not vpn_status.connected:
                self.notify("VPN not connected — search blocked", severity="error")
                return

        self.notify(f"Searching: {query}...")
        info = self.query_one("#info-bar", Static)
        info.update("Searching...")

        quality_exclude = self.config.get("quality_exclude", default=[])
        max_results = self.config.get("display", "max_results", default=50)

        self.search_results = await self.jackett.search(
            query, quality_exclude=quality_exclude, max_results=max_results
        )
        self.selected_indices.clear()

        table = self.query_one("#results-table", DataTable)
        table.clear()

        for i, result in enumerate(self.search_results):
            table.add_row(
                " ",
                str(i + 1),
                result.title[:80],
                result.size_human,
                str(result.seeders),
                str(result.leechers),
                result.indexer[:15],
            )

        status = f"Found {len(self.search_results)} results for '{query}'"
        if self.jackett.last_errors:
            status += f" — {len(self.jackett.last_errors)} indexer(s) skipped"
            self.notify(
                "Indexers skipped: "
                + ", ".join(f"{n} ({r})" for n, r in self.jackett.last_errors),
                severity="warning",
            )
        info.update(status)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Toggle selection on a result row."""
        table = self.query_one("#results-table", DataTable)

        if event.data_table.id != "results-table":
            return

        row_index = event.cursor_row
        if row_index >= len(self.search_results):
            return

        if row_index in self.selected_indices:
            self.selected_indices.discard(row_index)
            marker = " "
        else:
            self.selected_indices.add(row_index)
            marker = "✓"

        # Update the checkbox column
        row_key = table.get_row_at(row_index)
        table.update_cell_at((row_index, 0), marker)

        info = self.query_one("#info-bar", Static)
        info.update(f"{len(self.selected_indices)} selected")

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_inspect_result(self) -> None:
        """Open detail modal for the highlighted search result."""
        table = self.query_one("#results-table", DataTable)
        if not self.search_results or table.row_count == 0:
            self.notify("No results to inspect", severity="warning")
            return

        row_index = table.cursor_row
        if row_index < 0 or row_index >= len(self.search_results):
            self.notify("No result selected", severity="warning")
            return

        result = self.search_results[row_index]

        def _on_dismiss(action: str | None) -> None:
            if action == "download":
                self._download_single_result(result)
            elif action == "open":
                self._open_info_url(result)

        self.push_screen(InspectScreen(result), _on_dismiss)

    @work(exclusive=True, group="download_action")
    async def _download_single_result(self, result: TorrentResult) -> None:
        """Download a single result from the inspect modal."""
        if self.config.get("vpn", "enabled"):
            try:
                vpn_status = await self.vpn.check()
                if not vpn_status.connected:
                    self.notify("VPN not connected", severity="error")
                    return
            except Exception:
                pass

        url = result.download_url
        if not url:
            self.notify("No download URL", severity="error")
            return

        try:
            dest = resolve_destination(
                self.config, result.category, self.aria2.download_dir
            )
        except DestinationUnavailable as e:
            self.notify(f"Failed: {e}", severity="error")
            return

        try:
            if url.startswith("magnet:"):
                await self.aria2.add_magnet(url, download_dir=dest.path)
            else:
                await self.aria2.add_torrent_url(url, download_dir=dest.path)
        except Exception as e:
            self.notify(f"Failed: {e}", severity="error")
            return

        if dest.redirected:
            self.notify(
                f"Added: {result.title[:40]} — {dest.notice}", severity="warning"
            )
        else:
            self.notify(f"Added: {result.title[:50]}")

    def _open_info_url(self, result: TorrentResult) -> None:
        """Open the torrent info URL in the default browser."""
        url = result.info_url
        if not url or url.startswith("magnet:"):
            self.notify("No info URL available", severity="warning")
            return
        try:
            if platform.system() == "Darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.notify(f"Opened: {url[:50]}")
        except Exception as e:
            self.notify(f"Failed to open URL: {e}", severity="error")

    def action_select_all(self) -> None:
        """Toggle select all results."""
        table = self.query_one("#results-table", DataTable)
        if len(self.selected_indices) == len(self.search_results):
            # Deselect all
            self.selected_indices.clear()
            for i in range(len(self.search_results)):
                table.update_cell_at((i, 0), " ")
        else:
            # Select all
            self.selected_indices = set(range(len(self.search_results)))
            for i in range(len(self.search_results)):
                table.update_cell_at((i, 0), "✓")

    @work(exclusive=True, group="download_action")
    async def action_download_selected(self) -> None:
        """Download all selected torrents."""
        # If nothing explicitly selected, use the current cursor row
        if not self.selected_indices:
            table = self.query_one("#results-table", DataTable)
            if table.row_count == 0 or not self.search_results:
                self.notify("No results to download", severity="warning")
                return
            cursor_row = table.cursor_row
            if 0 <= cursor_row < len(self.search_results):
                self.selected_indices.add(cursor_row)
                table.update_cell_at((cursor_row, 0), "✓")
            else:
                self.notify("Nothing selected", severity="warning")
                return

        # VPN gate
        try:
            if self.config.get("vpn", "enabled"):
                vpn_status = await self.vpn.check()
                if not vpn_status.connected:
                    self.notify("VPN not connected — aborting download", severity="error")
                    return
        except Exception as e:
            self.notify(f"VPN check failed: {e}", severity="error")
            return

        added = 0
        notices: set[str] = set()
        redirected_cats: set[str] = set()

        for idx in sorted(self.selected_indices):
            result = self.search_results[idx]
            url = result.download_url
            if not url:
                self.notify(f"No URL for: {result.title[:40]}", severity="warning")
                continue

            # Determine download directory from category, falling back to a
            # local path when the category's drive isn't connected.
            try:
                dest = resolve_destination(
                    self.config, result.category, self.aria2.download_dir
                )
            except DestinationUnavailable as e:
                self.notify(f"Skipped {result.title[:40]}: {e}", severity="error")
                continue

            try:
                if url.startswith("magnet:"):
                    await self.aria2.add_magnet(url, download_dir=dest.path)
                else:
                    await self.aria2.add_torrent_url(url, download_dir=dest.path)
                added += 1
                if dest.redirected:
                    notices.add(dest.notice)
                    redirected_cats.add(result.category)
            except Exception as e:
                self.notify(f"Failed: {result.title[:40]}: {e}", severity="error")

        self.notify(f"Added {added} download(s)")
        for notice in sorted(notices):
            self.notify(notice, severity="warning")

        # Trigger Plex scan for relevant categories. Redirected downloads
        # never reach the library, so scanning for them would find nothing.
        if self.config.get("plex", "enabled"):
            scanned_cats = set(redirected_cats)
            for idx in sorted(self.selected_indices):
                cat = self.search_results[idx].category
                if cat not in scanned_cats:
                    await self.plex.scan_for_category(cat)
                    scanned_cats.add(cat)

        self.selected_indices.clear()

    async def action_pause_all(self) -> None:
        """Pause all active downloads."""
        try:
            await self.aria2.pause_all()
            self.notify("All downloads paused")
        except Exception as e:
            self.notify(f"Pause failed: {e}", severity="error")

    async def action_clear_finished(self) -> None:
        """Clear all dead, finished, and zombie downloads."""
        try:
            removed = 0
            # Force-remove any active downloads that are done or stuck at 0 bytes
            active = await self.aria2.get_active()
            for dl in active:
                should_remove = (
                    (dl.total_bytes > 0 and dl.completed_bytes >= dl.total_bytes)  # done seeding
                    or (dl.total_bytes == 0 and dl.download_speed == 0)  # zombie metadata
                )
                if should_remove:
                    try:
                        await self.aria2._call("forceRemove", [dl.gid])
                        removed += 1
                    except Exception:
                        pass
            # Force-remove any waiting/errored/paused
            waiting = await self.aria2.get_waiting(count=50)
            for dl in waiting:
                if dl.status in ("error", "paused") or (dl.total_bytes == 0 and dl.download_speed == 0):
                    try:
                        await self.aria2._call("forceRemove", [dl.gid])
                        removed += 1
                    except Exception:
                        pass
            # Purge all stopped results
            await self.aria2.purge_completed()
            self.notify(f"Cleared {removed} download(s)")
        except Exception as e:
            self.notify(f"Clear failed: {e}", severity="error")

    async def action_remove_download(self) -> None:
        """Remove the selected download, its torrent data, and partial files."""
        from pathlib import Path
        import shutil

        # Find the GID to remove
        gid = None

        # Try from downloads table cursor
        table = self.query_one("#downloads-table", DataTable)
        if table.row_count > 0:
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                gid = row_key.value
            except Exception:
                pass

        # Fall back to first active/waiting download
        if not gid:
            try:
                active = await self.aria2.get_active()
                waiting = await self.aria2.get_waiting(count=10)
                all_dl = active + waiting
                if all_dl:
                    gid = all_dl[0].gid
            except Exception:
                pass

        if not gid:
            self.notify("No downloads to remove", severity="warning")
            return

        try:
            # Get download info before removing
            dl_dir = ""
            dl_name = ""
            try:
                full = await self.aria2._call("tellStatus", [gid])
                dl_dir = full.get("dir", "")
                bt = full.get("bittorrent", {})
                dl_name = bt.get("info", {}).get("name", "")
            except Exception:
                pass

            # Force remove from aria2
            try:
                await self.aria2._call("forceRemove", [gid])
            except Exception:
                pass
            try:
                await self.aria2.remove_result(gid)
            except Exception:
                pass

            # Delete partial files from disk
            deleted = False
            if dl_dir and dl_name:
                dl_path = Path(dl_dir) / dl_name
                if dl_path.exists():
                    if dl_path.is_dir():
                        shutil.rmtree(dl_path, ignore_errors=True)
                    else:
                        dl_path.unlink(missing_ok=True)
                    deleted = True
                aria2_file = Path(f"{dl_path}.aria2")
                aria2_file.unlink(missing_ok=True)

            msg = f"Removed: {dl_name or gid[:12]}"
            if deleted:
                msg += " (files deleted)"
            self.notify(msg)
        except Exception as e:
            self.notify(f"Remove failed: {e}", severity="error")

    async def action_inspect_download(self) -> None:
        """Show health info for all active downloads."""
        try:
            active = await self.aria2.get_active()
            waiting = await self.aria2.get_waiting(count=10)
            all_dl = active + waiting

            if not all_dl:
                self.notify("No downloads to inspect", severity="warning")
                return

            info = self.query_one("#info-bar", Static)
            lines = []

            for dl in all_dl:
                full = await self.aria2._call("tellStatus", [dl.gid])
                bt = full.get("bittorrent", {})
                name = bt.get("info", {}).get("name", dl.gid[:12])
                total = int(full.get("totalLength", 0))
                speed = int(full.get("downloadSpeed", 0))
                conns = int(full.get("connections", 0))

                try:
                    peers = await self.aria2._call("getPeers", [dl.gid])
                except Exception:
                    peers = []

                seeders = sum(1 for p in peers if p.get("seeder") == "true")
                choking = sum(1 for p in peers if p.get("peerChoking") == "true")
                unchoked = len(peers) - choking

                if total == 0 and speed == 0:
                    health = "DEAD - no metadata"
                elif speed == 0 and unchoked == 0:
                    health = "ZOMBIE - all peers choking"
                elif speed == 0:
                    health = "STALLED"
                elif speed < 10240:
                    health = "SLOW"
                else:
                    health = "OK"

                lines.append(
                    f"{name[:30]} | {health} | "
                    f"{len(peers)}p {seeders}s {unchoked}unchoked {conns}conn "
                    f"{speed/1024:.0f}KB/s"
                )

            info.update(" | ".join(lines) if len(lines) == 1 else lines[0])
            # Show all via notifications for multi-download
            for line in lines:
                severity = "information"
                if "DEAD" in line or "ZOMBIE" in line:
                    severity = "error"
                elif "STALLED" in line:
                    severity = "warning"
                self.notify(line, severity=severity)

        except Exception as e:
            self.notify(f"Inspect failed: {e}", severity="error")

    def action_refresh_downloads(self) -> None:
        """Manual refresh of download status."""
        self.check_vpn_status()


def run_tui(config: Config):
    """Launch the TUI app."""
    app = TGetApp(config)
    app.run()
