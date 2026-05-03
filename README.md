# tget — Terminal Torrent Aggregator & Downloader

Search, select, and download torrents entirely from your terminal with VPN enforcement and Plex integration.

## Architecture

```
┌─────────────┐     ┌──────────┐     ┌────────────┐
│  tget CLI   │────▶│  Jackett  │────▶│  Indexers   │
│  or TUI     │     │  Torznab  │     │ (50+ sites) │
└──────┬──────┘     └──────────┘     └────────────┘
       │
       │  magnet/torrent
       ▼
┌──────────────┐     ┌──────────┐
│  VPN Guard   │────▶│  aria2c   │────▶ Downloads
│  kill switch │     │  RPC      │
└──────────────┘     └──────┬───┘
                            │ on complete
                            ▼
                     ┌──────────┐
                     │  Plex    │────▶ Library scan
                     │  API     │
                     └──────────┘
```

## Prerequisites

1. **Jackett** — torrent indexer aggregator
   ```bash
   # macOS
   brew install jackett
   # Or Docker
   docker run -d --name jackett -p 9117:9117 linuxserver/jackett
   ```

2. **aria2** — download engine
   ```bash
   brew install aria2
   # Start with RPC enabled:
   aria2c --enable-rpc --rpc-listen-all=false --rpc-allow-origin-all \
          --seed-ratio=2.0 --bt-enable-lpd=true --enable-dht=true
   ```

3. **ProtonVPN** (or any VPN that creates a `utun` interface on macOS)

4. **ClamAV** — virus scanning
   ```bash
   brew install clamav
   # Initialize virus database
   sudo freshclam
   # Start daemon for fast scanning
   clamd
   ```

5. **Plex Media Server** (optional) — for auto library scanning

## Install

```bash
cd torrent-cli
pip install -e .
```

## Setup

```bash
# Create config file
tget config --init

# Edit with your credentials
$EDITOR ~/.config/tget/config.yaml
```

### Required config values:
- `jackett.api_key` — Find in Jackett UI at http://localhost:9117
- `plex.token` — Get from https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

### Recommended config:
- `vpn.interface_prefix` — `utun` for macOS ProtonVPN
- `categories.movies.path` / `categories.tv.path` — Your Plex library directories
- `aria2.bt_interface` — Set to your VPN interface (e.g., `utun4`) for interface-level binding

## Usage

### Interactive TUI
```bash
tget
```

| Key      | Action              |
|----------|---------------------|
| Ctrl+S   | Focus search bar    |
| Enter    | Search / select row |
| Ctrl+A   | Select all results  |
| Ctrl+D   | Download selected   |
| Ctrl+P   | Pause all downloads |
| Ctrl+R   | Refresh status      |
| Ctrl+Q   | Quit                |

### CLI Mode
```bash
# Search
tget search "The Bear S03"
tget search "Oppenheimer 2160p" --limit 10 --sort size

# Search + interactive download
tget search "Big Lebowski" -d

# Add magnet directly
tget add "magnet:?xt=urn:btih:..."

# Download status
tget status
tget status --watch      # live refresh

# Pause / resume
tget pause
tget resume

# VPN check
tget vpn

# Plex
tget plex libraries      # list sections
tget plex scan           # scan all
tget plex scan -s 1      # scan specific section

# Config check (with connection tests)
tget config

# Security scanning
tget scan ~/Downloads/some-torrent    # manual scan
tget quarantine list                  # list quarantined items
tget quarantine release <name> ~/Media/Movies  # release false positive
tget quarantine log                   # view scan log
```

## VPN Enforcement

tget enforces VPN at three levels:

1. **Interface check** — Verifies a `utun*` tunnel interface exists and is UP
2. **IP verification** — Confirms your external IP differs from your real IP
3. **Kill switch** — Background task polls every 5s; if VPN drops, all downloads pause instantly

The kill switch runs automatically in TUI mode. In CLI mode, each command does a one-shot VPN check before executing.

### aria2 interface binding (recommended)

For belt-and-suspenders protection, bind aria2 to your VPN interface:

```bash
# Find your VPN interface while connected
ifconfig | grep utun

# Start aria2 bound to that interface
aria2c --enable-rpc --interface=utun4
```

Or set `aria2.bt_interface: utun4` in your config.

## Plex Integration

When downloads are added, tget automatically triggers a Plex library scan for the matching content category (movies/tv). This means new content appears in Plex shortly after download completes.

## Security

Every completed download goes through a 4-stage security pipeline before it reaches your media library:

1. **File type enforcement** — Blocks executables, scripts, and other dangerous file types (.exe, .bat, .ps1, .dll, .scr, etc.) that have no business being in a media download
2. **Size verification** — Flags downloads where actual size differs >5% from expected (catches malware-stuffed repackages)
3. **Password-protected archive detection** — Flags encrypted .zip/.rar/.7z files (common malware evasion tactic)
4. **ClamAV virus scan** — Full recursive scan with `clamdscan` (fast daemon mode) or falls back to `clamscan`

If any stage flags a threat, the download is automatically moved to quarantine (`~/Downloads/quarantine`) with a detailed log entry. You can review and release false positives with `tget quarantine release`.

The TUI status bar shows ClamAV health in real-time. `tget config` includes ClamAV in its connection checks.

## Project Structure

```
src/torrentcli/
├── main.py        # CLI entry point (click)
├── tui.py         # Interactive TUI (textual)
├── search.py      # Jackett/Torznab search
├── download.py    # aria2 JSON-RPC client
├── vpn.py         # VPN guard + kill switch
├── plex.py        # Plex Media Server client
├── security.py    # ClamAV scanning, file enforcement, quarantine
└── config.py      # YAML config loader
```

## Phase 2 Roadmap

- [ ] Config file quality preferences (auto-filter for x265/2160p)
- [ ] Duplicate detection against Plex library
- [ ] Download completion → auto Plex-friendly rename
- [ ] RSS watchlist mode with cron scheduling
- [ ] Download history log
- [ ] Per-indexer health checks and priority
- [ ] macOS notifications on completion
