"""Interactive TUI powered by Textual."""

import asyncio
import re
import shutil
import subprocess
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Log,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from textual.theme import Theme

from . import onboard, settings
from .branding import TAGLINE, VERSION, splash_text
from .config import Config
from .download import Aria2Client, DownloadStatus, predict_category
from .naming import parse_release_name, recommend_folder, suggest_name
from .organize import (
    OrganizeRecord,
    OrganizeStore,
    apply_pending_selection,
    cleanup_wrapper,
    configured_junk,
    effective_junk,
    find_orphan_records,
    new_plan_gid,
    organize_download,
)
from .plex import PlexClient
from .search import JackettSearch, TorrentResult
from .security import SecurityScanner, detect_content_category
from .storage import Destination, DestinationUnavailable, resolve_destination, shorten
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
RULE = "#5f5f87"        # 60 — column rules, the theme's own selection violet
TRACK = "#3a3a3a"       # 237 — the unfilled half of a progress bar

# Shown when a finished download is re-filed by its contents.
_CATEGORY_ICONS = {"audiobooks": "🎧", "comics": "💥", "ebooks": "📚"}

# Stall detection, counted in refresh ticks of ~2s each.
_STALL_WARN_TICKS = 30    # ~60s — say something
_STALL_REMOVE_TICKS = 90  # ~3min — give up, but only on a magnet with no peers


# Everything in the results row that isn't the name: marker, index, size,
# seeders, leechers, source, plus DataTable's cell padding. Name gets the rest.
_RESULT_CHROME = 54
# Below this the Status column is dropped; the progress bar and Seeds colour
# already carry that information.
_WIDE = 140

# ── Downloads table geometry ─────────────────────────────────────────────────
# Rows are three lines so their text sits centred with a blank line above and
# below. Six rows plus the header is the height the section always occupies —
# fixed, not grown, so the layout never shifts as downloads come and go.
_DOWNLOAD_ROW_LINES = 3
_DOWNLOAD_ROWS_VISIBLE = 6
_DOWNLOAD_TABLE_HEIGHT = 1 + _DOWNLOAD_ROWS_VISIBLE * _DOWNLOAD_ROW_LINES

# Column widths are pinned rather than sized to content: a torrent arriving
# with "23/102" seeders would otherwise widen that column and shift every rule
# on screen. Name takes whatever is left.
# Sized to each formatter's widest real output, not its typical one: Speed and
# Size both roll over at 1023.9 ("1023.9 KB/s"), and a column narrower than its
# content silently eats the padding that separates it from the rule.
_DOWNLOAD_COL_WIDTHS = {"Size": 9, "Seeds": 8, "Speed": 11, "ETA": 9, "Status": 8}
# Cells carry their own single space either side, so cell_padding is 0 on this
# table and each rule costs one column instead of three. That is 10 columns of
# Name width back, which is the difference between the name fitting and not.
_DOWNLOAD_CELL_PAD = " "
# The table's one column of inset either side. Measured, not guessed: a
# vertical scrollbar appears as soon as the queue outgrows six rows, but
# Textual has already taken it out of the widget's own width by then.
_DOWNLOAD_GUTTER = 2
# Rows the user cannot act on, padding the fixed height so the rules run all
# the way down instead of stopping at the last real download.
_GHOST_PREFIX = "__ghost_"

# The progress bar is the one pinned column that can afford to give: below
# roughly 100 columns there is no width for both a full bar and a readable
# name, and the name is what tells two downloads apart.
_PROGRESS_BAR = 20
_PROGRESS_BAR_MIN = 10
_PROGRESS_TEXT = 7      # " 100.0%"
# "The Accountant 2 (2025)" is 23 — a title and its year is the shape the name
# column exists to show, so the bar gives way until that much fits.
_NAME_MIN = 24


def _download_chrome(wide: bool, bar: int = _PROGRESS_BAR) -> int:
    """Width the downloads row spends on everything except the Name."""
    widths = dict(_DOWNLOAD_COL_WIDTHS, Progress=bar + _PROGRESS_TEXT)
    columns = ["Size", "Progress", "Seeds", "Speed", "ETA"]
    if wide:
        columns.append("Status")
    pad = 2 * len(_DOWNLOAD_CELL_PAD)
    rules = len(columns)  # one before each column that follows Name
    return (sum(widths[c] + pad for c in columns) + rules + pad
            + _DOWNLOAD_GUTTER)


def download_layout(width: int, wide: bool) -> tuple[int, int]:
    """(Name budget, progress bar width) for a terminal `width` columns wide.

    The bar shrinks only once a full-width one would squeeze the name below
    what is worth reading, and never past _PROGRESS_BAR_MIN — a two-character
    bar would be decoration, not information.
    """
    for bar in range(_PROGRESS_BAR, _PROGRESS_BAR_MIN - 1, -1):
        budget = width - _download_chrome(wide, bar)
        if budget >= _NAME_MIN or bar == _PROGRESS_BAR_MIN:
            # Floor of 6, not of a comfortable width: on a window too narrow
            # for the row, clamping the name up is what pushes the table into
            # scrolling sideways, which is worse than a stubby name.
            return max(6, min(60, budget)), bar
    return _NAME_MIN, _PROGRESS_BAR_MIN
# Indexer names are long ("The Pirate Bay") and low-value once you know which
# indexers you run. Capping it is what buys the Name column its width back —
# and stops the row spilling past the right edge into a scrollbar.
SOURCE_MAX = 12


