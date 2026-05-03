# Claude Code Kickoff Prompt for tget

Copy and paste everything below the line into Claude Code.

---

I have a scaffolded Python project in this directory called `tget` — a terminal torrent aggregator and downloader. I need you to take this from scaffolding to fully working software. Use ultrathink for complex debugging. Use your agentic capabilities fully — run code, test things, read error output, iterate, and fix. Don't ask me what to do when you hit a bug — figure it out and fix it. Be autonomous.

## Architecture

```
src/torrentcli/
├── main.py        # Click CLI entry point — `tget` launches TUI, subcommands for CLI mode
├── tui.py         # Textual interactive TUI — search, select, download, live status
├── search.py      # Jackett/Torznab XML search aggregation
├── download.py    # aria2 JSON-RPC client — add/pause/resume/status
├── vpn.py         # VPN enforcement — utun interface detection, IP verification, kill switch daemon
├── plex.py        # Plex auto-scan on download completion
├── security.py    # ClamAV virus scanning, file type enforcement, size verification, quarantine
├── config.py      # YAML config loader with deep merge defaults
```

The config lives at `~/.config/tget/config.yaml`. Example at `config.example.yaml` in project root.

## What I need you to do

Work through this in order. Each phase should be fully working before moving to the next. Run tests at every step — actually execute the code, check the output, fix errors.

### Phase 1: Foundation
1. `pip install -e .` and verify the package installs clean
2. Run `tget config --init` and verify it creates the config file
3. Verify config loading — defaults, deep merge, path expansion all work
4. Run `tget config` and check the connection tests fire (they'll fail without services running — that's fine, verify the error handling is graceful)

### Phase 2: VPN Guard
1. Test `tget vpn` — verify it detects interfaces correctly on this machine
2. Verify the IP check works (hit ipinfo.io, compare against real_ip if set)
3. Test the kill switch loop — spin it up, verify it polls and fires callbacks
4. Make sure VPN gate blocks search/download commands when VPN is down

### Phase 3: Search
1. Test Jackett connection check against localhost:9117
2. Run a real search — `tget search "test"` — and verify XML parsing works end to end
3. Verify quality filters exclude CAM/TS results
4. Verify category detection (S01E01 → tv, FLAC → music, etc.)
5. Test the interactive download selection flow (`tget search "test" -d`)

### Phase 4: Downloads
1. Start aria2 with `aria2c --enable-rpc` and test connection
2. Add a magnet via `tget add "magnet:..."` and verify it appears in aria2
3. Test `tget status` and `tget status --watch` — verify live refresh works
4. Test pause/resume commands
5. Verify category-based download directory routing (movies → ~/Media/Movies, etc.)

### Phase 5: Security Pipeline
This is critical — every completed download must be scanned before touching the media library.

1. Check if ClamAV is installed (`clamscan --version`). If not, tell me to install it but continue with the other checks.
2. Test file type enforcement — create a test directory with a mix of .mkv, .exe, .bat, .srt files. Run `tget scan` on it. Verify it flags the .exe and .bat.
3. Test size verification — verify the tolerance check math works
4. Test the full scan pipeline — `tget scan ~/some/path` should run all 4 stages and print results
5. Test quarantine — verify flagged files move to quarantine dir with metadata logged
6. Test quarantine management — `tget quarantine list`, `tget quarantine log`, `tget quarantine release`
7. Wire up scan-on-complete in the download monitor — when aria2 reports a download as complete, the security scanner should auto-run and quarantine if flagged

### Phase 6: Plex Integration
1. Test Plex connection check
2. Test `tget plex libraries` — list sections
3. Test `tget plex scan` — trigger library refresh
4. Verify auto-scan fires after download is added (category → section mapping)

### Phase 7: TUI
1. Launch `tget` (no subcommand) and verify the Textual app starts clean
2. Verify the status bar shows VPN status, download count, speed, and ClamAV health
3. Test search from the TUI — type a query, hit Enter, verify results populate
4. Test row selection (Enter to toggle ✓), Ctrl+A for select all
5. Test Ctrl+D to download selected
6. Verify download progress table updates in real-time
7. Verify kill switch is running in TUI background
8. Verify scan-on-complete fires in TUI mode and shows notifications

### Phase 8: Hardening
1. Test graceful error handling everywhere — what happens when Jackett is down? aria2 not running? Plex unreachable? ClamAV not installed?
2. Make sure no command crashes with a traceback — all errors should be caught and printed with rich formatting
3. Verify VPN gate can't be bypassed — search and download should both refuse without VPN
4. Test the kill switch under failure — simulate VPN drop (disable interface) and verify downloads pause
5. Edge cases: empty search results, malformed magnet links, zero-byte downloads, unicode filenames

## Technical Notes

- Python 3.10+, all async where possible (httpx for HTTP, asyncio subprocesses for ClamAV)
- `click` for CLI, `textual` for TUI, `rich` for formatting
- aria2 JSON-RPC — standard `aria2.methodName` calls with optional `token:SECRET` auth
- Jackett Torznab API — returns XML, parse with `xmltodict`
- Plex API — simple HTTP GET with `X-Plex-Token` header
- ClamAV — shell out to `clamdscan` (fast, daemon) with fallback to `clamscan` (slow, standalone)
- VPN detection — `ifconfig` on macOS looking for `utunN` interfaces with UP flag
- Config — YAML with deep merge against defaults, `~` expansion on all paths

## My setup
- macOS
- ProtonVPN (creates utun interfaces)
- Jackett at localhost:9117
- Plex at localhost:32400
- aria2 installed via Homebrew

Go.
