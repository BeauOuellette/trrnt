"""Filing downloads under clean names, and keeping junk off the disk.

naming.py decides what a download should be called and where it belongs;
this module makes it happen across the download's whole life:

* before any payload byte moves — deselect junk files (.nfo, .txt, samples)
  on the paused follow-up download aria2 creates once metadata resolves, so
  they are never downloaded at all;
* on completion — move the wanted files out of the release's gibberish
  wrapper folder into the chosen destination under the chosen name.

Plans survive restarts through a JSON file in the tget state dir, keyed by a
GID we assign at add time. That key is durable: aria2's session file records
the `gid=` option and re-creates the download under the same GID on the next
run (verified against aria2 1.37 — the session also carries `select-file`
forward, which is why a restart doesn't resurrect junk). The follow-up
download that actually carries the torrent gets a fresh GID each session, so
it is always re-derived from the parent's `followedBy` rather than stored as
truth.
"""

import json
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .daemon import STATE_DIR
from .naming import parse_release_name
from .security import VIDEO_EXTENSIONS

# File types that are release-scene packaging, not content. Conservative on
# purpose: subtitles are never here, and the ambiguous types are exempted per
# category below rather than removed from the default.
DEFAULT_JUNK_EXTENSIONS = {
    ".nfo", ".sfv", ".srr", ".srs", ".diz", ".url", ".website", ".torrent",
    ".md5", ".sha1", ".sha256",
    ".txt", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".htm", ".html",
}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# The junk types that are junk in *any* category — never content, never art.
_UNAMBIGUOUS_JUNK = {
    ".nfo", ".sfv", ".srr", ".srs", ".diz", ".url", ".website", ".torrent",
    ".md5", ".sha1", ".sha256",
}

# "sample.mkv" / "Show.S01E01.sample.mkv" — a video that is not the video.
_SAMPLE_TOKEN = re.compile(r"(^|[\s._-])sample([\s._-]|$)", re.IGNORECASE)
_SAMPLE_MAX_BYTES = 200_000_000

# Wrapper subfolders that carry no content.
_JUNK_DIR_NAMES = {"sample", "samples", "proof", "screens", "screenshots"}


def new_plan_gid() -> str:
    """A GID for aria2's `gid` option: 16 hex chars, ours to key plans on."""
    return uuid.uuid4().hex[:16]


def effective_junk(category: str, configured: set[str] | None = None) -> set[str]:
    """The junk set for one category.

    Cover art belongs with music and audiobooks, a .txt can *be* an ebook,
    and for unknown content only the unambiguous scene droppings qualify.
    """
    junk = set(configured) if configured is not None else set(DEFAULT_JUNK_EXTENSIONS)
    if category in ("movies", "tv", "comics"):
        return junk
    if category in ("music", "audiobooks"):
        return junk - _IMAGE_EXTENSIONS
    if category == "ebooks":
        return junk - _IMAGE_EXTENSIONS - {".txt"}
    return junk & _UNAMBIGUOUS_JUNK


def configured_junk(config) -> set[str]:
    """The junk extension list from config, normalized to ".ext" form."""
    raw = config.get("organize", "junk_extensions", default=None)
    if not raw:
        return set(DEFAULT_JUNK_EXTENSIONS)
    return {
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in raw
    }


def _is_sample(path: Path, length: int) -> bool:
    return (
        path.suffix.lower() in VIDEO_EXTENSIONS
        and bool(_SAMPLE_TOKEN.search(path.stem))
        and 0 < length < _SAMPLE_MAX_BYTES
    )


def plan_selection(files: list[dict], junk_exts: set[str]) -> list[int] | None:
    """aria2 file indices worth downloading, or None to leave selection alone.

    None both when nothing is junk (no RPC call needed) and when *everything*
    would be junk — a torrent that is all .txt is an ebook we misjudged, and
    deselecting every file is never what anyone wants.
    """
    keep = []
    for f in files:
        path = Path(f.get("path", ""))
        if path.suffix.lower() in junk_exts:
            continue
        if _is_sample(path, int(f.get("length", 0))):
            continue
        keep.append(int(f["index"]))
    if not keep or len(keep) == len(files):
        return None
    return keep


# ── Plan store ────────────────────────────────────────────────────────────────

