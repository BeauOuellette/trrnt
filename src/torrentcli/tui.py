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

from textual.theme import Theme

from .config import Config
from .download import Aria2Client, DownloadStatus, predict_category
from .plex import PlexClient
from .search import JackettSearch, TorrentResult
from .security import SecurityScanner, detect_content_category
from .storage import DestinationUnavailable, resolve_destination
from .vpn import VPNGuard

# ── Palette ───────────────────────────────────────────────────────────────────
# Every value is an exact xterm-256 slot. macOS Terminal.app has no truecolor,
# so Textual quantises anything else on the way out — and the 6×6×6 colour cube
# only has the levels 00/5f/87/af/d7/ff, which leaves a dark *tinted* ground
# nowhere to land but pure black, taking the panel separation with it. Neutral
# greys from the 232–255 ramp survive exactly; the character goes in the accent.
VIOLET = Theme(
    name="violet",
    background="#1c1c1c",   # 234
    surface="#262626",      # 235
    panel="#3a3a3a",        # 237
    # Declared one slot high on purpose: rich's 256-colour downgrade shifts
    # every value in the 232–255 grey ramp down by one, so this renders as
    # #d0d0d0 (252) — 11:1 on the ground. Pure white is the only light grey
    # that round-trips exactly, and 17:1 is punishing for a long session.
    foreground="#d7d7d7",
    primary="#af87d7",      # 140 — headers
    secondary="#5f5f87",    # 60  — selection
    accent="#af87d7",       # 140
    success="#5fd7af",      # 79
    warning="#d7af5f",      # 179
    error="#ff5f87",        # 204
    dark=True,
)

ACCENT = "#af87d7"      # 140 — same as the theme's primary
SEED_GOOD = "#5fd7af"   # a seeder is connected
SEED_WARN = "#d7af5f"   # peers, but all partial
SEED_NONE = "#ff5f87"   # nothing on the line
DIM = "#808080"         # 244

# Shown when a finished download is re-filed by its contents.
_CATEGORY_ICONS = {"audiobooks": "🎧", "comics": "💥", "ebooks": "📚"}

# Stall detection, counted in refresh ticks of ~2s each.
_STALL_WARN_TICKS = 30    # ~60s — say something
_STALL_REMOVE_TICKS = 90  # ~3min — give up, but only on a magnet with no peers


# Everything in the results row that isn't the name: marker, index, size,
# seeders, leechers, source, plus DataTable's cell padding. Name gets the rest.
_RESULT_CHROME = 54
# Same for the downloads row, in its narrow form (no Status column).
_DOWNLOAD_CHROME = 72
# Below this the Status column is dropped; the progress bar and Seeds colour
# already carry that information.
_WIDE = 140
# Indexer names are long ("The Pirate Bay") and low-value once you know which
# indexers you run. Capping it is what buys the Name column its width back —
# and stops the row spilling past the right edge into a scrollbar.
SOURCE_MAX = 12


def fit_name(name: str, budget: int) -> str:
    """Truncate to budget, keeping the end of the string.

    Releases differ at the tail — "2160p PMTP WEB-DL DDP5 1 DV" against
    "2160p AMZN WEB-DL DDP5 1 H" — so cutting the end throws away the only
    part that tells two rows apart. Cut the middle instead.
    """
    if budget <= 1 or len(name) <= budget:
        return name[:budget] if budget > 0 else ""
    head = int((budget - 1) * 0.42)
    return name[:head] + "…" + name[len(name) - (budget - 1 - head):]


def fit_source(indexer: str) -> str:
    """Cap an indexer name, cut from the right.

    Unlike release titles these differ at the front, so the head is the part
    worth keeping — and an ellipsis marks it as deliberately shortened rather
    than looking like a rendering fault.
    """
    if len(indexer) <= SOURCE_MAX:
        return indexer
    # rstrip first, or a name that happens to break on a space renders as
    # "The Pirate …" with a gap in front of the ellipsis.
    return indexer[:SOURCE_MAX - 1].rstrip() + "…"


