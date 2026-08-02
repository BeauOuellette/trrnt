# Audiobook Category Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an `audiobooks` category to `tget` that routes downloads containing M4B files to a dedicated folder (`~/Media/Audiobooks` by default), and add a `tget jackett` command that opens Jackett's UI so the user can add audiobook indexers there.

**Architecture:** Two-phase detection (Approach B from the design doc). Phase 1: at search time, `_detect_category` recognizes Jackett category `3030` and audiobook keywords in titles to pre-route downloads. Phase 2: in the existing post-completion hook (where ClamAV scans run), if `.m4b` files are present in the finished download and it didn't land in the audiobooks folder, `shutil.move` the folder to the audiobooks path. This piggybacks on the existing security pipeline and mirrors how quarantine moves work, so seeding behavior is consistent.

**Tech Stack:** Python 3.10+, existing deps unchanged. **No test framework added** — this project's verification workflow is manual smoke testing via the `tget` command, and the plan matches that.

**Reference design:** `docs/plans/2026-05-03-audiobook-category-design.md`.

---

## Pre-flight

Before starting, read these files end-to-end so context isn't lost:

- `docs/plans/2026-05-03-audiobook-category-design.md` — the approved design.
- `src/torrentcli/search.py` — current `_detect_category` lives at line 54.
- `src/torrentcli/security.py` — where `is_audiobook_dir` will live.
- `src/torrentcli/main.py` — post-completion hook around line 381; CLI command group structure (`@cli.command(...)`).
- `src/torrentcli/tui.py` — post-completion hook in `_scan_completed_download` around line 413.
- `config.example.yaml` (root) and `src/torrentcli/config.example.yaml` — both must stay in sync.

---

## Task 1: Extend `_detect_category` to recognize audiobooks

**Goal:** Update `_detect_category` in `search.py` so that:
1. Jackett category `3030` (Torznab "Audio > Audiobook") returns `"audiobooks"`.
2. Titles matching `\b(audiobook|m4b|abook|audible)\b` (case-insensitive) return `"audiobooks"`.
3. Audiobook check fires **before** the generic 3000s → music mapping and before the music-keyword fallback. This matters because category `3030` falls inside the 3000s "music" range and audiobook releases sometimes ship as MP3.
4. All existing categorizations (movies/tv/music) continue to work unchanged.

**Files:**
- Modify: `src/torrentcli/search.py:54-84` (the `_detect_category` function)

**Step 1: Replace the entire function body** (currently lines 54–84) with:

```python
def _detect_category(title: str, jackett_cats: list[str] | None = None) -> str:
    """Guess content category from title and Jackett category IDs."""
    title_lower = title.lower()

    # Audiobook signals take precedence — they overlap with the music range
    # (Jackett 3030 sits inside 3000s) and audiobook releases can ship as MP3.
    if jackett_cats:
        for cat in jackett_cats:
            try:
                if int(cat) == 3030:
                    return "audiobooks"
            except ValueError:
                pass
    if re.search(r"\b(audiobook|m4b|abook|audible)\b", title_lower):
        return "audiobooks"

    # Jackett category ranges: 2000s = Movies, 5000s = TV, 3000s = Audio
    if jackett_cats:
        for cat in jackett_cats:
            try:
                num = int(cat)
                if 2000 <= num < 3000:
                    return "movies"
                elif 5000 <= num < 6000:
                    return "tv"
                elif 3000 <= num < 4000:
                    return "music"
            except ValueError:
                pass

    # Fallback: title pattern matching
    tv_patterns = [
        r"s\d{2}e\d{2}", r"season.\d+", r"complete.series",
        r"\d{1,2}x\d{2}", r"episode.\d+",
    ]
    for pat in tv_patterns:
        if re.search(pat, title_lower):
            return "tv"

    if any(kw in title_lower for kw in ["flac", "mp3", "album", "discography"]):
        return "music"

    return "movies"  # default assumption for video content
```

**Step 2: Smoke-check the function from a Python REPL.**