@dataclass
class OrganizeRecord:
    """One download's filing plan, durable across app and aria2 restarts."""

    gid: str                    # the GID we assigned at add time (durable)
    dir: str                    # directory the torrent was added with
    category: str = ""
    name: str = ""              # clean name; empty = as-is, junk pruning only
    selection_done: bool = False
    active_gid: str = ""        # this session's follow-up GID, best effort
    created: str = ""
    # True once aria2 accepted index-out mappings: the kept files are being
    # written straight to their final paths, so completion is cleanup only
    # and the torrent can keep seeding.
    remapped: bool = False
    # The torrent's own folder name inside dir (empty for single-file
    # torrents). Lets the orphan sweep finish a download aria2 has already
    # forgotten without guessing at directory contents.
    wrapper: str = ""


class OrganizeStore:
    """JSON-backed record set in the tget state dir.

    Reloaded before every use because the TUI and `status --watch` may both
    be running; the file is small enough that correctness wins over cleverness.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else STATE_DIR / "organize.json"
        self._records: dict[str, OrganizeRecord] = {}
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self._records = {
                r["gid"]: OrganizeRecord(**r) for r in raw if r.get("gid")
            }
        except (OSError, ValueError, TypeError):
            self._records = {}
        self._prune()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([asdict(r) for r in self._records.values()], indent=1)
            )
        except OSError:
            pass  # losing a plan degrades to today's behavior, not to breakage

    def _prune(self, max_age_days: int = 30) -> None:
        cutoff = datetime.now() - timedelta(days=max_age_days)
        stale = []
        for gid, rec in self._records.items():
            try:
                if datetime.fromisoformat(rec.created) < cutoff:
                    stale.append(gid)
            except ValueError:
                stale.append(gid)
        for gid in stale:
            del self._records[gid]
        if stale:
            self._save()

    def add(self, record: OrganizeRecord) -> None:
        if not record.created:
            record.created = datetime.now().isoformat(timespec="seconds")
        self._records[record.gid] = record
        self._save()

    def update(self, record: OrganizeRecord) -> None:
        self._records[record.gid] = record
        self._save()

    def remove(self, gid: str) -> None:
        if gid in self._records:
            del self._records[gid]
            self._save()

    def pending(self) -> list[OrganizeRecord]:
        return [r for r in self._records.values() if not r.selection_done]

    def settled(self) -> list[OrganizeRecord]:
        return [r for r in self._records.values() if r.selection_done]

    def by_gid(self, gid: str) -> OrganizeRecord | None:
        """The record for a GID, with no directory guessing.

        Separate from match() because their tolerances differ. Filing a
        finished torrent can afford the directory fallback — a wrong guess
        there is caught by the single-hit guard. Labelling a table row cannot:
        an as-is download sharing a category root with exactly one organized
        record would wear that record's name, and the user would be looking at
        a row that claims to be a different torrent.
        """
        if not gid:
            return None
        for rec in self._records.values():
            if gid in (rec.active_gid, rec.gid):
                return rec
        return None

    def match(self, gid: str, dl_dir: str) -> OrganizeRecord | None:
        """The record for a finished download.

        The follow-up GID is per-session, so it only matches when this
        session's applier saw the download; the directory is the durable
        fallback — but only when it identifies exactly one record, since
        as-is movie downloads all share the category root.
        """
        for rec in self._records.values():
            if gid and gid in (rec.active_gid, rec.gid):
                return rec
        dir_hits = [r for r in self._records.values() if r.dir == dl_dir]
        if len(dir_hits) == 1:
            return dir_hits[0]
        return None


def plan_output_names(
    files: list[dict], clean_name: str, junk_exts: set[str], dest_dir: str | Path
) -> dict[int, str]:
    """aria2 `index-out` mappings that write kept files straight to their
    final names — the wrapper folder never exists for them.

    Same naming rules as organize_download: episodes are renamed from their
    own SxxEyy, a lone video (and its subs) takes the clean name, everything
    else keeps its own name flattened out of the wrapper (subfolders like
    Subs/ keep their shape). A file whose target already exists on disk, or
    that would collide with another mapping, is left at its torrent path —
    the completion mover deals with it the old way.
    """
    dest_dir = Path(dest_dir)
    kept = []
    for f in files:
        p = Path(f.get("path", ""))
        if not f.get("path"):
            return {}
        if p.suffix.lower() in junk_exts or _is_sample(p, int(f.get("length", 0))):
            continue
        kept.append(f)
    if not kept:
        kept = list(files)  # the all-junk guard, mirroring plan_selection

    rel_parts: list[tuple[dict, tuple[str, ...]]] = []
    for f in kept:
        try:
            parts = Path(f["path"]).relative_to(dest_dir).parts
        except ValueError:
            return {}  # paths not under the download dir — don't touch anything
        rel_parts.append((f, parts))

    # A multi-file torrent puts everything under one info-name root; dropping
    # that first component is the flatten. A single-file torrent has none.
    multi = len(files) > 1
    rels = [
        Path(*parts[1:]) if multi and len(parts) > 1 else Path(parts[-1])
        for _, parts in rel_parts
    ]

    renamable = {".srt", ".sub", ".ass", ".ssa", ".idx"} | VIDEO_EXTENSIONS
    videos = sum(1 for r in rels if r.suffix.lower() in VIDEO_EXTENSIONS)
    base_title = parse_release_name(clean_name).title or clean_name

    out: dict[int, str] = {}
    taken: set[str] = set()
    for (f, _), rel in zip(rel_parts, rels):
        suffix = rel.suffix.lower()
        new = rel
        if len(rel.parts) == 1 and suffix in renamable:
            episode = _episode_rename(rel, base_title)
            if episode:
                new = Path(episode)
            elif videos == 1:
                new = Path(f"{clean_name}{suffix}")
        target = new.as_posix()
        if target in taken or (dest_dir / new).exists():
            continue  # collision — leave this file at its torrent path
        taken.add(target)
        out[int(f["index"])] = target
    return out


def cleanup_wrapper(wrapper: Path, junk_exts: set[str]) -> bool:
    """Remove a wrapper folder left holding only junk placeholders.

    After a remapped download finishes, the torrent-name folder contains at
    most the deselected junk (zero-byte placeholders until aria2's
    bt-remove-unselected-file deletes them). True when the wrapper is gone;
    False when something real is inside and the move path should handle it.
    """
    if not wrapper.is_dir():
        return True
    for entry in wrapper.rglob("*"):
        if not entry.is_file() or entry.name == ".DS_Store":
            continue
        if entry.suffix.lower() in junk_exts:
            continue
        if entry.stat().st_size == 0:
            continue
        return False
    shutil.rmtree(wrapper, ignore_errors=True)
    return True


# ── In-flight file selection ──────────────────────────────────────────────────

async def apply_pending_selection(
    aria2, store: OrganizeStore, config, notify=None
) -> None:
    """Deselect junk on follow-up downloads whose metadata has resolved.

    Runs from the refresh loops. Each pending record's parent GID is asked
    for its `followedBy`; once the follow-up exists we push `select-file`,
    clear `pause-metadata` (so a session restore doesn't re-pause the
    download forever), record the follow-up GID, and unpause.
    """
    pending = store.pending()
    if not pending:
        return

    junk_config = configured_junk(config)
    for record in pending:
        try:
            status = await aria2.get_status(record.gid)
        except Exception:
            # aria2 doesn't know this GID (removed, or a session that never
            # made it into the session file). Old records get pruned; young
            # ones stay for the next tick in case aria2 is still starting.
            continue
        if status.status in ("error", "removed"):
            store.remove(record.gid)
            continue
        if not status.followed_by:
            continue  # metadata still resolving

        gid = status.followed_by[-1]
        junk = effective_junk(record.category, junk_config)
        remapped = False
        try:
            files = await aria2.get_files_detailed(gid)
            if not files:
                continue
            keep = plan_selection(files, junk)
            if keep:
                await aria2.change_option(
                    gid, {"select-file": ",".join(str(i) for i in keep)}
                )
            if record.name:
                # Write kept files straight to their final names — no
                # wrapper folder, and nothing to move when it finishes.
                outputs = plan_output_names(files, record.name, junk, record.dir)
                if outputs:
                    try:
                        await aria2.change_option(gid, {
                            "index-out": [
                                f"{i}={path}" for i, path in sorted(outputs.items())
                            ],
                        })
                        remapped = True
                    except Exception:
                        pass  # old aria2 — the completion mover still files it
            # Without this, the session file keeps pause-metadata=true and
            # every aria2 restart re-creates the download paused, forever.
            await aria2.change_option(gid, {"pause-metadata": "false"})
        except Exception:
            continue  # try again next tick

        record.active_gid = gid
        record.selection_done = True
        record.remapped = remapped
        if len(files) > 1:
            try:
                parts = Path(files[0]["path"]).relative_to(record.dir).parts
                if len(parts) > 1:
                    record.wrapper = parts[0]
            except ValueError:
                pass
        store.update(record)

        try:
            follow = await aria2.get_status(gid)
            if follow.status == "paused":
                await aria2.unpause(gid)
        except Exception:
            pass

        if keep and notify:
            skipped = len(files) - len(keep)
            label = record.name or Path(files[0]["path"]).parent.name or "download"
            notify(f"Skipping {skipped} junk file(s): {label[:40]}")


async def find_orphan_records(aria2, store: OrganizeStore) -> list[OrganizeRecord]:
    """Settled records whose downloads aria2 no longer knows about.

    A completion is missed when its result leaves aria2 before the scan loop
    sees it — Clear Done, a remove, the app closed at the wrong moment. The
    plan record is the surviving evidence. Records whose wrapper folder is
    still on disk are returned so the caller can run them through the normal
    completion path; records with nothing left on disk to do are dropped
    here. Records whose downloads are still alive are never touched.
    """
    to_finish = []
    for record in store.settled():
        alive = False
        confirmed_gone = 0
        probed = 0
        for gid in (record.active_gid, record.gid):
            if not gid:
                continue
            probed += 1
            try:
                await aria2.get_status(gid)
                alive = True
                break
            except RuntimeError as e:
                # aria2 answered and doesn't know the GID — that's the only
                # response that proves the download is gone. Anything else
                # (transport trouble, a busy daemon) must read as alive, or a
                # blip could push a still-downloading torrent into the mover.
                # aria2 1.37 says "GID … is not found"; other versions say
                # "No such download" — accept both.
                message = str(e).lower()
                if "not found" in message or "no such download" in message:
                    confirmed_gone += 1
                else:
                    alive = True
                    break
            except Exception:
                alive = True
                break
        if alive or confirmed_gone < probed or probed == 0:
            continue
        if record.wrapper and (Path(record.dir) / record.wrapper).exists():
            to_finish.append(record)
        else:
            # Whatever happened, there is no folder left to file from —
            # the record is stale bookkeeping.
            store.remove(record.gid)
    return to_finish


# ── Completion-time filing ────────────────────────────────────────────────────

def _unique_target(folder: Path, name: str) -> Path | None:
    """`folder/name` if free, else None — collisions are reported, not clobbered."""
    target = folder / name
    return None if target.exists() else target


def _episode_rename(path: Path, base_title: str) -> str | None:
    """Clean per-file name for an episode inside a season pack, if parseable."""
    parsed = parse_release_name(path.stem)
    if parsed.season is not None and parsed.episode is not None and base_title:
        return f"{base_title} S{parsed.season:02d}E{parsed.episode:02d}{path.suffix.lower()}"
    return None


def organize_download(
    download_path: Path,
    dest_dir: Path,
    clean_name: str,
    junk_exts: set[str],
) -> list[str]:
    """Move a finished download into place under its clean name.

    Single file: renamed to `<clean_name><ext>` in dest_dir. Folder: junk is
    deleted, media files move up into dest_dir (episodes renamed per-file
    from their own SxxEyy tags), non-junk subfolders (Subs/) move along, and
    the emptied wrapper folder is removed. Anything that would collide is
    left where it is and reported.
    """
    messages: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    renamable = {".srt", ".sub", ".ass", ".ssa", ".idx"} | VIDEO_EXTENSIONS
    base_title = parse_release_name(clean_name).title or clean_name

    def place(src: Path, new_name: str) -> None:
        if src.parent == dest_dir and src.name == new_name:
            return
        target = _unique_target(dest_dir, new_name)
        if target is None:
            messages.append(f"Exists, left in place: {new_name}")
            return
        shutil.move(str(src), str(target))

    if download_path.is_file():
        place(download_path, f"{clean_name}{download_path.suffix.lower()}")
        Path(f"{download_path}.aria2").unlink(missing_ok=True)
        return messages

    entries = sorted(download_path.iterdir())
    files = [e for e in entries if e.is_file()]
    videos = [
        f for f in files
        if f.suffix.lower() in VIDEO_EXTENSIONS
        and not _is_sample(f, f.stat().st_size)
    ]

    for entry in entries:
        if entry.name == ".DS_Store":
            continue
        if entry.is_dir():
            if entry.name.lower() in _JUNK_DIR_NAMES:
                shutil.rmtree(entry, ignore_errors=True)
            else:
                place(entry, entry.name)
            continue
        suffix = entry.suffix.lower()
        if suffix in junk_exts or _is_sample(entry, entry.stat().st_size):
            entry.unlink(missing_ok=True)
            continue
        if suffix in renamable:
            new_name = _episode_rename(entry, base_title)
            if new_name is None and len(videos) == 1:
                # One real video: it *is* the release, subs ride along.
                new_name = f"{clean_name}{suffix}"
            place(entry, new_name or entry.name)
        else:
            place(entry, entry.name)

    leftovers = [
        e for e in download_path.iterdir() if e.name != ".DS_Store"
    ] if download_path.exists() else []
    if not leftovers:
        shutil.rmtree(download_path, ignore_errors=True)
    Path(f"{download_path}.aria2").unlink(missing_ok=True)
    return messages
