# trrnt — Terminal Torrent Aggregator & Downloader

Search, select, and download torrents entirely from your terminal with VPN enforcement and Plex integration.

## Install

Homebrew, on macOS:

```bash
brew install BeauOuellette/tap/trrnt
```

Or, with no package manager of your own:

```bash
curl -fsSL https://raw.githubusercontent.com/BeauOuellette/trrnt/main/install.sh | sh
```

Or, if you already keep CLI tools in [uv](https://docs.astral.sh/uv/) or pipx:

```bash
uv tool install trrnt
```

All three put a `trrnt` command on your PATH, usable from any directory.

## Setup

```bash
trrnt
```

That is the setup. The first launch opens a wizard that installs what is
missing, starts it, and writes your config — there is no API key to copy out
of a web page, because Jackett mints one on its first start and the wizard
reads it from Jackett's own config file.

It will:

1. **Install aria2 and Jackett** via Homebrew, then start Jackett.
   Homebrew installs aria2 as a formula dependency, so that step is usually
   already done.
2. **Read Jackett's API key** and pick indexers that actually answer — the
   curated public list minus the ones sitting behind Cloudflare, which need
   FlareSolverr to work at all.
3. **Offer ClamAV** for scanning finished downloads. Optional, and a large
   install; declining is a supported state, not a degraded one.
4. **Write `~/.config/trrnt/config.yaml`.**

Every step is safe to re-run — quit half way and `trrnt setup` picks it back
up — and every automated step has a manual fallback.

You still need a **VPN** yourself. trrnt binds every aria2 socket to the
tunnel interface and refuses to start the download engine when no tunnel is
carrying traffic, but it does not install or manage one. On macOS anything
that creates a `utun` interface works (ProtonVPN, Mullvad, WireGuard).

**Plex** is optional. Set `plex.token` to have completed downloads trigger a
library scan; leave it blank and that step is skipped.

> Upgrading from `tget`? The command was renamed but `tget` still works as an
> alias, and an existing `~/.config/tget/` keeps being used — nothing to
> migrate. New installs get `~/.config/trrnt/`.

### Config values worth setting by hand

- `categories.movies.path` / `categories.tv.path` — your library directories
- `aria2.bt_interface` — pin your VPN interface (e.g. `utun4`) instead of
  letting trrnt detect it each launch
- `plex.token` — [finding an auth token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

## Usage

### Interactive TUI
```bash
trrnt
```

Launch always lands on the home screen: the logo, a quick-search box, and a
live health line (aria2 · VPN · ClamAV, plus the Jackett host it will search).
Enter runs the query on the working screen; **Esc goes there directly** —
onto the Downloads table if anything is running, the search box otherwise.
The health line shows the active count, so you can see there is something
waiting before you jump. Set `display.home: false` to boot straight to the
working screen every time.

It also checks once a day whether Homebrew has newer aria2 or Jackett builds
and offers `Ctrl+U` to apply them. Jackett cannot self-update under Homebrew
(its launcher passes `--NoUpdates`), and stale builds mean stale indexer
definitions, so searches quietly return less over time. `Ctrl+U` runs
`brew upgrade` **and** restarts the service — upgrading alone leaves the
running daemon on the old binary.

| Key      | Action                                        |
|----------|-----------------------------------------------|
| Enter    | Search (in the search box) / toggle row select |
| Ctrl+D   | Download selected                             |
| Ctrl+F   | Force reconnect (drop and re-peer downloads)  |
| Ctrl+X   | Clear finished downloads                      |
| Ctrl+R   | Remove download                               |
| Ctrl+Q   | Quit                                          |
| Ctrl+S   | Focus search bar                              |
| Ctrl+A   | Select all results                            |
| Ctrl+P   | Pause / resume all                            |
| Ctrl+E   | Inspect highlighted result                    |
| Ctrl+G   | Download health (live peers per download)     |
| Ctrl+O   | Settings — speed, seeding, port, encryption   |
| Ctrl+K   | Keys — the full list, in-app                  |
| Esc      | Leave home screen / close dialogs             |

The footer shows the five most-used; `Ctrl+K` lists every binding and is
the source of truth — it is built from the app's own key map.

Ctrl+D opens a naming prompt before the download starts: the release title
is parsed into a clean name (`www.Site.com.The.Agency.S02E03.2160p…` →
`The Agency S02E03`) and a destination matching the library layout
(`TV/The Agency/Season 2`; movies land flat in `Movies/`). Both fields are
editable and the folder follows the name as you type — Enter accepts,
"As-is" downloads unrenamed, Esc skips the torrent. Junk files inside the
torrent (.nfo, .txt, samples) are deselected in aria2 before any data moves,
and a clean finished download is filed under the chosen name — season packs
get per-episode names from their own SxxEyy tags. Tune it under `organize:`
in config.yaml (`rename_prompt`, `exclude_junk`, `junk_extensions`).

That chosen name is also what the Downloads table shows, rather than the
torrent's own folder. Release folders all lead with the tracker's stamp
(`www.Site.org  -  Show.S02E03.2160p…`), so a column of them truncates to
identical prefixes and you cannot tell one row from another. Downloads added
as-is, or outside the prompt, still show the release name.

### Settings (Ctrl+O)

Speed limits, seeding, listen port and peer encryption, saved back to
config.yaml with its comments intact. The header reports the aria2 daemon
itself: whether it is answering, which interface it is bound to, how long it
has been up, and whether quitting trrnt will stop it.

Settings are grouped by when they actually take effect, because aria2 returns
`OK` for all of them regardless:

| Group | Fields | Reality |
|-------|--------|---------|
| Speed | download/upload cap, max concurrent | Live on the running daemon |
| Seeding & encryption | ratio, time, encryption | Newly added downloads only; queued torrents keep theirs |
| Network | listen port, local peer discovery | Fixed at spawn — needs a relaunch |

Restart-tier options are deliberately never pushed to a running daemon: aria2
would answer `OK`, change nothing, and the UI would report success for a
setting that never took.

Two aria2 semantics worth knowing, since guessing gets both backwards:
`seed_ratio: 0` seeds **forever**, and `seed_time: 0` is what means **never
seed** (blank means no time limit). The screen spells out the current pair in
plain English as you type.

Local peer discovery now defaults **off**. It announces your torrents to
everyone on your LAN, which is outside the VPN tunnel by design — aria2's own
default is off, and trrnt previously forced it on.

### CLI Mode
```bash
# Search
trrnt search "The Bear S03"
trrnt search "Oppenheimer 2160p" --limit 10 --sort size

# Search + interactive download
trrnt search "Big Lebowski" -d

# Add magnet directly
trrnt add "magnet:?xt=urn:btih:..."

# Download status
trrnt status
trrnt status --watch      # live refresh

# Pause / resume
trrnt pause
trrnt resume

# VPN check
trrnt vpn

# Plex
trrnt plex libraries      # list sections
trrnt plex scan           # scan all
trrnt plex scan -s 1      # scan specific section

# Config check (with connection tests)
trrnt config

# Security scanning
trrnt scan ~/Downloads/some-torrent    # manual scan
trrnt quarantine list                  # list quarantined items
trrnt quarantine release <name> ~/Media/Movies  # release false positive
trrnt quarantine log                   # view scan log
```

## VPN Enforcement

trrnt enforces VPN at three levels:

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

When downloads are added, trrnt automatically triggers a Plex library scan for the matching content category (movies/tv). This means new content appears in Plex shortly after download completes.

## Security

Every completed download goes through a 4-stage security pipeline before it reaches your media library:

1. **File type enforcement** — Blocks executables, scripts, and other dangerous file types (.exe, .bat, .ps1, .dll, .scr, etc.) that have no business being in a media download
2. **Size verification** — Flags downloads where actual size differs >5% from expected (catches malware-stuffed repackages)
3. **Password-protected archive detection** — Flags encrypted .zip/.rar/.7z files (common malware evasion tactic)
4. **ClamAV virus scan** — Full recursive scan with `clamdscan` (fast daemon mode) or falls back to `clamscan`

If any stage flags a threat, the download is automatically moved to quarantine (`~/Downloads/quarantine`) with a detailed log entry. You can review and release false positives with `trrnt quarantine release`.

The TUI status bar shows ClamAV health in real-time. `trrnt config` includes ClamAV in its connection checks.

### Managing indexers (Ctrl+N)

`Ctrl+N` opens the indexer manager at any time — not just during setup. It
lists everything configured in Jackett, `Ctrl+T` tests them all and marks
each one green (responding), amber (behind Cloudflare) or red (failing), and
space toggles whether trrnt searches it. Toggling writes
`jackett.exclude_indexers` and takes effect on the next search, so a tracker
that errors on every query stops costing you anything. `Ctrl+R` deletes an
indexer from Jackett outright.

Excluding is a blocklist, not an allowlist: indexers you add to Jackett
later are picked up automatically without editing config.

### Indexers and Cloudflare

The setup wizard's quick-pick pre-selects popular public indexers, then
**tests each one it added** and tells you which actually answer. Some
trackers (1337x, EZTV, KickassTorrents among them) sit behind Cloudflare:
Jackett cannot reach them on its own, because solving that challenge needs a
real browser. This is not a Jackett version problem and upgrading will not
fix it.

trrnt can run that browser for you. The wizard offers a **Cloudflare solver**
step: a private [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)
with its own bundled copy of Chrome for Testing (~190MB, one download). It is
never your browser, nothing appears on screen, and it runs only for the few
seconds a challenge takes before shutting down again.

Jackett stores the resulting clearance cookie per indexer and replays it, so
solving happens on repair rather than on every search — a search works with
the solver stopped entirely. Cookies do expire, though, typically inside half
an hour. When one does, that indexer is skipped and the toast says so:

```
Skipping 1337x — press ^y to repair
```

**^y** mints fresh cookies for whatever went quiet, puts any indexer you had
switched off back into the search once it answers, and widens the per-indexer
timeout if it is too tight for a Cloudflare-backed tracker to reply. The other
indexers carry the search while it works, so nothing blocks on it.

Setting `solver.enabled: false` turns the whole thing off; the gated indexers
then simply stay quiet, exactly as before.

## Testing the setup wizard

`scripts/sandbox-setup.sh` runs the first-run wizard against a throwaway
environment — a redirected `HOME` plus a second Jackett on its own port and
data folder — so nothing touches your real config, your real Jackett, or a
running aria2 (which is adopted, never owned).

```bash
scripts/sandbox-setup.sh --reset
```

`--reset` returns it to first-run state; `--clean` removes it entirely.

## Architecture

```
┌─────────────┐     ┌───────────┐     ┌─────────────┐
│  trrnt CLI  │────▶│  Jackett  │────▶│  Indexers   │
│  or TUI     │     │  Torznab  │     │ (50+ sites) │
└──────┬──────┘     └───────────┘     └─────────────┘
       │
       │  magnet/torrent
       ▼
┌──────────────┐     ┌───────────┐
│  VPN Guard   │────▶│  aria2c   │────▶ Downloads
│  kill switch │     │  RPC      │
└──────────────┘     └──────┬────┘
                            │ on complete
                            ▼
                     ┌──────────┐
                     │  Plex    │────▶ Library scan
                     │  API     │
                     └──────────┘
```

## Project Structure

```
src/trrnt/
├── main.py        # CLI entry point (click)
├── tui.py         # Interactive TUI (textual)
├── onboard.py     # First-run wizard: detection, Jackett's admin API, config
├── search.py      # Jackett/Torznab search
├── download.py    # aria2 JSON-RPC client
├── daemon.py      # aria2c lifecycle: start, adopt, guaranteed shutdown
├── vpn.py         # VPN guard + kill switch
├── plex.py        # Plex Media Server client
├── security.py    # ClamAV scanning, file enforcement, quarantine
├── naming.py      # Release name → clean name + destination
├── organize.py    # Junk pruning before download, filing on completion
├── storage.py     # Destination resolution across external volumes
├── settings.py    # Tunable aria2 options + when each actually applies
├── config.py      # YAML config loader
├── paths.py       # Config/state locations, with the tget fallback
└── branding.py    # The mask, the splash, the palette
```

Packaging and release plumbing:

```
install.sh                     # curl-able bootstrap (uv or pipx, no sudo)
scripts/brew_formula.py        # generates the Homebrew formula, resources and all
.github/workflows/release.yml  # tag → build → release → PyPI + tap
```

The formula is generated, never hand-written: Homebrew pins every transitive
Python dependency by URL and sha256, and that list is only correct for one
exact build. `scripts/brew_formula.py` resolves it with `uv pip compile` — the
same resolver the project is developed against — hashes the sdist that was
just built, and CI pushes the result to the tap. It is not committed here,
because a checked-in copy would carry a sha256 that is wrong the moment
anything in `src/` changes.

## Phase 2 Roadmap

- [ ] First-run onboarding wizard in the TUI — detect/brew-install aria2 +
      Jackett, start the services, auto-read the Jackett API key from
      ServerConfig.json, write config.yaml, verify connections; public-indexer
      quick-pick, with "open Jackett UI" fallback for private trackers
- [ ] Config file quality preferences (auto-filter for x265/2160p)
- [ ] Duplicate detection against Plex library
- [ ] Download completion → auto Plex-friendly rename
- [ ] RSS watchlist mode with cron scheduling
- [ ] Download history log
- [ ] Per-indexer health checks and priority
- [ ] macOS notifications on completion