Run:
```bash
python -c "
from torrentcli.search import _detect_category
checks = [
    ('Anything', ['3030'], 'audiobooks'),
    ('Stephen King It Audiobook 2017', None, 'audiobooks'),
    ('Some Book [M4B] Unabridged', None, 'audiobooks'),
    ('Author - Title (Audible)', None, 'audiobooks'),
    ('Some Book [abook]', None, 'audiobooks'),
    ('Audio Stuff FLAC', ['3030'], 'audiobooks'),
    ('Some Title Audiobook MP3', None, 'audiobooks'),
    ('Audibly Reviewed Album FLAC', None, 'music'),
    ('Some.Movie.2024.1080p', ['2040'], 'movies'),
    ('Show.Name.S03E07.1080p', None, 'tv'),
    ('Some Album FLAC', ['3010'], 'music'),
    ('Mystery Item 2024', None, 'movies'),
]
for title, cats, expected in checks:
    actual = _detect_category(title, cats)
    status = 'OK' if actual == expected else 'FAIL'
    print(f'{status}: {title!r} cats={cats} -> {actual} (expected {expected})')
"
```

Expected: every line prints `OK`. If any `FAIL`s, fix the regex/order before continuing.

**Step 3: Commit.**

```bash
git add src/torrentcli/search.py
git commit -m "Detect audiobook category from Jackett cat 3030 and title keywords"
```

---

## Task 2: Add `is_audiobook_dir` helper

**Goal:** Add a pure filesystem helper to `security.py` that returns `True` if a path is or contains any `.m4b` file (recursive). Used by the post-completion mover to decide whether to relocate.

**Files:**
- Modify: `src/torrentcli/security.py` (add a module-level function near the top, after `DEFAULT_BLOCKED_EXTENSIONS`)

**Step 1: Insert the helper** after the `DEFAULT_BLOCKED_EXTENSIONS` set (around line 53), before the `class SecurityScanner` line:

```python
def is_audiobook_dir(path: str | Path) -> bool:
    """True if path is an .m4b file or contains any .m4b file (recursive).

    M4B presence is the canonical signal for an audiobook download — it's
    the format Apple/Audible use, and bare .mp3 audiobooks are too
    ambiguous (could be music) to re-route automatically.
    """
    path = Path(path)
    if not path.exists():
        return False
    if path.is_file():
        return path.suffix.lower() == ".m4b"
    for entry in path.rglob("*"):
        if entry.is_file() and entry.suffix.lower() == ".m4b":
            return True
    return False
```

**Step 2: Smoke-check from a Python REPL.**

Run:
```bash
python -c "
import tempfile
from pathlib import Path
from torrentcli.security import is_audiobook_dir

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    print('empty dir →', is_audiobook_dir(tmp))            # False
    (tmp / 'movie.mkv').write_bytes(b'')
    print('movie only →', is_audiobook_dir(tmp))           # False
    nested = tmp / 'Author' / 'Title'
    nested.mkdir(parents=True)
    (nested / 'CH01.M4B').write_bytes(b'')
    print('nested M4B →', is_audiobook_dir(tmp))           # True
    print('single file →', is_audiobook_dir(nested / 'CH01.M4B'))  # True
print('nonexistent →', is_audiobook_dir(Path('/no/such/path')))    # False
"
```

Expected output:
```
empty dir → False
movie only → False
nested M4B → True
single file → True
nonexistent → False
```

**Step 3: Commit.**

```bash
git add src/torrentcli/security.py
git commit -m "Add is_audiobook_dir helper for post-completion routing"
```

---

## Task 3: Add `audiobooks` to both config example files

**Files:**
- Modify: `config.example.yaml`
- Modify: `src/torrentcli/config.example.yaml`

The two files must stay in sync — `pyproject.toml` ships the package copy as `package-data` for `tget config --init`, while the root copy is the visible reference.

**Step 1: Edit `config.example.yaml` (root).** Inside the `categories:` block, add the new entry between `music:` and `other:`:

```yaml
  music:
    path: "~/Media/Music"
  audiobooks:
    path: "~/Media/Audiobooks"
  other:
    path: "~/Downloads/torrents"
```

**Step 2: Edit `src/torrentcli/config.example.yaml`.** Apply the identical change.

**Step 3: Verify the two files are in sync.**

Run: `diff config.example.yaml src/torrentcli/config.example.yaml`
Expected: no output (files identical).

