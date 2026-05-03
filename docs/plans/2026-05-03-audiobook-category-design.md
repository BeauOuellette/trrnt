# Audiobook category + Jackett launcher — design

Date: 2026-05-03
Status: approved (Approach B)

## Goal

Add audiobook downloads as a first-class content type in `tget`:

1. New `audiobooks` category, with its own download folder (default `~/Media/Audiobooks`).
2. Detection of audiobooks at two points: title/Jackett-category at search time, and **file extension (`.m4b`) after the download completes**. Anything that contains M4B files lands in the audiobooks folder regardless of how the title was tagged.
3. A `tget jackett` command that opens Jackett's UI in the browser (starting it via `brew services` if needed) so the user can add audiobook indexers themselves.

The user explicitly does **not** want non-Jackett sources (LibriVox, direct HTTP scrapers, etc.) — Jackett remains the single search backend. The "more sources" in the request is satisfied by indexers added inside Jackett's own UI.

## Approach

Approved approach: **B — title detection at search + post-completion file-type correction.**

- Search-time detection routes most audiobook downloads to the right folder up front (cheap, no extra round-trip).
- Post-completion correction runs in the same hook that already triggers the security scan. If `.m4b` is present and the download didn't land in the audiobooks folder, move the folder there. This is the "based on file type" guarantee.
- Mid-download re-routing via aria2's `changeOption` was rejected as Approach C — it's racy and aria2 won't reliably move an in-flight download.

## Changes by file

### `config.example.yaml` and `src/torrentcli/config.example.yaml`
Add to the `categories` block:

```yaml
audiobooks:
  path: "~/Media/Audiobooks"
```

No Plex section mapping — audiobooks are transferred manually to an iPhone, not served by Plex. The audiobook category is intentionally excluded from the Plex auto-scan path.

### `src/torrentcli/search.py` — `_detect_category`
Add audiobook recognition:

- Jackett Torznab category `3030` (Audio > Audiobook) → `audiobooks`.
- Title regex: `\b(audiobook|m4b|abook|audible)\b` → `audiobooks`.
- Audiobook check runs **before** the music check, because audiobook releases sometimes contain MP3 files and would otherwise be miscategorized as `music`.

### `src/torrentcli/security.py` (or a new tiny `categorize.py`)
Add a pure helper:

```python
def is_audiobook_dir(path: Path) -> bool:
    """True if path contains any .m4b file (recursive)."""
```

Putting it in `security.py` is fine — it already walks completed download trees with `rglob`, so the dependency shape matches. No new module unless this grows.

### Post-completion hook — `src/torrentcli/main.py` (CLI watch) and `src/torrentcli/tui.py` (TUI)
After `scanner.full_scan` returns clean (i.e., download is not quarantined), insert:

```python
if is_audiobook_dir(download_path):
    audiobook_root = Path(config.get("categories", "audiobooks", "path")).expanduser()
    if audiobook_root not in download_path.parents:
        new_path = audiobook_root / download_path.name
        audiobook_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(download_path), str(new_path))
```

This runs once, only on a clean download, and only if the download isn't already inside the audiobooks folder. Quarantined downloads don't get re-routed (they shouldn't reach the media library at all). No Plex scan is triggered for audiobooks — the folder is treated as a manual handoff point for iPhone transfer.

### `src/torrentcli/main.py` — new `tget jackett` command
Behavior:

1. Read `jackett.url` from config (default `http://localhost:9117`).
2. `httpx.get(url, timeout=2)` — if it responds, skip step 3.
3. If unreachable, try `brew services start jackett` (only on macOS, only if `brew` is on PATH). Wait up to ~5s for it to come up.
4. `webbrowser.open(url)`.
5. Print status to console.

Errors print friendly messages, don't traceback. If `brew` isn't installed, print install hint and still try to open the URL (in case the user has Jackett running another way).

## Out of scope

- Non-Jackett audiobook sources (LibriVox, direct HTTP).
- A general "source provider" abstraction. We can add one later if a non-Jackett source ever becomes interesting; YAGNI for now.
- Renaming/reorganizing audiobook files inside the moved folder (e.g., per-author subfolders).
- Mid-download re-routing via aria2 (Approach C).
- Plex integration for audiobooks — explicitly out of scope; the folder is a manual iPhone-transfer handoff.
- Any iPhone sync automation (AirDrop, iTunes Finder sync, Books.app import). The user handles transfer manually.

## Testing

Manual verification (no automated test suite in this project today):

1. `tget config --init` → verify `audiobooks` appears under categories with the default path.
2. Search for a known audiobook with a tagged title (e.g. "Stephen King audiobook m4b") → `_detect_category` should return `audiobooks` and the download lands in `~/Media/Audiobooks`.
3. Search for an audiobook without "audiobook"/"m4b" in the title but with M4B contents → it lands in another folder first, then gets moved to `~/Media/Audiobooks` after completion.
4. `tget jackett` with Jackett running → browser opens to `:9117`.
5. `tget jackett` with Jackett stopped → service starts, browser opens.
6. A non-audiobook download (e.g., a movie) is **not** moved by the post-completion mover.