def centre_cell(lines: list, height: int = _DOWNLOAD_ROW_LINES,
                pad: str = _DOWNLOAD_CELL_PAD) -> Text:
    """One cell's content, centred vertically in a row `height` lines tall.

    The horizontal padding goes on each line rather than around the finished
    cell: the centring prepends blank lines, so padding the cell as a whole
    would put the space on a blank line and leave the text flush against the
    rule.
    """
    out = Text()
    for _ in range(max(0, (height - len(lines)) // 2)):
        out.append("\n")
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        out.append(pad)
        out.append_text(line if isinstance(line, Text) else Text(str(line)))
        out.append(pad)
    return out


def rule_cell(height: int = _DOWNLOAD_ROW_LINES) -> Text:
    """A column rule, drawn down every line of a row."""
    return Text("\n".join("│" for _ in range(height)), style=RULE)


def fit_name(name: str, budget: int) -> str:
    """Truncate to budget: nearly all of it front, plus a short tail hint.

    The front is what a row is chosen by — title, SxxEyy, resolution — so
    it gets the budget. A generous tail turned out to read as the wrong
    half being cut: at ordinary window widths it traded the episode number
    for audio tags. The tail keeps just enough to tell apart releases that
    differ only in their final group/codec run ("…65-NTb" vs "…65-FLUX");
    for anything deeper, the inspect modal has the full title.
    """
    if budget <= 1 or len(name) <= budget:
        return name[:budget] if budget > 0 else ""
    tail = min(8, (budget - 1) // 4)
    head = budget - 1 - tail
    return name[:head] + "…" + name[len(name) - tail:]


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


@dataclass
class OrganizeChoice:
    """What the organize prompt decided for one download."""

    organize: bool
    name: str = ""
    folder: str = ""


class OrganizeScreen(ModalScreen[OrganizeChoice | None]):
    """Name a download and pick its destination before it starts.

    Both fields are pre-filled from the release title. The folder tracks the
    name as it is typed — an SxxEyy in the name recommends Show/Season N —
    until the user edits the folder by hand, which pins it. Dismissing with
    None skips the torrent entirely; "As-is" downloads without renaming.
    """

    CSS = """
    OrganizeScreen {
        align: center middle;
    }
    #organize-dialog {
        width: 90;
        max-width: 95%;
        height: auto;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #organize-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #organize-orig {
        color: $text-muted;
        margin-bottom: 1;
    }
    .organize-label {
        color: $text-muted;
    }
    #organize-hint {
        color: $text-muted;
        margin-top: 1;
    }
    #organize-buttons {
        height: 3;
        margin-top: 1;
        align: center middle;
    }
    #organize-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Skip"),
    ]

    def __init__(self, result: TorrentResult, category_root: str):
        super().__init__()
        self.result = result
        self.category_root = category_root
        self._parsed = parse_release_name(result.title)
        self._suggested = suggest_name(self._parsed, result.category) or result.title
        self._folder_auto = str(
            recommend_folder(self._parsed, result.category, category_root)
        )
        self._folder_touched = False

    def compose(self) -> ComposeResult:
        with Vertical(id="organize-dialog"):
            yield Label("Name this download", id="organize-title")
            yield Static(self.result.title, id="organize-orig")
            yield Label("Name", classes="organize-label")
            yield Input(value=self._suggested, id="organize-name")
            yield Label("Save to", classes="organize-label")
            yield Input(value=self._folder_auto, id="organize-folder")
            yield Static(
                "Junk files (.nfo, .txt, samples) are skipped automatically.",
                id="organize-hint",
            )
            with Horizontal(id="organize-buttons"):
                yield Button("Save [Enter]", variant="success", id="btn-organize-save")
                yield Button("As-is", variant="default", id="btn-organize-asis")
                yield Button("Skip [Esc]", variant="default", id="btn-organize-cancel")

    def on_mount(self) -> None:
        self.query_one("#organize-name", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "organize-name" and not self._folder_touched:
            parsed = (
                parse_release_name(event.value)
                if event.value.strip()
                else self._parsed
            )
            folder = str(
                recommend_folder(parsed, self.result.category, self.category_root)
            )
            field = self.query_one("#organize-folder", Input)
            if field.value != folder:
                self._folder_auto = folder
                field.value = folder  # fires on_input_changed for the folder…
        elif event.input.id == "organize-folder" and event.value != self._folder_auto:
            self._folder_touched = True  # …which this guard recognises as ours

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-organize-save":
            self._save()
        elif event.button.id == "btn-organize-asis":
            self.dismiss(OrganizeChoice(organize=False))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        name = self.query_one("#organize-name", Input).value.strip() or self._suggested
        name = re.sub(r"[/:\x00]", "-", name)
        folder = (
            self.query_one("#organize-folder", Input).value.strip()
            or self.category_root
        )
        folder_path = Path(folder).expanduser()
        if not folder_path.is_absolute():
            folder_path = Path(self.category_root) / folder
        self.dismiss(OrganizeChoice(organize=True, name=name, folder=str(folder_path)))


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


def _format_uptime(seconds: float | None) -> str:
    """Compact age for the daemon header. None when we cannot know it."""
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours, minutes = divmod(int(seconds // 60), 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


class SettingsScreen(ModalScreen[bool]):
    """Speed, seeding and network tunables, grouped by when they take effect.

    The grouping is the whole design. aria2 answers "OK" to every option on
    this screen, but only the throttles change the running daemon: seeding and
    encryption become the template for newly *added* downloads, and the listen
    port and LPD switch are fixed until aria2 restarts. One undifferentiated
    "Saved" would be telling the user something untrue about two thirds of the
    screen, so each group says what will actually happen to it.

    The header exists for the same reason the daemon does: aria2 is a separate
    process that can die, wedge, or be someone else's, and none of that is
    visible from a list of torrents that simply stops updating.
    """

    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-dialog {
        width: 78;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #settings-title {
        text-style: bold;
    }
    #settings-daemon {
        margin-bottom: 1;
    }
    #settings-ownership {
        color: $text-muted;
        margin-bottom: 1;
    }
    #settings-body {
        height: auto;
        max-height: 28;
    }
    .settings-group {
        text-style: bold;
        margin-top: 1;
    }
    .settings-tier {
        color: $text-muted;
        margin-bottom: 1;
    }
    .settings-label {
        color: $text-muted;
    }
    .settings-hint {
        color: $text-muted;
    }
    #settings-seed-note {
        margin-top: 1;
    }
    #settings-buttons {
        height: 3;
        margin-top: 1;
        align: center middle;
    }
    #settings-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    # Grouped by tier, because that is what changes what the user should
    # expect after pressing Save.
    GROUPS = (
        ("Speed", settings.TIER_LIVE,
         ("max_download_rate", "max_upload_rate", "max_concurrent")),
        ("Seeding & encryption", settings.TIER_NEW,
         ("seed_ratio", "seed_time", "encryption")),
        ("Network", settings.TIER_RESTART,
         ("listen_port", "enable_lpd")),
    )

    TIER_BLURB = {
        settings.TIER_LIVE: "Takes effect on the running daemon immediately.",
        settings.TIER_NEW: (
            "Applies to downloads added from now on; torrents already queued "
            "keep theirs."
        ),
        settings.TIER_RESTART: (
            "Fixed when aria2 starts — quit and relaunch trrnt to apply."
        ),
    }

    def __init__(self, config: Config, daemon=None):
        super().__init__()
        self.config = config
        self.daemon = daemon
        self._before = {
            field.key: config.get("aria2", field.key) for field in settings.FIELDS
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Label("Settings", id="settings-title")
            yield Static("checking aria2…", id="settings-daemon")
            yield Static("", id="settings-ownership")
            with VerticalScroll(id="settings-body"):
                for title, tier, keys in self.GROUPS:
                    yield Label(title, classes="settings-group")
                    yield Static(self.TIER_BLURB[tier], classes="settings-tier")
                    for key in keys:
                        yield from self._field_widgets(settings.FIELDS_BY_KEY[key])
                        # Directly under the pair it explains, not at the end
                        # of the group — it is the reading of those two boxes.
                        if key == "seed_time":
                            yield Static(self._seed_note(), id="settings-seed-note")
            with Horizontal(id="settings-buttons"):
                yield Button("Save [Enter]", variant="success", id="btn-settings-save")
                yield Button("Cancel [Esc]", variant="default", id="btn-settings-cancel")

    def _field_widgets(self, field: settings.Field):
        """One row per field — the widget type follows the value type."""
        current = self._before.get(field.key)

        if field.key == "encryption":
            yield Label(field.label, classes="settings-label")
            buttons = [
                RadioButton(choice, value=(choice == str(current)))
                for choice in settings.ENCRYPTION_CHOICES
            ]
            yield RadioSet(*buttons, id="set-encryption")
        elif field.key == "enable_lpd":
            yield Checkbox(field.label, value=bool(current), id="set-enable_lpd")
        else:
            yield Label(field.label, classes="settings-label")
            yield Input(value="" if current is None else str(current),
                        id=f"set-{field.key}")

        if field.hint:
            yield Static(field.hint, classes="settings-hint")

    def on_mount(self) -> None:
        self.query_one("#set-max_download_rate", Input).focus()
        self._load_daemon_line()

    # ── Daemon header ────────────────────────────────────────────────────────

    @work(exclusive=True, group="settings_daemon")
    async def _load_daemon_line(self) -> None:
        """Fill in the header without blocking the screen from opening.

        Uptime comes from ps(1) via a thread: it is a subprocess call, and the
        one thing on this screen that would otherwise stall the event loop.
        """
        line = self.query_one("#settings-daemon", Static)
        try:
            alive = await self.app.aria2.check_connection()
        except Exception:
            alive = False
        if not alive:
            line.update("[red]●[/] aria2 is not responding")
            self.query_one("#settings-ownership", Static).update(
                "Downloads cannot start until it is back."
            )
            return

        parts = ["[green]●[/] aria2 running"]

        try:
            options = await self.app.aria2.get_global_option()
        except Exception:
            options = {}
        bound = (options.get("interface") or "").strip()
        if bound:
            parts.append(f"bound [green]{bound}[/]")
        elif self.config.get("vpn", "enabled"):
            parts.append("[bold red]unbound[/]")
        else:
            parts.append("[dim]unbound[/]")

        uptime = None
        if self.daemon is not None:
            try:
                uptime = await asyncio.to_thread(self.daemon.uptime_seconds)
            except Exception:
                uptime = None
        if uptime is not None:
            parts.append(f"up {_format_uptime(uptime)}")

        line.update(" · ".join(parts))
        self.query_one("#settings-ownership", Static).update(self._ownership_text())

    def _ownership_text(self) -> str:
        """Whether quitting trrnt takes the downloads with it.

        More actionable than uptime on its own: an adopted daemon keeps
        running after you quit, and a trrnt-owned one does not.
        """
        if self.daemon is None:
            return "Started outside this session — left running when you quit."
        if self.daemon.owns_daemon:
            return "Started by trrnt — stops when you quit, and resumes next launch."
        return "Adopted an aria2 you started — left running when you quit."

    # ── Editing ──────────────────────────────────────────────────────────────

    def _raw_values(self) -> dict[str, str]:
        """Every field as text, ready for settings.parse_all."""
        raw: dict[str, str] = {}
        for field in settings.FIELDS:
            if field.key == "encryption":
                chosen = self.query_one("#set-encryption", RadioSet).pressed_index
                raw[field.key] = settings.ENCRYPTION_CHOICES[max(chosen, 0)]
            elif field.key == "enable_lpd":
                checked = self.query_one("#set-enable_lpd", Checkbox).value
                raw[field.key] = "true" if checked else "false"
            else:
                raw[field.key] = self.query_one(f"#set-{field.key}", Input).value
        return raw

    def _seed_note(self, ratio=None, seed_time=None) -> str:
        """Plain English for the ratio/time pair, whose zeroes invert."""
        try:
            return settings.describe_seeding(
                float(self._before["seed_ratio"] if ratio is None else ratio),
                self._before["seed_time"] if seed_time is None else seed_time,
            )
        except (TypeError, ValueError):
            return ""

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id not in ("set-seed_ratio", "set-seed_time"):
            return
        note = self._seed_note(
            ratio=self.query_one("#set-seed_ratio", Input).value,
            seed_time=self.query_one("#set-seed_time", Input).value,
        )
        self.query_one("#settings-seed-note", Static).update(note)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-settings-save":
            self._save()
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    # ── Saving ───────────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            values = settings.parse_all(self._raw_values())
        except settings.InvalidSetting as e:
            self.app.notify(str(e), severity="error", timeout=8)
            return

        tiers = settings.changed_tiers(self._before, values)
        if not tiers:
            self.dismiss(False)
            return

        try:
            onboard.write_config_values(
                self.config.path,
                {("aria2", key): value for key, value in values.items()},
            )
        except OSError as e:
            self.app.notify(f"Could not save: {e}", severity="error")
            return
        self.config.reload()

        self._apply_live(values, tiers)
        self.dismiss(True)

    @work(group="settings_apply")
    async def _apply_live(self, values: dict, tiers: set[str]) -> None:
        """Push what the running daemon can actually take, then say what stuck.

        Restart-tier options are deliberately not sent: aria2 would answer OK
        and change nothing, and a success message for a setting that did not
        take is worse than no message at all.
        """
        pushable = tuple(t for t in (settings.TIER_LIVE, settings.TIER_NEW) if t in tiers)
        if pushable:
            options = settings.aria2_options(values, tiers=pushable)
            try:
                await self.app.aria2.change_global_option(options)
            except Exception as e:
                self.app.notify(
                    f"Saved to config, but aria2 rejected them: {e}",
                    severity="warning", timeout=8,
                )
                return

        messages = []
        if settings.TIER_LIVE in tiers:
            messages.append("speed limits are live")
        if settings.TIER_NEW in tiers:
            messages.append("seeding applies to new downloads")
        if settings.TIER_RESTART in tiers:
            messages.append("port/LPD need a relaunch")
        self.app.notify("Saved — " + ", ".join(messages), timeout=6)


class HomeScreen(Screen[str | None]):
    """Landing page: the wordmark, a quick search, service health at a glance.

    The splash used to print into scrollback while services started, then the
    working screen erased it. This gives those pixels a job: it greets an idle
    launch, fronts the same search the main screen runs, and its status line
    is live — a red aria2 dot here is the first thing a broken daemon shows.

    Exits are always the user's: Enter hands the query to the main screen's
    search, esc goes straight to the working screen. This screen used to step
    aside on its own when aria2 reported downloads moving, but a seeding
    torrent counts as active — so with any seed ratio set, the launch you
    actually saw was almost never this one. Landing somewhere predictable
    beats saving a keypress.

    Esc still reads the download count to decide where to *put* you: on the
    downloads table when there is something in it, on the search box when
    there is not.
    """

    CSS = """
    HomeScreen { align: center middle; }
    /* align centers the children as one block, not one by one — the block is
       as wide as the widest child and the rest hug its left edge. Uniform
       widths make the block itself the column; text-align does the rest. */
    HomeScreen > * { width: 64; max-width: 90%; }
    HomeScreen > Static { text-align: center; }
    #home-tagline { color: $text-muted; margin-bottom: 2; }
    #home-status { margin-top: 1; }
    #home-meta { color: $text-muted; margin-top: 1; }
    #home-update { height: auto; margin-top: 1; }
    #home-hints { color: $text-muted; margin-top: 2; }
    """

    BINDINGS = [
        Binding("escape", "browse", "Browse"),
        Binding("ctrl+u", "upgrade", "Update", priority=True),
    ]

    # Set before dismissal so the app's callback can land focus on the
    # Downloads table instead of the search box. Reading it off the screen
    # keeps the dismissal result free to be exactly the query or None.
    to_downloads = False

    def __init__(self):
        super().__init__()
        self._outdated: dict[str, tuple[str, str]] = {}
        self._upgrading = False
        # Latest count from the aria2 probe. Only decides where esc lands.
        self._moving = 0

    def compose(self) -> ComposeResult:
        yield Static(splash_text(), id="home-logo")
        yield Static(TAGLINE, id="home-tagline")
        yield Input(placeholder="Search torrents... (Enter to search)", id="home-search")
        yield Static("", id="home-status")
        yield Static("", id="home-meta")
        yield Static("", id="home-update")
        yield Static(
            f"[bold {ACCENT}]enter[/] search   [bold {ACCENT}]esc[/] downloads   "
            f"[bold {ACCENT}]^k[/] keys   [bold {ACCENT}]^q[/] quit",
            id="home-hints",
        )

    def on_mount(self) -> None:
        url = self.app.config.get("jackett", "url", default="") or ""
        host = urlsplit(url).netloc or url or "unconfigured"
        self.query_one("#home-meta", Static).update(f"v{VERSION} — jackett @ {host}")
        self.query_one("#home-search", Input).focus()
        self._watch_services()
        self._check_for_updates()

    @work(exclusive=True, group="home_status")
    async def _watch_services(self) -> None:
        """Keep the health line current for as long as the screen is up.

        aria2 and clamd are probed every tick — both may still be coming up
        under us, and green resolving in place is the point of the line. The
        VPN is checked once: its check reaches out for the external IP, too
        heavy to repeat every two seconds, and the app's kill switch owns the
        ongoing watch anyway. The worker dies with the screen, so no tick
        outlives a dismissal.
        """
        vpn_part = f"[{DIM}]vpn off[/]"
        vpn_pending = bool(self.app.config.get("vpn", "enabled"))
        if vpn_pending:
            vpn_part = f"[{DIM}]vpn …[/]"
        first = True
        while True:
            aria2_part, moving = await self._probe_aria2()
            self._moving = moving
            clam_part = await self._probe_clamav()
            self.query_one("#home-status", Static).update(
                f"{aria2_part}   {vpn_part}   {clam_part}"
            )
            if first:
                first = False
                if vpn_pending:
                    vpn = await self.app.vpn.check()
                    if vpn.connected:
                        vpn_part = f"[green]●[/] vpn {vpn.interface}"
                    else:
                        vpn_part = "[red]●[/] vpn down"
            await asyncio.sleep(2)

    async def _probe_aria2(self) -> tuple[str, int]:
        """One status fragment, plus how many downloads are moving."""
        try:
            stats = await self.app.aria2.get_global_stat()
        except Exception:
            return "[red]●[/] aria2 down", 0
        moving = int(stats.get("numActive", 0)) + int(stats.get("numWaiting", 0))
        if moving:
            return f"[green]●[/] aria2 · {moving} active", moving
        return "[green]●[/] aria2", 0

    async def _probe_clamav(self) -> str:
        clam = await self.app.security.check_clamav_available()
        if clam["installed"]:
            if clam["daemon_running"]:
                return "[green]●[/] av"
            return "[yellow]●[/] av no daemon"  # matches the status bar's phrasing
        return "[red]●[/] av missing"

    @work(exclusive=True, group="home_update")
    async def _check_for_updates(self) -> None:
        """Surface stale Homebrew formulas, at most once a day.

        Jackett cannot update itself under Homebrew (its launcher passes
        --NoUpdates), so nothing tells the user their indexer definitions
        have gone stale — the failure mode is searches quietly returning
        less. A line on the screen they already open beats a chore they
        have to remember.
        """
        import time

        if not onboard.brew_path():
            return
        cache = onboard.read_update_cache()
        if not onboard.update_check_due(time.time(), cache):
            stored = cache.get("outdated") or {}
            self._outdated = {k: tuple(v) for k, v in stored.items()}
        else:
            found = {}
            for formula in ("jackett", "aria2"):
                result = await asyncio.to_thread(onboard.brew_outdated, formula)
                if result:
                    found[formula] = result
            self._outdated = found
            onboard.write_update_cache(
                {"checked_at": time.time(),
                 "outdated": {k: list(v) for k, v in found.items()}}
            )
        self._render_update_line()

    def _render_update_line(self) -> None:
        line = self.query_one("#home-update", Static)
        if self._upgrading:
            return  # the worker owns the line while it runs
        if not self._outdated:
            line.update("")
            return
        parts = [f"{name} {old} → {new}"
                 for name, (old, new) in sorted(self._outdated.items())]
        line.update(
            f"[{SEED_WARN}]●[/] update available: {', '.join(parts)}   "
            f"[bold {ACCENT}]^u[/] to upgrade"
        )

    def action_upgrade(self) -> None:
        if self._outdated and not self._upgrading:
            self._run_upgrade()
            return
        # Nothing to upgrade. ctrl+u is Input's clear-the-line, and this
        # binding is priority, so it would otherwise swallow that habit on
        # every launch to buy an affordance that is only live occasionally.
        focused = self.focused
        if isinstance(focused, Input):
            focused.action_delete_left_all()

    @work(exclusive=True, group="home_upgrade")
    async def _run_upgrade(self) -> None:
        """brew upgrade + service restart, reporting progress in place."""
        import time

        self._upgrading = True
        line = self.query_one("#home-update", Static)
        formulas = sorted(self._outdated)
        failed = []
        try:
            for name in formulas:
                line.update(f"[{ACCENT}]●[/] upgrading {name}…")
                ok = await onboard.upgrade_formula(name, lambda _l: None)
                if not ok:
                    failed.append(name)
        finally:
            self._upgrading = False

        # Re-check rather than assume: an upgrade that half-worked should
        # leave the line telling the truth, not a cheerful message.
        remaining = {}
        for name in formulas:
            result = await asyncio.to_thread(onboard.brew_outdated, name)
            if result:
                remaining[name] = result
        self._outdated = remaining
        onboard.write_update_cache(
            {"checked_at": time.time(),
             "outdated": {k: list(v) for k, v in remaining.items()}}
        )
        if failed:
            self.app.notify(
                f"Could not upgrade {', '.join(failed)} — try brew upgrade by hand",
                severity="error",
            )
        elif not remaining:
            self.app.notify("Up to date — services restarted")
        self._render_update_line()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Submitted bubbles past this handler to the app's; without the stop
        # the app would run the search a second time. An empty Enter stays put.
        event.stop()
        query = event.value.strip()
        if query:
            self.dismiss(query)

    def action_browse(self) -> None:
        """Leave for the working screen, landing where there is something to see."""
        self.to_downloads = self._moving > 0
        self.dismiss(None)


class IndexersScreen(ModalScreen[None]):
    """Manage indexers any time — not just during setup.

    Exists because a Cloudflare-gated tracker fails on every single search,
    and the only remedies used to be Jackett's web UI or hand-editing YAML.
    Space excludes an indexer from trrnt's searches (reversible, local);
    ctrl+r deletes it from Jackett outright (not reversible from here).
    """

    CSS = """
    IndexersScreen { align: center middle; }
    #indexers-dialog {
        width: 78; max-width: 96%; height: auto; max-height: 90%;
        background: $surface; border: thick $primary; padding: 1 2;
    }
    #indexers-title { text-style: bold; color: $primary; }
    #indexers-help { color: $text-muted; margin-bottom: 1; }
    #indexers-list { height: auto; max-height: 16; margin-bottom: 1; }
    #indexers-status { height: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+n", "close", "Close", priority=True),
        Binding("ctrl+t", "test_all", "Test all", priority=True),
        Binding("ctrl+r", "remove_indexer", "Remove from Jackett", priority=True),
    ]

    def __init__(self, admin_factory=None):
        super().__init__()
        self._admin_factory = admin_factory
        self._rows: list[dict] = []          # {id, name, excluded}
        self._health: dict[str, tuple[str, str]] = {}
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="indexers-dialog"):
            yield Label("Indexers", id="indexers-title")
            yield Static(
                "space  include / exclude from searches\n"
                "^t  test all      ^r  delete from Jackett      esc  close",
                id="indexers-help",
            )
            yield SelectionList(id="indexers-list")
            yield Static("Loading…", id="indexers-status")

    def on_mount(self) -> None:
        self.load_indexers()

    def _admin(self):
        url = self.app.config.get("jackett", "url") or "http://localhost:9117"
        factory = self._admin_factory or onboard.JackettAdmin
        return factory(url)

    def _excluded(self) -> set[str]:
        configured = self.app.config.get("jackett", "exclude_indexers", default=[]) or []
        return {i.strip().lower() for i in configured}

    @work(exclusive=True, group="indexers_load")
    async def load_indexers(self) -> None:
        admin = self._admin()
        try:
            await admin.login()
            catalog = await admin.catalog()
        except onboard.JackettAdminError as e:
            self.query_one("#indexers-status", Static).update(
                f"[{SEED_NONE}]Cannot reach Jackett ({e})[/]"
            )
            return
        finally:
            await admin.close()

        excluded = self._excluded()
        self._rows = sorted(
            ({"id": i["id"], "name": i.get("name") or i["id"],
              "excluded": i["id"].lower() in excluded}
             for i in catalog if i.get("configured")),
            key=lambda r: r["name"].lower(),
        )
        self._render_rows()
        if not self._rows:
            self.query_one("#indexers-status", Static).update(
                "No indexers configured in Jackett yet."
            )

    def _render_rows(self) -> None:
        picker = self.query_one("#indexers-list", SelectionList)
        # Preserve where the user was; clear() resets the highlight.
        cursor = picker.highlighted
        picker.clear_options()
        for row in self._rows:
            verdict, detail = self._health.get(row["id"], ("", ""))
            mark = {"ok": f"[{SEED_GOOD}]●[/]", "cloudflare": f"[{SEED_WARN}]●[/]",
                    "error": f"[{SEED_NONE}]●[/]", "testing": f"[{DIM}]◌[/]"}.get(verdict, " ")
            # No "(excluded)" suffix: the tickbox already says that, and it
            # would only be redrawn on load — a toggle does not rebuild the
            # list, so the two would disagree the moment anyone pressed space.
            label = f"{mark} {row['name']}"
            if detail:
                label += f"  [{DIM}]{detail}[/]"
            picker.add_option(
                Selection(label, row["id"], initial_state=not row["excluded"])
            )
        if cursor is not None and self._rows:
            picker.highlighted = min(cursor, len(self._rows) - 1)
        self._update_status()

    def _update_status(self) -> None:
        active = sum(1 for r in self._rows if not r["excluded"])
        blocked = [i for i, (v, _) in self._health.items() if v == "cloudflare"]
        line = f"{active} of {len(self._rows)} searched"
        if blocked:
            line += (f"   [{SEED_WARN}]{len(blocked)} behind Cloudflare[/] — "
                     "space to exclude them")
        self.query_one("#indexers-status", Static).update(line)

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged
    ) -> None:
        """Selected == searched. Persist the inverse as the exclude list."""
        if self._busy or not self._rows:
            return
        selected = set(event.selection_list.selected)
        for row in self._rows:
            row["excluded"] = row["id"] not in selected
        excluded = sorted(r["id"] for r in self._rows if r["excluded"])
        try:
            onboard.write_config_values(
                self.app.config.path, {("jackett", "exclude_indexers"): excluded}
            )
        except OSError as e:
            self.app.notify(f"Could not save: {e}", severity="error")
            return
        self.app.config.reload()
        # Rebuild the client so the next search honours this immediately,
        # rather than at the next launch.
        self.app.jackett = JackettSearch(self.app.config.get("jackett"))
        self._update_status()

    def action_test_all(self) -> None:
        if not self._busy and self._rows:
            self.test_all()

    @work(exclusive=True, group="indexers_test")
    async def test_all(self) -> None:
        self._busy = True
        admin = self._admin()
        try:
            await admin.login()
            for row in self._rows:
                self._health[row["id"]] = ("testing", "")
            self._render_rows()
            results = await asyncio.gather(*(
                admin.test_indexer(r["id"]) for r in self._rows
            ), return_exceptions=True)
            for row, outcome in zip(self._rows, results):
                if isinstance(outcome, BaseException):
                    self._health[row["id"]] = ("error", str(outcome)[:40])
                else:
                    self._health[row["id"]] = outcome
        except onboard.JackettAdminError as e:
            self.app.notify(f"Jackett: {e}", severity="error")
            self._health.clear()
        finally:
            await admin.close()
            self._busy = False
        self._render_rows()

    def action_remove_indexer(self) -> None:
        picker = self.query_one("#indexers-list", SelectionList)
        index = picker.highlighted
        if self._busy or index is None or index >= len(self._rows):
            return
        self.remove_indexer(self._rows[index])

    @work(exclusive=True, group="indexers_remove")
    async def remove_indexer(self, row: dict) -> None:
        self._busy = True
        admin = self._admin()
        try:
            await admin.login()
            await admin.delete_indexer(row["id"])
        except onboard.JackettAdminError as e:
            self.app.notify(f"Could not remove {row['id']}: {e}", severity="error")
            return
        finally:
            await admin.close()
            self._busy = False
        self._rows = [r for r in self._rows if r["id"] != row["id"]]
        self._health.pop(row["id"], None)
        self._render_rows()
        self.app.notify(f"Removed {row['name']} from Jackett")

    def action_close(self) -> None:
        self.dismiss(None)