**Step 4: Sanity-check `tget config --init`.**

Run:
```bash
mv ~/.config/tget/config.yaml ~/.config/tget/config.yaml.bak 2>/dev/null || true
tget config --init
grep -A1 "audiobooks:" ~/.config/tget/config.yaml
mv ~/.config/tget/config.yaml.bak ~/.config/tget/config.yaml 2>/dev/null || true
```
Expected: `audiobooks:` and `path: "~/Media/Audiobooks"` appear in the grep output.

**Step 5: Commit.**

```bash
git add config.example.yaml src/torrentcli/config.example.yaml
git commit -m "Add audiobooks category to config example"
```

---

## Task 4: Wire post-completion audiobook re-router in CLI watch loop

**Goal:** In `main.py`'s `status --watch` loop, after the security scan returns clean for a completed download, check for `.m4b` files. If present and the download isn't already inside the configured audiobooks folder, move it there.

**Files:**
- Modify: `src/torrentcli/main.py` (around line 381 — the post-completion hook inside `status`)

**Step 1: Read the current hook in context.**

Open `src/torrentcli/main.py` and locate the block around line 381 that starts with `console.print(f"\n[bold]Scanning completed download:[/] ...`. Note the surrounding variables: `dl` (DownloadStatus), `download_path` (Path), `scan_result`, `scanner`, and the surrounding indentation.

**Step 2: Update imports.** Run `grep -n "^import shutil\|^from .security" src/torrentcli/main.py`. If `shutil` isn't imported, add `import shutil` to the stdlib import block near the top. If there's an existing `from .security import ...` line, extend it to include `is_audiobook_dir`; otherwise add `from .security import is_audiobook_dir` near the other relative imports.

**Step 3: Add the re-router** immediately after the existing quarantine block (right after the `scanner.quarantine(download_path, scan_result)` line and its conditional). Insert (matching the surrounding indentation level):

```python
                        # Re-route audiobooks based on file content. A clean
                        # download containing .m4b files belongs in the
                        # audiobooks folder regardless of how its title was
                        # tagged at search time.
                        if scan_result.clean and is_audiobook_dir(download_path):
                            ab_root = Path(
                                config.get("categories", "audiobooks", "path")
                            ).expanduser()
                            if ab_root not in download_path.parents:
                                ab_root.mkdir(parents=True, exist_ok=True)
                                new_path = ab_root / download_path.name
                                if new_path.exists():
                                    console.print(
                                        f"[yellow]⚠ Audiobook target already exists, "
                                        f"leaving in place:[/] {new_path}"
                                    )
                                else:
                                    shutil.move(str(download_path), str(new_path))
                                    console.print(
                                        f"[green]🎧 Routed audiobook → {new_path}[/]"
                                    )
```

**Step 4: Verify it parses.**

Run: `python -c "import torrentcli.main"`
Expected: no errors.

**Step 5: Smoke-test the routing on a synthetic completed download.**

```bash
python -c "
import asyncio, shutil, tempfile
from pathlib import Path
from torrentcli.config import Config
from torrentcli.security import is_audiobook_dir

config = Config.load()
with tempfile.TemporaryDirectory() as tmp:
    fake_dl = Path(tmp) / 'Some Untagged Audiobook'
    fake_dl.mkdir()
    (fake_dl / 'ch01.m4b').write_bytes(b'')
    print('detected as audiobook?', is_audiobook_dir(fake_dl))
    ab_root = Path(config.get('categories', 'audiobooks', 'path')).expanduser()
    print('audiobook root:', ab_root)
"
```