def _peer_cell(dl: DownloadStatus) -> Text:
    """Seeders connected, over total peers connected — "9/12".

    aria2's numSeeders counts seeders it actually has a connection to, not
    what the tracker advertised, so this is the live answer to "is anything
    on the other end of this?" — the same signal the stall detector uses
    before giving up on a magnet.
    """
    if dl.status not in ("active", "waiting"):
        return Text("—", style="dim")

    seeders, peers = dl.seeders, dl.connections
    if seeders > 0:
        style = SEED_GOOD        # someone has the whole file
    elif peers > 0:
        style = SEED_WARN        # peers, but all of them partial
    else:
        style = SEED_NONE        # nothing on the line
    return Text(f"{seeders}/{peers}", style=style)


def is_dead_magnet(dl: DownloadStatus, ticks: int) -> bool:
    """True when a magnet has waited long enough with nobody answering.

    Elapsed time alone is a poor signal. A magnet connected to peers is still
    working through the metadata handshake however long it has taken — DHT
    lookups on a sparse torrent are slow, more so when aria2 is bound to a
    tunnel with no incoming connections. Only silence means dead.

    total_bytes is 0 until metadata resolves, so a download that is merely
    slow, or stalled part-way through, never reaches this at all.
    """
    return (
        ticks >= _STALL_REMOVE_TICKS
        and dl.total_bytes == 0
        and dl.download_speed == 0
        and dl.connections == 0
    )


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


# Ctrl+letter combinations a terminal physically cannot deliver: these bytes
# ARE the control codes for other keys, so the app is handed Tab or Backspace
# and the binding never fires — no error, no clue, the key just does nothing.
# Textual explicitly clears IXON/IXOFF, so ctrl+s and ctrl+q are fine despite
# being XON/XOFF; termios cannot rescue the ones below, because there is
# nothing to distinguish.
UNDELIVERABLE_KEYS = {
    "ctrl+h": "backspace",
    "ctrl+i": "tab",
    "ctrl+j": "enter (line feed)",
    "ctrl+m": "enter (carriage return)",
    "ctrl+[": "escape",
}

# Footer contents, in the order they appear. Actions rather than keys, so
# rebinding a key moves the label with it.
_FOOTER_LEFT = [
    "download_selected",
    "force_reconnect",
    "clear_finished",
    "remove_download",
    "quit",
]
_FOOTER_RIGHT = "show_keys"


class KeyBar(Static):
    """The footer, rendered directly rather than by Textual's Footer.

    Textual's Footer orders keys by how it collects bindings — not by the
    order they are declared — and reserves the right-hand slot for the command
    palette, which is why ctrl+p showed up there labelled "Pause All". Neither
    is configurable, and both matter here: the order is the muscle memory, and
    the right-hand slot is where Keys belongs.
    """

    def _entries(self, actions) -> list[tuple[str, str]]:
        by_action = {b.action: b for b in self.app.BINDINGS}
        out = []
        for action in actions:
            binding = by_action.get(action)
            if binding:
                out.append((_key_label(binding.key), binding.description))
        return out

    def render(self) -> Text:
        left = Text()
        for i, (key, label) in enumerate(self._entries(_FOOTER_LEFT)):
            if i:
                left.append("   ")
            left.append(key, style=f"bold {ACCENT}")
            left.append(f" {label}")

        right = Text()
        for key, label in self._entries([_FOOTER_RIGHT]):
            right.append(key, style=f"bold {ACCENT}")
            right.append(f" {label}")

        bar = Text(" ")
        bar.append_text(left)
        # Push Keys to the right edge, but never off it: on a window too narrow
        # to hold both, the left keys win and Keys drops rather than wrapping.
        gap = self.size.width - len(left.plain) - len(right.plain) - 2
        if gap >= 1:
            bar.append(" " * gap)
            bar.append_text(right)
        return bar