class SetupScreen(Screen[None]):
    """First-run wizard: install, wire up, and verify the whole stack.

    Every step is written to be re-runnable — Esc leaves at any point and
    `trrnt setup` resumes from a clean detect — and every automation keeps a
    manual way out, because the Jackett admin API this leans on is the one
    Jackett's own UI uses, not a documented surface.

    The engine is one worker awaiting step coroutines in order; interactive
    steps park on a Future that the button/input handlers resolve. Escape
    dismisses the screen, which cancels the worker at its next await.
    """

    CSS = """
    SetupScreen { align: center middle; }
    SetupScreen > * { width: 74; max-width: 96%; }
    #setup-title { text-style: bold; color: $primary; }
    #setup-steps { margin-top: 1; }
    #setup-detail { margin-top: 1; min-height: 2; }
    #setup-widgets { height: auto; margin-top: 1; }
    #setup-widgets Horizontal { height: auto; }
    #setup-widgets Button { margin-right: 2; }
    #setup-widgets SelectionList { max-height: 10; margin-bottom: 1; }
    #setup-widgets RadioSet { margin-bottom: 1; }
    #setup-widgets Input { margin-bottom: 1; }
    #setup-log { height: 9; display: none; margin-top: 1; }
    #setup-hints { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "leave", "Skip setup"),
    ]

    STEPS = [
        ("brew", "Homebrew"),
        ("packages", "Install aria2 · Jackett · ClamAV"),
        ("services", "Start services"),
        ("api_key", "Jackett API key"),
        ("indexers", "Add indexers"),
        ("vpn", "VPN kill switch"),
        ("aria2", "Start aria2"),
        ("verify", "Verify"),
    ]

    _MARK_COLOR = {" ": DIM, "…": ACCENT, "✓": SEED_GOOD, "✗": SEED_NONE, "–": SEED_WARN}

    def __init__(self, admin_factory=None):
        super().__init__()
        # Injectable for tests; resolved lazily so monkeypatching
        # onboard.JackettAdmin also works.
        self._admin_factory = admin_factory
        self._marks = {key: " " for key, _ in self.STEPS}
        self._answer: asyncio.Future | None = None
        self.daemon = None  # Aria2Daemon started at the end; main.py owns shutdown

    def compose(self) -> ComposeResult:
        yield Static("trrnt setup", id="setup-title")
        yield Static("", id="setup-steps")
        yield Static("", id="setup-detail")
        yield Container(id="setup-widgets")
        yield Log(id="setup-log")
        yield Static("esc to skip setup — resume anytime with: trrnt setup", id="setup-hints")

    def on_mount(self) -> None:
        self._render_steps()
        self.run_wizard()

    # ── engine ────────────────────────────────────────────────────────────

    @work(exclusive=True, group="setup")
    async def run_wizard(self) -> None:
        if self.app.config.ensure_config_exists():
            self.app.config.reload()
        steps = {
            "brew": self._step_brew,
            "packages": self._step_packages,
            "services": self._step_services,
            "api_key": self._step_api_key,
            "indexers": self._step_indexers,
            "vpn": self._step_vpn,
            "aria2": self._step_aria2,
            "verify": self._step_verify,
        }
        for key, _ in self.STEPS:
            self._mark(key, "…")
            self._mark(key, await steps[key]())
            await self._show_widgets()
        self.dismiss(None)

    def _mark(self, key: str, mark: str) -> None:
        self._marks[key] = mark
        self._render_steps()

    def _render_steps(self) -> None:
        rows = []
        for key, title in self.STEPS:
            mark = self._marks[key]
            style = self._MARK_COLOR.get(mark, DIM)
            title_markup = f"[bold]{title}[/]" if mark == "…" else title
            rows.append(f" [{style}]{mark or ' '}[/] {title_markup}")
        self.query_one("#setup-steps", Static).update("\n".join(rows))

    def _detail(self, markup: str) -> None:
        self.query_one("#setup-detail", Static).update(markup)

    async def _show_widgets(self, *widgets) -> None:
        box = self.query_one("#setup-widgets", Container)
        await box.remove_children()
        for w in widgets:
            await box.mount(w)
        for w in widgets:
            if isinstance(w, (Input, SelectionList)):
                w.focus()
                break

    async def _ask(self):
        self._answer = asyncio.get_running_loop().create_future()
        try:
            return await self._answer
        finally:
            self._answer = None

    def _reply(self, value) -> None:
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._reply(("button", event.button.id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # the app's search handler must never see wizard inputs
        self._reply(("input", event.value))

    def action_leave(self) -> None:
        self.app.notify("Setup paused — resume anytime with: trrnt setup")
        self.dismiss(None)

    # ── steps ─────────────────────────────────────────────────────────────

    async def _step_brew(self) -> str:
        if onboard.brew_path():
            self._detail("Homebrew found.")
            return "✓"
        self._detail(
            "Homebrew is missing — install it from [bold]https://brew.sh[/], "
            "then press Retry.\nIts installer needs your password, so trrnt "
            "won't run it for you."
        )
        while True:
            await self._show_widgets(Horizontal(
                Button("Retry", variant="primary", id="setup-retry"),
                Button("Skip", id="setup-skip"),
            ))
            _, pressed = await self._ask()
            if pressed == "setup-skip":
                return "–"
            if onboard.brew_path():
                return "✓"

    async def _step_packages(self) -> str:
        missing = [
            f for f in onboard.BREW_FORMULAS
            if not await asyncio.to_thread(onboard.component_installed, f)
        ]
        if not missing:
            self._detail("aria2, Jackett and ClamAV are already installed.")
            return "✓"
        if not onboard.brew_path():
            self._detail("Can't install packages without Homebrew — skipped.")
            return "–"
        log = self.query_one("#setup-log", Log)
        log.styles.display = "block"
        failed = []
        for formula in missing:
            self._detail(f"Installing [bold]{formula}[/]…")
            rc = await onboard.run_streaming(
                ["brew", "install", formula], log.write_line
            )
            if rc != 0:
                failed.append(formula)
        log.styles.display = "none"
        if failed:
            self._detail(
                f"Failed: {', '.join(failed)} — install by hand, "
                "then run trrnt setup again."
            )
            return "✗"
        return "✓"

    async def _step_services(self) -> str:
        # ClamAV is strictly best-effort: give it configs, kick off the
        # definitions download, try the daemon. It must never block setup —
        # scans simply activate once freshclam finishes.
        if onboard.binary_present("clamd"):
            note = await asyncio.to_thread(onboard.clamav_conf_bootstrap)
            try:
                subprocess.run(["pgrep", "-x", "clamd"], capture_output=True,
                               check=True)
            except subprocess.CalledProcessError:
                if note and note.startswith("created"):
                    subprocess.Popen(["freshclam"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                subprocess.Popen(["clamd"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        if onboard.brew_path():
            self._detail("Starting Jackett…")
            await onboard.run_streaming(
                ["brew", "services", "start", "jackett"], lambda _line: None
            )
        url = onboard.jackett_url(onboard.read_jackett_server_config())
        for _ in range(25):
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    if (await client.get(url)).status_code < 500:
                        self._detail("Jackett is up. AV definitions may still "
                                     "be downloading in the background.")
                        return "✓"
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        self._detail(
            "Jackett didn't come up — start it yourself "
            "([bold]brew services start jackett[/]) and run trrnt setup again."
        )
        return "✗"

    async def _step_api_key(self) -> str:
        self._detail("Reading the API key Jackett minted for itself…")
        found = await onboard.wait_for(
            lambda: onboard.read_jackett_server_config() is not None, timeout=30
        )
        if found:
            server = onboard.read_jackett_server_config()
            onboard.write_config_values(self.app.config.path, {
                ("jackett", "api_key"): server["APIKey"],
                ("jackett", "url"): onboard.jackett_url(server),
            })
            self._detail("API key captured — nothing to copy, nothing to type.")
        else:
            # Jackett on another box or in Docker: its config file isn't
            # ours to read, so this is the one step that may need typing.
            self._detail(
                "Couldn't find Jackett's config on this machine — running it "
                "elsewhere?\nEnter its URL, then its API key "
                "(top-right in the Jackett UI)."
            )
            await self._show_widgets(
                Input(placeholder="http://nas.local:9117", id="setup-jackett-url"))
            _, url = await self._ask()
            await self._show_widgets(
                Input(placeholder="Jackett API key", password=True,
                      id="setup-jackett-key"))
            _, key = await self._ask()
            if not key.strip():
                return "–"
            onboard.write_config_values(self.app.config.path, {
                ("jackett", "api_key"): key.strip(),
                ("jackett", "url"): (url.strip() or "http://localhost:9117").rstrip("/"),
            })
        self.app.config.reload()
        self.app.jackett = JackettSearch(self.app.config.get("jackett"))
        return "✓"

    async def _step_indexers(self) -> str:
        url = self.app.config.get("jackett", "url") or "http://localhost:9117"
        factory = self._admin_factory or onboard.JackettAdmin
        admin = factory(url)
        try:
            try:
                await admin.login()
            except onboard.JackettAdminError:
                self._detail("Jackett has an admin password — enter it, or Skip "
                             "and add indexers in its UI later.")
                await self._show_widgets(
                    Input(placeholder="Jackett admin password", password=True,
                          id="setup-jackett-pass"),
                    Horizontal(Button("Skip", id="setup-skip")),
                )
                kind, value = await self._ask()
                if kind == "button":
                    return "–"
                try:
                    await admin.login(value)
                except onboard.JackettAdminError:
                    self._detail("Still locked out — indexers can be added in "
                                 "the Jackett UI anytime.")
                    return "–"

            already = await admin.configured_ids()
            if already:
                self._detail(f"{len(already)} indexer(s) already configured.")
                return "✓"

            choices = onboard.order_catalog(await admin.catalog())
            if not choices:
                self._detail("Jackett returned an empty catalog — add indexers "
                             "in its UI.")
                return "–"
            self._detail(
                "Pick public indexers — space toggles, popular ones are "
                "pre-selected.\nOnes marked [bold]needs FlareSolverr[/] sit "
                "behind Cloudflare and stay off by default.\nPrivate trackers "
                "need credentials: use [bold]Open Jackett UI[/] for those."
            )
            await self._show_widgets(
                SelectionList(*[
                    Selection(
                        (c.get("name") or c["id"])
                        + (" — needs FlareSolverr"
                           if c["id"] in onboard.NEEDS_SOLVER else ""),
                        c["id"],
                        # Curated *and* reachable without a solver: a first
                        # run should end with indexers that answer.
                        initial_state=(c["id"] in onboard.CURATED_PUBLIC
                                       and c["id"] not in onboard.NEEDS_SOLVER),
                    )
                    for c in choices
                ]),
                Horizontal(
                    Button("Add selected", variant="primary", id="setup-add"),
                    Button("Open Jackett UI", id="setup-open"),
                    Button("Skip", id="setup-skip"),
                ),
            )
            while True:
                _, pressed = await self._ask()
                if pressed == "setup-skip":
                    return "–"
                if pressed == "setup-open":
                    import webbrowser
                    webbrowser.open(url)
                    self._detail("Waiting for an indexer to appear in Jackett…")
                    for _ in range(150):
                        await asyncio.sleep(2)
                        if await admin.configured_ids():
                            self._detail("Indexer found.")
                            return "✓"
                    return "–"
                picked = self.query_one(SelectionList).selected
                if not picked:
                    self._detail("Nothing selected — space toggles a row.")
                    continue
                ok_ids, failed = [], []
                for indexer_id in picked:
                    self._detail(f"Adding [bold]{indexer_id}[/]… "
                                 f"({len(ok_ids) + len(failed) + 1}/{len(picked)})")
                    try:
                        await admin.add_indexer(indexer_id)
                        ok_ids.append(indexer_id)
                    except onboard.JackettAdminError:
                        failed.append(indexer_id)
                note = f"Added {len(ok_ids)} indexer(s)."
                if failed:
                    note += f" Failed: {', '.join(failed)} — try them in the Jackett UI."
                if ok_ids:
                    self._detail(note + "\nChecking which ones actually answer…")
                    note += "\n" + await self._report_indexer_health(admin, ok_ids)
                self._detail(note)
                await self._show_widgets(Horizontal(
                    Button("Continue", variant="primary", id="setup-continue")))
                await self._ask()
                return "✓" if ok_ids else "✗"
        except onboard.JackettAdminError as e:
            # The admin API is Jackett's own UI surface, not a contract —
            # if it drifts, the wizard degrades to opening the UI.
            self._detail(f"Jackett's admin API balked ({e}) — add indexers in "
                         "its UI; search works as soon as one exists.")
            return "–"
        finally:
            await admin.close()

    async def _report_indexer_health(self, admin, indexer_ids: list[str]) -> str:
        """Test each added indexer and say plainly what works.

        Adding an indexer only writes config — it never touches the site, so
        a Cloudflare-gated tracker looks like a success until the user's
        first search comes back short. Testing here turns that silent
        failure into a sentence they read before they leave setup.
        """
        results = await asyncio.gather(*(
            admin.test_indexer(i) for i in indexer_ids
        ), return_exceptions=True)

        working, blocked, broken = [], [], []
        for indexer_id, outcome in zip(indexer_ids, results):
            if isinstance(outcome, BaseException):
                broken.append(indexer_id)
                continue
            verdict, _detail = outcome
            if verdict == "ok":
                working.append(indexer_id)
            elif verdict == "cloudflare":
                blocked.append(indexer_id)
            else:
                broken.append(indexer_id)

        lines = [f"[{SEED_GOOD}]●[/] {len(working)} of {len(indexer_ids)} responding"]
        if blocked:
            lines.append(
                f"[{SEED_WARN}]●[/] {', '.join(blocked)} — behind Cloudflare; "
                "they need FlareSolverr and will return nothing until it is set up"
            )
        if broken:
            lines.append(f"[{SEED_NONE}]●[/] {', '.join(broken)} — not answering right now")
        return "\n".join(lines)

    async def _step_vpn(self) -> str:
        iface = self.app.vpn.find_vpn_interface()
        if iface:
            status = await self.app.vpn.check()
            ip = f" · external IP {status.vpn_ip}" if status.connected else ""
            evidence = f"Tunnel found: [bold]{iface}[/]{ip}."
            keep_label = "Bind BitTorrent to the VPN, auto-detected each launch (recommended)"
        else:
            evidence = "No VPN tunnel is up right now."
            keep_label = "Keep enforcement on — downloads wait until a VPN is connected (recommended)"
        self._detail(
            f"{evidence}\nEnforcement means aria2 is bound to the tunnel "
            "interface, so torrent traffic physically cannot use your ISP line."
        )
        await self._show_widgets(
            RadioSet(
                RadioButton(keep_label, value=True, id="setup-vpn-keep"),
                RadioButton("No VPN on this machine — allow direct downloads",
                            id="setup-vpn-off"),
            ),
            Horizontal(Button("Continue", variant="primary", id="setup-continue")),
        )
        await self._ask()
        if self.query_one("#setup-vpn-off", RadioButton).value:
            onboard.write_config_values(self.app.config.path, {
                ("vpn", "enabled"): False,
                ("aria2", "bt_interface"): "none",
            })
            # The mount-time kill switch is watching with the old settings —
            # without this it would pause every download the moment it
            # notices there is no tunnel.
            self.app.workers.cancel_group(self.app, "kill_switch")
        else:
            onboard.write_config_values(self.app.config.path, {
                ("vpn", "enabled"): True,
            })
        self.app.config.reload()
        self.app.vpn = VPNGuard(self.app.config.get("vpn"))
        return "✓"

    async def _step_aria2(self) -> str:
        cfg = self.app.config
        if not onboard.binary_present("aria2c"):
            self._detail("aria2 isn't installed — downloads stay off until it is.")
            return "✗"
        # Same resolution _resolve_bt_interface does for the CLI, minus the
        # console output a TUI can't host.
        configured = (cfg.get("aria2", "bt_interface") or "").strip()
        if configured.lower() == "none":
            bind = ""
        elif configured:
            bind = configured
        elif not cfg.get("vpn", "enabled", default=True):
            bind = ""
        else:
            bind = self.app.vpn.find_vpn_interface() or ""
            if not bind:
                self._detail("VPN enforcement is on and no tunnel is up — "
                             "aria2 starts on your next launch, once the VPN "
                             "is connected.")
                return "–"
        self._detail("Starting aria2…")
        from .daemon import Aria2Daemon
        daemon = Aria2Daemon(cfg.get("aria2"), bind_interface=bind)
        result = await asyncio.to_thread(daemon.ensure_running)
        if result not in ("adopted", "reclaimed", "replaced", "started"):
            self._detail("aria2 didn't start — is its RPC port already in use? "
                         "[bold]trrnt config[/] shows the connection checks.")
            return "✗"
        self.daemon = daemon
        if bind:
            actual = await asyncio.to_thread(daemon.bound_interface)
            if actual != bind:
                self._detail(f"⚠ aria2 is NOT bound to {bind} — quit any aria2 "
                             "you started yourself, then relaunch trrnt.")
                return "✗"
            self._detail(f"aria2 up, bound to [bold]{bind}[/].")
        else:
            self._detail("aria2 up.")
        return "✓"

    async def _step_verify(self) -> str:
        app = self.app
        jack_ok = await app.jackett.check_connection()
        aria_ok = await app.aria2.check_connection()
        clam = await app.security.check_clamav_available()
        if clam["installed"] and clam["daemon_running"]:
            av_line = f"[{SEED_GOOD}]●[/] ClamAV"
        elif clam["installed"]:
            av_line = f"[{SEED_WARN}]●[/] ClamAV — daemon warming up (definitions may still be downloading)"
        else:
            av_line = f"[{SEED_NONE}]●[/] ClamAV missing"
        dot = lambda ok: f"[{SEED_GOOD}]●[/]" if ok else f"[{SEED_NONE}]●[/]"
        self._detail(
            f"{dot(jack_ok)} Jackett\n{dot(aria_ok)} aria2\n{av_line}\n\n"
            "Setup can be re-run anytime with [bold]trrnt setup[/]."
        )
        await self._show_widgets(Horizontal(
            Button("Start trrnt", variant="primary", id="setup-done")))
        await self._ask()
        return "✓" if (jack_ok and aria_ok) else "✗"


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
        /* Fixed, so the layout never shifts as downloads come and go. The
           percentage is the small-terminal escape hatch: on a short window
           the section gives way rather than crowding out the results. Set so
           the full six rows still survive an ordinary 40-line terminal. */
        height: 19;
        max-height: 60%;
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
        Binding("ctrl+p", "pause_all", "Pause / Resume", priority=True, show=False),
        # Was ctrl+i and ctrl+h. Both were undeliverable — the terminal sends
        # Tab and Backspace for those bytes — so neither key had ever worked.
        Binding("ctrl+e", "inspect_result", "Inspect", priority=True, show=False),
        Binding("ctrl+g", "inspect_download", "Health", priority=True, show=False),
        Binding("ctrl+n", "manage_indexers", "Indexers", priority=True, show=False),
        # ctrl+o rather than the conventional ctrl+comma (terminals cannot
        # deliver it) or ctrl+t (reads as "new tab" to anyone in a terminal
        # multiplexer). ctrl+s is already Search.
        Binding("ctrl+o", "show_settings", "Settings", priority=True, show=False),
    ]

    def __init__(self, config: Config, setup_mode: bool = False, daemon=None):
        super().__init__()
        self.config = config
        self.setup_mode = setup_mode
        # aria2 main.py started and still owns. Read-only here — the settings
        # screen reports its uptime, binding and ownership — but shutdown stays
        # with main.py, which is the only thing that outlives the TUI.
        self.daemon = daemon
        self.setup_daemon = None  # aria2 the wizard started; main.py shuts it down
        self.jackett = JackettSearch(config.get("jackett"))
        self.aria2 = Aria2Client(config.get("aria2"))
        self.plex = PlexClient(config.get("plex"))
        self.vpn = VPNGuard(config.get("vpn"))
        self.security = SecurityScanner(config.get("security"))
        self.organize_store = OrganizeStore()
        self.search_results: list[TorrentResult] = []
        self.selected_indices: set[int] = set()
        self._scanned_gids: set[str] = set()  # Track downloads already scanned
        self._routed_gids: set[str] = set()  # Destination corrected in flight
        self._downloads_wide: bool | None = None  # Status column shown?
        self._downloads_budget: int | None = None  # pinned Name column width
        self._progress_bar = _PROGRESS_BAR         # shrinks on narrow windows
        self._stall_tracker: dict[str, int] = {}  # gid -> consecutive stall checks
        self._refresh_tick = 0  # paces the orphan-record sweep
        # Which indexers we last warned about, so a permanently broken one
        # does not toast on every search.
        self._reported_indexer_errors: tuple[str, ...] = ()

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

        # Before the workers start, so the working screen never flashes first.
        self.push_home_screen()
        if self.setup_mode:
            self.push_setup_screen()

        # Start background tasks
        self.check_vpn_status()
        self.check_clamav_status()
        self.refresh_downloads_loop()

        # Register VPN kill switch
        self.vpn.on_vpn_drop(self._on_vpn_drop)
        if self.config.get("vpn", "enabled"):
            self._run_kill_switch()

    def push_home_screen(self) -> None:
        """Open the landing screen, unless configured straight to work.

        A method of its own so tests that live on the working screen can
        neuter it the same way they neuter the mount-time workers.
        """
        if not self.config.get("display", "home", default=True):
            return
        home = HomeScreen()

        def _landed(query: str | None) -> None:
            if query:
                search = self.query_one("#search-input", Input)
                search.value = query
                search.focus()
                self.run_search(query)
            elif home.to_downloads:
                # Something is in the table — put the cursor where the news is.
                self.query_one("#downloads-table", DataTable).focus()
            else:
                self.query_one("#search-input", Input).focus()

        self.push_screen(home, _landed)

    def push_setup_screen(self) -> None:
        """Open the wizard above the home screen.

        The home screen sits beneath and greens up as the wizard installs
        and starts things — dismissing the wizard lands the user there.
        """
        setup = SetupScreen()

        def _done(_result: None) -> None:
            if setup.daemon is not None:
                self.setup_daemon = setup.daemon

        self.push_screen(setup, _done)

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

    def download_display_name(self, dl: DownloadStatus) -> str:
        """What to call a download in the table.

        aria2 reports the torrent's own folder name, and release folders lead
        with the tracker's stamp — `www.Site.org    -    Show.S02E03.2160p…`.
        Truncated to a column, a screenful of those is a column of identical
        prefixes, which is no name at all.

        The name chosen at add time is the one the user picked and the one the
        finished files actually carry, so it is the one worth showing. Falls
        back to aria2's name for an as-is download (no chosen name), one added
        outside the prompt, or a magnet whose metadata has not landed yet.
        """
        record = self.organize_store.by_gid(dl.gid)
        if record and record.name:
            return record.name
        return dl.name or dl.gid

    async def _update_downloads(self) -> None:
        """Fetch and display current download statuses."""
        try:
            active = await self.aria2.get_active()
            waiting = await self.aria2.get_waiting()
            # 25, not 5: every torrent's metadata parent also lands in the
            # stopped list, and a window that small can push a real
            # completion out of sight before it gets scanned.
            stopped = await self.aria2.get_stopped(count=25)

            # Finish downloads aria2 has already forgotten — a completion
            # whose result was cleared (Clear Done, a remove, the app closed
            # at the wrong moment) before the scan loop saw it. Runs on the
            # first tick and every ~2 minutes.
            self._refresh_tick += 1
            if self._refresh_tick % 60 == 1:
                try:
                    self.organize_store.reload()
                    for record in await find_orphan_records(
                        self.aria2, self.organize_store
                    ):
                        orphan = DownloadStatus(
                            gid=record.active_gid or record.gid,
                            status="complete",
                            name=record.wrapper,
                            dir=record.dir,
                        )
                        if orphan.gid not in self._scanned_gids:
                            self._scanned_gids.add(orphan.gid)
                            self._scan_completed_download(orphan)
                except Exception:
                    pass

            # Deselect junk on follow-up downloads whose metadata resolved.
            # Held back while the VPN is down, because the step ends in an
            # unpause that would undercut the kill switch. The interface
            # check is the cheap, synchronous half of the VPN check.
            try:
                self.organize_store.reload()
                if self.organize_store.pending() and (
                    not self.vpn.enabled or self.vpn.find_vpn_interface()
                ):
                    await apply_pending_selection(
                        self.aria2, self.organize_store, self.config,
                        notify=self.notify,
                    )
            except Exception:
                pass

            # Detect magnets stuck without metadata (30 checks at 2s ≈ 60s).
            # total_bytes stays 0 until metadata resolves, so this never
            # touches a download that is merely slow.
            for dl in active:
                if dl.total_bytes == 0 and dl.download_speed == 0:
                    self._stall_tracker[dl.gid] = self._stall_tracker.get(dl.gid, 0) + 1
                    if self._stall_tracker[dl.gid] == _STALL_WARN_TICKS:
                        self.notify(
                            f"Stalled: {self.download_display_name(dl)} "
                            "— no metadata after 60s",
                            severity="warning",
                        )
                    elif is_dead_magnet(dl, self._stall_tracker[dl.gid]):
                        try:
                            await self.aria2._call("forceRemove", [dl.gid])
                            self.notify(
                                f"Removed dead download: "
                                f"{self.download_display_name(dl)} "
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

            budget = self.download_name_budget()
            queued = active + waiting
            for dl in queued:
                status_color = {
                    "active": "green",
                    "waiting": "yellow",
                    "paused": "dim",
                    "error": "red",
                }.get(dl.status, "white")

                cells = [
                    [fit_name(self.download_display_name(dl), budget)],
                    [dl.size_human],
                    [self._make_progress(dl.progress)],
                    [_peer_cell(dl)],
                    [dl.speed_human if dl.status == "active" else "—"],
                    [dl.eta if dl.status == "active" else "—"],
                ]
                if self._downloads_wide:
                    cells.append([Text(dl.status, style=status_color)])
                table.add_row(
                    *self._ruled(cells), key=dl.gid, height=_DOWNLOAD_ROW_LINES
                )

            # Pad the fixed height so the rules reach the bottom. Without
            # these the grid stops at the last download and the reserved
            # space below reads as a rendering fault rather than as room.
            blank = [[""] for _ in cells] if queued else [
                [""] for _ in range(7 if self._downloads_wide else 6)
            ]
            for i in range(max(0, _DOWNLOAD_ROWS_VISIBLE - len(queued))):
                table.add_row(
                    *self._ruled(blank),
                    key=f"{_GHOST_PREFIX}{i}",
                    height=_DOWNLOAD_ROW_LINES,
                )
        except Exception:
            pass  # aria2 might not be running yet

    def download_name_budget(self) -> int:
        """Width left for the Name column, as the columns were actually built.

        Read back rather than recomputed: the Name column's width is pinned,
        so a row fitted to a different number would be padded or clipped by
        the table instead of by fit_name.
        """
        if self._downloads_budget is not None:
            return self._downloads_budget
        return download_layout(self.size.width, bool(self._downloads_wide))[0]

    @staticmethod
    def _ruled(cells: list[list]) -> list:
        """Interleave column rules between a row's cells, centring each."""
        out = []
        for i, lines in enumerate(cells):
            if i:
                out.append(rule_cell())
            out.append(centre_cell(lines))
        return out

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
            self.notify(
                f"{icon} {self.download_display_name(dl)[:30]} is {category}"
                " — filing there when done"
            )

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

    async def _finish_remapped(self, dl: DownloadStatus, record) -> bool:
        """Completion for a download whose files were written in place.

        Each payload file is scanned where it lies, then the wrapper folder
        the junk placeholders lived in is removed. Nothing moves, so this is
        the one filing path that does NOT stop seeding. Returns False when
        aria2 can no longer report the file list, sending the caller down
        the legacy locate-and-move path instead.
        """
        try:
            files = await self.aria2.get_files_detailed(dl.gid)
        except Exception:
            files = []
        paths = [Path(f["path"]) for f in files if f["selected"] and f["path"]]
        paths = [p for p in paths if p.exists()]
        if not paths:
            return False

        for path in paths:
            scan = await self.security.full_scan(path)
            if scan.threats or scan.blocked_files:
                self.notify(
                    f"⚠ THREAT: {path.name} — {scan.summary}", severity="error"
                )
                await self._release_from_aria2(dl)
                self.security.quarantine(path, scan)
                self.notify(f"Quarantined: {path.name}", severity="warning")
                self.organize_store.remove(record.gid)
                return True
            if not scan.clean:
                self.notify(
                    f"Scan warning: {path.name} — {scan.error}", severity="warning"
                )

        junk = effective_junk(record.category, configured_junk(self.config))
        note = " — still seeding"
        if dl.name and dl.name != dl.gid:
            wrapper = Path(dl.dir) / dl.name
            if not cleanup_wrapper(wrapper, junk):
                # Something real was left at its torrent path (a collision,
                # an unparseable episode name). Moving it is what ends
                # seeding — same trade as the legacy path.
                await self._release_from_aria2(dl)
                try:
                    for message in organize_download(
                        wrapper, Path(record.dir), record.name, junk
                    ):
                        self.notify(message, severity="warning")
                except OSError as e:
                    self.notify(f"Filing stragglers failed: {e}", severity="error")
                note = ""
        self.organize_store.remove(record.gid)
        self.notify(
            f"📁 Filed: {record.name} → {shorten(record.dir)}{note}",
            severity="information",
        )
        if self.config.get("plex", "enabled"):
            try:
                await self.plex.scan_for_category(record.category)
            except Exception:
                pass
        return True

    @work(group="security_scan")
    async def _scan_completed_download(self, dl: DownloadStatus) -> None:
        """Scan a completed download for threats, then file it by content."""
        from pathlib import Path

        try:
            self.organize_store.reload()
            record = self.organize_store.match(dl.gid, dl.dir)
        except Exception:
            record = None

        # Remapped downloads were written straight to their final names —
        # completion is a per-file scan plus wrapper cleanup, and the
        # torrent keeps seeding. Falls through to the legacy locate-and-move
        # flow only when aria2 can no longer say where the files are.
        if record is not None and record.remapped and record.name:
            if await self._finish_remapped(dl, record):
                return

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

        if not result.clean:
            return

        # File the download under the name chosen at add time. The plan wins
        # over content detection — the user picked this destination by hand.
        if record is not None:
            self.organize_store.remove(record.gid)
            if record.name:
                # Filing moves the files, so seeding has to end first —
                # the same trade quarantine and content re-routing make.
                await self._release_from_aria2(dl)
                junk = effective_junk(record.category, configured_junk(self.config))
                try:
                    messages = organize_download(
                        download_path, Path(record.dir), record.name, junk
                    )
                except OSError as e:
                    self.notify(f"Filing failed: {e}", severity="error")
                    return
                self.notify(
                    f"📁 Filed: {record.name} → {shorten(record.dir)}",
                    severity="information",
                )
                for message in messages:
                    self.notify(message, severity="warning")
                if self.config.get("plex", "enabled"):
                    try:
                        await self.plex.scan_for_category(record.category)
                    except Exception:
                        pass
                return

        # Safety net for anything the in-flight re-route couldn't settle —
        # a category only the finished files reveal. Normally this finds the
        # download already in the right place and does nothing, which is what
        # keeps seeding alive: the move below is what forces us to stop it.
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
        """A text progress bar in the theme's violet, on a recessive track."""
        width = self._progress_bar
        filled = int(width * percent / 100)
        bar = Text()
        bar.append("█" * filled, style=ACCENT)
        bar.append("░" * (width - filled), style=TRACK)
        bar.append(f" {percent:5.1f}%")
        return bar

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle search submission.

        Guarded by id: Submitted events bubble here from every screen's
        inputs — the organize prompt's name field, the home screen's search —
        not just the search box, and a handler on the screen does not stop
        them on its own.
        """
        if event.input.id != "search-input":
            return
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
            names = sorted(n for n, _ in self.jackett.last_errors)
            status += f" — {len(names)} indexer(s) skipped: {', '.join(names)}"
            # Only interrupt when the failing set changes. A permanently
            # broken indexer (Cloudflare, a dead tracker) otherwise throws
            # the same toast on every single search, which trains people to
            # ignore toasts. The info bar still carries it every time.
            signature = tuple(names)
            if signature != self._reported_indexer_errors:
                self._reported_indexer_errors = signature
                self.notify(
                    f"Skipping {', '.join(names)} — press ^n to test or "
                    "exclude indexers",
                    severity="warning",
                )
        else:
            self._reported_indexer_errors = ()
        info.update(status)

    def _sync_download_columns(self) -> DataTable:
        """Rebuild the columns when the window crosses _WIDE, or on first use.

        Status is the first thing to go on a narrow window: the progress bar
        and the Seeds colour already say whether a download is moving.

        Rebuilt rather than adjusted because the Name width is pinned too, and
        it is the one column whose width tracks the terminal.
        """
        table = self.query_one("#downloads-table", DataTable)
        wide = self.size.width >= _WIDE
        budget, bar = download_layout(self.size.width, wide)
        if (wide == self._downloads_wide and table.columns
                and (budget, bar) == (self._downloads_budget, self._progress_bar)):
            return table
        self._downloads_wide = wide
        self._downloads_budget = budget
        self._progress_bar = bar
        table.clear(columns=True)
        # Cells bring their own padding so a rule costs one column, not three.
        table.cell_padding = 0
        widths = dict(_DOWNLOAD_COL_WIDTHS, Name=budget,
                      Progress=bar + _PROGRESS_TEXT)
        columns = ["Name", "Size", "Progress", "Seeds", "Speed", "ETA"]
        if wide:
            columns.append("Status")
        pad = 2 * len(_DOWNLOAD_CELL_PAD)
        for i, name in enumerate(columns):
            if i:
                table.add_column(Text("│", style=RULE), width=1)
            table.add_column(
                Text(f"{_DOWNLOAD_CELL_PAD}{name}{_DOWNLOAD_CELL_PAD}"),
                width=widths[name] + pad,
            )
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

    def action_manage_indexers(self) -> None:
        self.push_screen(IndexersScreen())

    def action_show_settings(self) -> None:
        """Open settings against whichever daemon this session is actually using.

        self.daemon is the one main.py started and still owns; setup_daemon is
        the wizard's. Either way the screen only reads from it.
        """
        self.push_screen(SettingsScreen(self.config, self.daemon or self.setup_daemon))

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

        added, dest = await self._add_with_organize(result)
        if not added:
            return

        if dest.redirected:
            self.notify(
                f"Added: {result.title[:40]} — {dest.notice}", severity="warning"
            )
        else:
            self.notify(f"Added: {result.title[:50]}")

    async def _add_with_organize(
        self, result: TorrentResult
    ) -> tuple[bool, Destination | None]:
        """Resolve the destination, run the organize prompt, add to aria2.

        Returns (added, destination). Every add gets a plan record when the
        junk filter or a rename is in play — the record's GID is what the
        selection applier and the completion organizer key on. The prompt is
        skipped for redirected destinations: a download that isn't landing
        in the library shouldn't be filed as though it were.
        """
        url = result.download_url
        if not url:
            self.notify(f"No URL for: {result.title[:40]}", severity="warning")
            return False, None

        try:
            dest = resolve_destination(
                self.config, result.category, self.aria2.download_dir
            )
        except DestinationUnavailable as e:
            self.notify(f"Skipped {result.title[:40]}: {e}", severity="error")
            return False, None

        target_dir = dest.path
        plan_name = ""
        prompt = self.config.get("organize", "rename_prompt", default=True)
        if prompt and not dest.redirected:
            choice = await self.push_screen_wait(OrganizeScreen(result, dest.path))
            if choice is None:
                self.notify(f"Skipped: {result.title[:40]}")
                return False, None
            if choice.organize:
                plan_name = choice.name
                target_dir = choice.folder

        exclude_junk = self.config.get("organize", "exclude_junk", default=True)
        extra_options = None
        plan_gid = ""
        if exclude_junk or plan_name:
            plan_gid = new_plan_gid()
            extra_options = {"gid": plan_gid, "bt-remove-unselected-file": "true"}
            if exclude_junk:
                # The follow-up spawns paused so junk can be deselected
                # before any payload byte moves; the applier unpauses it.
                extra_options["pause-metadata"] = "true"

        try:
            if url.startswith("magnet:"):
                await self.aria2.add_magnet(
                    url, download_dir=target_dir, extra_options=extra_options
                )
            else:
                await self.aria2.add_torrent_url(
                    url, download_dir=target_dir, extra_options=extra_options
                )
        except Exception as e:
            self.notify(f"Failed: {result.title[:40]}: {e}", severity="error")
            return False, None

        if plan_gid:
            self.organize_store.add(OrganizeRecord(
                gid=plan_gid,
                dir=target_dir,
                category=result.category,
                name=plan_name,
                selection_done=not exclude_junk,
            ))
        if plan_name:
            self.notify(f"Will file as: {plan_name} → {shorten(target_dir)}")
        return True, dest

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
            ok, dest = await self._add_with_organize(result)
            if not ok:
                continue
            added += 1
            if dest.redirected:
                notices.add(dest.notice)
                redirected_cats.add(result.category)

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
        """Pause everything — or resume everything, if anything is paused.

        A toggle rather than a one-way pause. There was previously no binding
        anywhere that could resume, so a download paused by this key, or left
        paused by a reconnect that failed part-way, could not be restarted
        from the app at all.
        """
        try:
            # aria2 files paused downloads under tellWaiting, not tellActive.
            waiting = await self.aria2.get_waiting()
            paused = [d for d in waiting if d.status == "paused"]
        except Exception as e:
            self.notify(f"Pause failed: {e}", severity="error")
            return

        try:
            if paused:
                await self.aria2.unpause_all()
                self.notify(f"Resumed {len(paused)} download(s)")
            else:
                await self.aria2.pause_all()
                self.notify("All downloads paused — ^p again to resume")
        except Exception as e:
            self.notify(f"Failed: {e}", severity="error")

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
                # The cursor can sit on one of the blank rows that pad the
                # table to its fixed height. Those are spacing, not downloads.
                if gid and gid.startswith(_GHOST_PREFIX):
                    gid = None
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

    @work(group="reconnect")
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

        paused: list[str] = []
        stranded: list[str] = []
        try:
            for dl in active:
                try:
                    await self.aria2.pause(dl.gid)
                    paused.append(dl.gid)
                except Exception:
                    continue  # finished or vanished since we listed it
            # Give aria2 a moment to actually close the sockets; resuming
            # instantly can hand back the same peers we were trying to shed.
            await asyncio.sleep(0.75)
        finally:
            # Resume on every path, including an exception part-way through
            # the pauses. A download left paused is a far worse outcome than
            # a reconnect that achieved nothing — the first version returned
            # early on error and stranded exactly that way.
            for gid in paused:
                try:
                    await self.aria2.unpause(gid)
                except Exception:
                    stranded.append(gid)
            if stranded:
                try:
                    await self.aria2.unpause_all()  # blunt, idempotent, last resort
                    stranded.clear()
                except Exception:
                    pass

        if stranded:
            self.notify(
                f"{len(stranded)} download(s) left paused — press ^p to resume",
                severity="error",
            )
        else:
            self.notify(
                f"Reconnected {len(paused)} download(s) — new peers on the next announce"
            )


def run_tui(config: Config, setup: bool = False, daemon=None):
    """Launch the TUI app.

    ``daemon`` is the one _ensure_services started, passed in so the settings
    screen can report on it. Ownership does not transfer — the caller still
    shuts it down.

    Returns the aria2 daemon the setup wizard started, if any, so the CLI's
    shutdown path can own it the same way it owns _ensure_services' daemon.
    """
    app = TGetApp(config, setup_mode=setup, daemon=daemon)
    app.run()
    return app.setup_daemon