Expected: prints `detected as audiobook? True` and a sensible path for the audiobook root. (Doesn't actually move anything — just verifies detection + config wiring outside the watch loop.)

**Step 6: Commit.**

```bash
git add src/torrentcli/main.py
git commit -m "Re-route completed downloads with .m4b files to audiobooks folder (CLI watch)"
```

---

## Task 5: Wire post-completion audiobook re-router in TUI

**Goal:** Mirror Task 4's logic inside the TUI's `_scan_completed_download` method (`tui.py:413`), so audiobooks are also re-routed when running interactively.

**Files:**
- Modify: `src/torrentcli/tui.py:413-461` (`_scan_completed_download`)

**Step 1: Read the existing method in context.**

Open `src/torrentcli/tui.py:413-465`. Note where `result.clean` is checked and where `self.security.quarantine(...)` is called — the audiobook router goes immediately after the quarantine branch, in the same conditional flow.

**Step 2: Update imports.**

Run: `grep -n "^import shutil\|^from .security\|^from pathlib" src/torrentcli/tui.py`

- If `shutil` isn't imported, add `import shutil`.
- If `Path` isn't imported, add `from pathlib import Path`.
- Extend the existing `from .security import ...` line to include `is_audiobook_dir`, or add the import if absent.

**Step 3: Insert the re-router** right after the quarantine block (the `self.security.quarantine(download_path, result)` line and its conditional), at the same indentation level:

```python
        # Re-route audiobooks based on file content. Mirrors the CLI
        # watch-mode behavior in main.py.
        if result.clean and is_audiobook_dir(download_path):
            ab_root = Path(
                self.config.get("categories", "audiobooks", "path")
            ).expanduser()
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
```

**Step 4: Verify it parses.**

Run: `python -c "import torrentcli.tui"`
Expected: no errors.

**Step 5: Commit.**

```bash
git add src/torrentcli/tui.py
git commit -m "Re-route completed downloads with .m4b files to audiobooks folder (TUI)"
```

---

## Task 6: Add `tget jackett` command

**Goal:** New CLI subcommand that ensures Jackett is running, then opens its UI in the user's default browser so they can manage indexers.

Behavior:
1. Read `jackett.url` from config (default `http://localhost:9117`).
2. Probe the URL with `httpx.get(..., timeout=2)`. If it responds, skip step 3.
3. If unreachable, attempt `brew services start jackett` (only if `brew` is on PATH; print a friendly message if not).
4. After up to ~5s of polling, open the URL in the default browser via `webbrowser.open(url)`.
5. Print a status line.

**Files:**
- Modify: `src/torrentcli/main.py` (add a new `@cli.command` near the other top-level commands)

**Step 1: Locate a good spot for the new command.**

Run: `grep -n "^@cli.command" src/torrentcli/main.py`

Pick a position adjacent to the `vpn` command (similar shape: connection check + status print).

**Step 2: Verify httpx is already imported in main.py.**

Run: `grep -n "^import httpx\|^from httpx" src/torrentcli/main.py`. If absent, add `import httpx` to the stdlib/3rd-party import block.

**Step 3: Add the command.** Insert near the other `@cli.command` definitions:

```python
@cli.command()
def jackett():
    """Open Jackett's UI in your browser (start the service if needed)."""
    import shutil as _shutil
    import subprocess
    import time
    import webbrowser

    config = Config.load()
    url = config.get("jackett", "url") or "http://localhost:9117"

    def is_up() -> bool:
        try:
            r = httpx.get(url, timeout=2)
            return r.status_code < 500
        except Exception:
            return False

    if is_up():
        console.print(f"[green]✓ Jackett is up at {url}[/]")
    else:
        console.print(f"[yellow]Jackett not reachable at {url}[/]")
        if _shutil.which("brew"):
            console.print("[dim]Starting via brew services...[/]")
            try:
                subprocess.run(
                    ["brew", "services", "start", "jackett"],
                    capture_output=True, timeout=10,
                )
            except Exception as e:
                console.print(f"[yellow]brew services start failed: {e}[/]")
            for _ in range(10):
                time.sleep(0.5)
                if is_up():
                    console.print(f"[green]✓ Jackett is up at {url}[/]")
                    break
            else:
                console.print(
                    "[yellow]Jackett didn't come up in time — opening the URL anyway "
                    "(if you run it via Docker or another method, that's fine).[/]"
                )
        else:
            console.print(
                "[dim]brew not found — start Jackett yourself, then re-run.[/]"
            )

    webbrowser.open(url)
    console.print(f"[blue]Opened {url}[/]")
```

**Step 4: Verify it parses and registers.**

Run: `python -c "import torrentcli.main"`
Expected: no errors.

Run: `tget --help | grep -i jackett`
Expected: a line showing the `jackett` command and its summary.

**Step 5: Smoke-test the command (Jackett-running case).**

If Jackett is currently running:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9117 || echo "down"
```

If it printed `200` or `302`, run:
```bash
tget jackett
```
Expected: `✓ Jackett is up at http://localhost:9117` and the browser opens to it.

**Step 6: Smoke-test the command (Jackett-stopped case).**

```bash
brew services stop jackett
tget jackett
```
Expected: a "not reachable" message, a `brew services start jackett` attempt, then `✓ Jackett is up...`, then the browser opens.

**Step 7: Commit.**

```bash
git add src/torrentcli/main.py
git commit -m "Add 'tget jackett' command to launch and open the Jackett UI"
```

---

## Task 7: End-to-end manual verification

No code changes — sanity-check the integrated feature.

**Step 1: Title-tagged audiobook is detected at search time.**

Run: `tget search "Stephen King audiobook m4b" --limit 5`
Expected: command runs without error. (Category routing is exercised, but the CLI doesn't display category in the results table by default — coverage from the Task 1 smoke check is the actual proof.)

**Step 2: Untagged audiobook is post-corrected.**

Either:
- Add a real audiobook torrent whose title doesn't say "audiobook"/"m4b" but contents are M4B, then watch it complete with `tget status --watch`. Expected sequence: lands in non-audiobook folder → security scan reports clean → console prints `🎧 Routed audiobook → ~/Media/Audiobooks/<name>` → `ls ~/Media/Audiobooks/` shows the moved folder.
- Or simulate post-completion routing by hand:

```bash
# Create a synthetic "completed download" containing an M4B
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/Some Untagged Audiobook"
touch "$TMPDIR/Some Untagged Audiobook/ch01.m4b"

# Then run a one-shot Python check that performs the same logic the hook runs
python -c "
import shutil
from pathlib import Path
from torrentcli.config import Config
from torrentcli.security import is_audiobook_dir

src = Path('$TMPDIR/Some Untagged Audiobook')
print('is audiobook?', is_audiobook_dir(src))
ab_root = Path(Config.load().get('categories', 'audiobooks', 'path')).expanduser()
ab_root.mkdir(parents=True, exist_ok=True)
dest = ab_root / src.name
if dest.exists():
    print('already exists:', dest)
else:
    shutil.move(str(src), str(dest))
    print('moved →', dest)
"

ls ~/Media/Audiobooks/ | grep "Some Untagged Audiobook"
# Cleanup
rm -rf ~/Media/Audiobooks/"Some Untagged Audiobook"
rm -rf "$TMPDIR"
```

Expected: `is audiobook? True`, then `moved → ...`, then the `ls` shows the folder, then cleanup removes it.

**Step 3: Non-audiobook downloads aren't moved.**

Complete any movie/TV download (or simulate one without an `.m4b` inside). Expected: the download stays in its category folder; the console never prints the `🎧 Routed audiobook` line.

**Step 4: `tget jackett` from a clean state.**

```bash
brew services stop jackett
tget jackett
```
Expected: Jackett comes up, browser opens to its UI. From there, add a new audiobook indexer (e.g., AudioBookBay or any audiobook-friendly Torznab provider).

**Step 5: No commit needed unless you fixed something during verification.**

---

## Notes for the implementer

- **YAGNI:** Don't add a "source provider" abstraction or a config-driven file-extension list. M4B is the only signal we route on; we can generalize later if needed.
- **Error handling:** Mirror existing patterns. The codebase uses broad `try/except: continue` in search and gentle `console.print` warnings in scans — match that tone, no tracebacks for users.
- **Comments:** The audiobook precedence note in `_detect_category` is worth keeping (it documents a non-obvious ordering constraint). The notes inside the post-completion hook are worth keeping for the same reason. Don't add explanatory comments anywhere else.
- **Seeding:** `shutil.move` will break aria2's seeding for the moved folder. This matches how `quarantine` already behaves (`security.py:333`). If the user complains later, we can add a "wait for seed ratio before moving" step — not in scope now.
- **Two config files in lockstep:** Always update both `config.example.yaml` and `src/torrentcli/config.example.yaml`. They drift easily.