class KeysScreen(ModalScreen[None]):
    """Every binding, including the ones the footer has no room for.

    Built from the app's own BINDINGS so it cannot drift out of sync with
    what the keys actually do.
    """

    CSS = """
    KeysScreen { align: center middle; }
    #keys-dialog {
        width: 52; max-width: 90%; height: auto; max-height: 80%;
        background: $surface; border: thick $primary; padding: 1 2;
    }
    #keys-title { text-style: bold; color: $primary; margin-bottom: 1; }
    .key-row { height: 1; }
    #keys-hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+k", "close", "Close", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="keys-dialog"):
            yield Label("Keys", id="keys-title")
            for binding in self.app.BINDINGS:
                if binding.action == "show_keys":
                    continue
                shown = binding.key_display or _key_label(binding.key)
                yield Static(
                    f"[bold $primary]{shown:>6}[/]  {binding.description}",
                    classes="key-row",
                )
            yield Static("esc or ? to close", id="keys-hint")

    def action_close(self) -> None:
        self.dismiss(None)


def _key_label(key: str) -> str:
    """Render a Textual key name the way the footer does — ctrl+d → ^d."""
    return f"^{key[5:]}" if key.startswith("ctrl+") else key


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

    # Frees ctrl+p for Pause All, and removes the right-hand footer slot
    # Textual reserves for the palette — Keys goes there instead.
    ENABLE_COMMAND_PALETTE = False

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
        overflow-x: hidden;
    }
    #downloads-table {
        height: auto;
        max-height: 10;
        margin: 0 1 1 1;
        overflow-x: hidden;
    }
    Toast {
        /* Textual's default is 60 wide, which at 102 columns blankets the
           whole Downloads row while the message is up. Narrower leaves the
           name, size and progress readable underneath. */
        width: 44;
        max-width: 44;
    }
    #key-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text;
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

    # The full set runs to 121 columns, which does not fit a 102-column window.
    # Rather than abbreviate every label, only the five most-used are shown;
    # the rest keep working and are listed by `?`. All still bind.
    BINDINGS = [
        Binding("ctrl+d", "download_selected", "Download", priority=True),
        Binding("ctrl+f", "force_reconnect", "Reconnect", priority=True),
        Binding("ctrl+x", "clear_finished", "Clear Done", priority=True),
        # Remove moves off ctrl+w — ctrl+r reads as "remove" far more readily
        # than as "refresh", and the download list already re-renders every 2s
        # on its own, so the manual refresh it displaces is near-redundant.
        Binding("ctrl+r", "remove_download", "Remove", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        # Not ctrl+? — that is DEL (0x7f) at the terminal, not a bindable key.
        # Not plain ? either: as a non-priority binding it never reaches
        # active_bindings while a table holds focus, so it silently never
        # fired. ctrl+k is priority like the rest and cannot collide with
        # typing in the search box.
        Binding("ctrl+k", "show_keys", "Keys", priority=True),

        Binding("ctrl+s", "focus_search", "Search", priority=True, show=False),
        Binding("ctrl+a", "select_all", "Select All", priority=True, show=False),
        Binding("ctrl+p", "pause_all", "Pause All", priority=True, show=False),
        # Was ctrl+i and ctrl+h. Both were undeliverable — the terminal sends
        # Tab and Backspace for those bytes — so neither key had ever worked.
        Binding("ctrl+e", "inspect_result", "Inspect", priority=True, show=False),
        Binding("ctrl+g", "inspect_download", "Health", priority=True, show=False),
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
        self._routed_gids: set[str] = set()  # Destination corrected in flight
        self._downloads_wide: bool | None = None  # Status column shown?
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
        yield KeyBar(id="key-bar")

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
        # Columns are set by _sync_download_columns, which drops Status on a
        # narrow window. Nothing to add here.

        self.register_theme(VIOLET)
        self.theme = "violet"

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

            # Detect magnets stuck without metadata (30 checks at 2s ≈ 60s).
            # total_bytes stays 0 until metadata resolves, so this never
            # touches a download that is merely slow.
            for dl in active:
                if dl.total_bytes == 0 and dl.download_speed == 0:
                    self._stall_tracker[dl.gid] = self._stall_tracker.get(dl.gid, 0) + 1
                    if self._stall_tracker[dl.gid] == _STALL_WARN_TICKS:
                        self.notify(
                            f"Stalled: {dl.name or dl.gid[:12]} — no metadata after 60s",
                            severity="warning",
                        )
                    elif is_dead_magnet(dl, self._stall_tracker[dl.gid]):
                        try:
                            await self.aria2._call("forceRemove", [dl.gid])
                            self.notify(
                                f"Removed dead download: {dl.name or dl.gid[:12]} "
                                "— no metadata and no peers",
                                severity="warning",
                            )
                        except Exception:
                            pass
                        self._stall_tracker.pop(dl.gid, None)
                else:
                    self._stall_tracker.pop(dl.gid, None)

            # Flag where each download will actually be filed, as soon as
            # aria2 resolves its file list. Torrents can't be redirected once
            # created, so this only reports — the move happens on completion.
            for dl in active:
                if dl.gid in self._routed_gids or not dl.total_bytes:
                    continue
                self._routed_gids.add(dl.gid)
                await self._announce_category(dl)

            # Scan newly completed downloads (skip tiny metadata downloads).
            # A torrent that has finished downloading but is still seeding
            # stays in aria2's *active* list, so watching `stopped` alone
            # would leave it unfiled until seed-ratio is met — which on a
            # low-demand torrent may be never.
            if self.config.get("security", "scan_on_complete"):
                finished = [d for d in stopped if d.status == "complete"]
                finished += [
                    d for d in active
                    if d.total_bytes > 0 and d.completed_bytes >= d.total_bytes
                ]
                for dl in finished:
                    if dl.gid in self._scanned_gids:
                        continue
                    self._scanned_gids.add(dl.gid)
                    if dl.dir and dl.total_bytes > 1_000_000:
                        self._scan_completed_download(dl)

            table = self._sync_download_columns()
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

                budget = max(8, min(60, self.size.width - _DOWNLOAD_CHROME))
                cells = [
                    fit_name(dl.name or dl.gid, budget),
                    dl.size_human,
                    progress_bar,
                    _peer_cell(dl),
                    dl.speed_human if dl.status == "active" else "—",
                    dl.eta if dl.status == "active" else "—",
                ]
                if self._downloads_wide:
                    cells.append(Text(dl.status, style=status_color))
                table.add_row(*cells, key=dl.gid)
        except Exception:
            pass  # aria2 might not be running yet

    async def _announce_category(self, dl: DownloadStatus) -> None:
        """Say where a running download will end up, once its files are known.

        A torrent can't be redirected mid-flight — aria2 fixes its paths when
        the download is created — so this is information, not a move. The
        filing happens when the download finishes.
        """
        try:
            category = await predict_category(self.aria2, self.config, dl)
        except Exception:
            return
        if category:
            icon = _CATEGORY_ICONS.get(category, "📁")
            self.notify(f"{icon} {dl.name[:30]} is {category} — filing there when done")

    async def _release_from_aria2(self, dl: DownloadStatus) -> None:
        """Stop seeding a finished download so its files can be moved.

        Seeding keeps a completed torrent in aria2's active list indefinitely
        on a low-demand swarm. Waiting for seed-ratio before filing would mean
        never filing it, so the trade is made explicitly here: the file gets
        put where it belongs, and this torrent stops seeding.
        """
        try:
            await self.aria2.force_remove(dl.gid)
        except Exception:
            pass  # already stopped — nothing to release
        try:
            await self.aria2.remove_result(dl.gid)
        except Exception:
            pass

    @work(group="security_scan")
    async def _scan_completed_download(self, dl: DownloadStatus) -> None:
        """Scan a completed download for threats, then file it by content."""
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
            # Auto-quarantine only for real threats, not scan errors.
            # Quarantining moves the files, so stop seeding them first.
            await self._release_from_aria2(dl)
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

        # Safety net for anything the in-flight re-route couldn't settle —
        # a category only the finished files reveal. Normally this finds the
        # download already in the right place and does nothing, which is what
        # keeps seeding alive: the move below is what forces us to stop it.
        if not result.clean:
            return
        content_cat = detect_content_category(download_path)
        if content_cat is None:
            return

        try:
            dest = resolve_destination(self.config, content_cat)
        except DestinationUnavailable as e:
            self.notify(f"{content_cat} not routed: {e}", severity="warning")
            return

        root = Path(dest.path)
        if root in download_path.parents:
            return  # already filed correctly — leave it seeding

        root.mkdir(parents=True, exist_ok=True)
        new_path = root / download_path.name
        if new_path.exists():
            self.notify(
                f"{content_cat} target already exists, leaving in place: {new_path}",
                severity="warning",
            )
            return

        # Only now, with a move actually required, does seeding have to end.
        await self._release_from_aria2(dl)
        shutil.move(str(download_path), str(new_path))
        Path(f"{download_path}.aria2").unlink(missing_ok=True)
        self.notify(
            f"{_CATEGORY_ICONS.get(content_cat, '📁')} Routed {content_cat} → {new_path}",
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

        self._render_results()

        status = f"Found {len(self.search_results)} results for '{query}'"
        if self.jackett.last_errors:
            status += f" — {len(self.jackett.last_errors)} indexer(s) skipped"
            self.notify(
                "Indexers skipped: "
                + ", ".join(f"{n} ({r})" for n, r in self.jackett.last_errors),
                severity="warning",
            )
        info.update(status)

    def _sync_download_columns(self) -> DataTable:
        """Add or drop the Status column as the window crosses _WIDE.

        Status is the first thing to go on a narrow window: the progress bar
        and the Seeds colour already say whether a download is moving.
        """
        table = self.query_one("#downloads-table", DataTable)
        wide = self.size.width >= _WIDE
        if wide == self._downloads_wide and table.columns:
            return table
        self._downloads_wide = wide
        table.clear(columns=True)
        columns = ["Name", "Size", "Progress", "Seeds", "Speed", "ETA"]
        if wide:
            columns.append("Status")
        table.add_columns(*columns)
        return table

    def name_budget(self) -> int:
        """Characters the results Name column may take at this width.

        Everything else in the row has a fixed cost, so Name gets what's left
        rather than a hard-coded cap that overflowed narrow windows and
        wasted wide ones.
        """
        return max(12, min(100, self.size.width - _RESULT_CHROME))

    def _render_results(self) -> None:
        """Draw the results table at the current terminal width."""
        table = self.query_one("#results-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        budget = self.name_budget()
        for i, result in enumerate(self.search_results):
            table.add_row(
                "✓" if i in self.selected_indices else " ",
                str(i + 1),
                fit_name(result.title, budget),
                result.size_human,
                str(result.seeders),
                str(result.leechers),
                fit_source(result.indexer),
            )
        if 0 <= cursor < table.row_count:
            table.move_cursor(row=cursor)

    async def on_resize(self, event) -> None:
        """Re-fit both tables when the window changes size."""
        if self.search_results:
            self._render_results()
        await self._update_downloads()

    def action_show_keys(self) -> None:
        """Show every binding, including those the footer can't fit."""
        self.push_screen(KeysScreen())

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
                    f"{name[:28]} · {health} · {len(peers)} peers · "
                    f"{seeders} seeders · {unchoked} unchoked · "
                    f"{conns} conns · {speed/1024:.0f} KB/s"
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

    @work(exclusive=True, group="reconnect")
    async def action_force_reconnect(self) -> None:
        """Drop every peer connection and pick up a fresh set.

        aria2 exposes no way to force a tracker announce — 36 RPC methods and
        none of them announce-related — so pause-and-resume is the only lever
        there is. On resume it re-announces and rebuilds the peer set instead
        of grinding on whatever stale connections it was left holding.

        This replaced a "Refresh" binding that only re-checked the VPN, and
        which would have been pointless anyway: the table already redraws
        every two seconds.
        """
        try:
            active = await self.aria2.get_active()
        except Exception as e:
            self.notify(f"Reconnect failed: {e}", severity="error")
            return

        if not active:
            self.notify("Nothing downloading to reconnect", severity="warning")
            return

        gids = [d.gid for d in active]
        try:
            for gid in gids:
                await self.aria2.pause(gid)
            # Give aria2 a moment to actually close the sockets; resuming
            # instantly can hand back the same peers we were trying to shed.
            await asyncio.sleep(0.75)
            for gid in gids:
                await self.aria2.unpause(gid)
        except Exception as e:
            self.notify(f"Reconnect failed: {e}", severity="error")
            return

        self.notify(
            f"Reconnected {len(gids)} download(s) — new peers on the next announce"
        )


def run_tui(config: Config):
    """Launch the TUI app."""
    app = TGetApp(config)
    app.run()
