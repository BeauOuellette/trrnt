"""The junk-exclusion flow against a real aria2c, end to end.

The chain under test: add with our GID + pause-metadata → aria2 fetches the
.torrent and spawns the follow-up download *paused* → the applier reads the
file list, deselects junk, clears pause-metadata, unpauses, and settles the
plan record. Faking aria2 here would prove nothing — the whole feature rests
on which options aria2 honours per-download and on paused follow-ups (its
`changeOption(dir)` famously pretends), so a real daemon is the only honest
harness. Same philosophy as test_daemon.py; skipped when aria2c is absent.

Everything is confined to scratch dirs and throwaway ports, DHT/LPD are off,
and the torrent's tracker plus the injected bt-tracker override point at a
dead localhost port so nothing announces anywhere.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from torrentcli.download import Aria2Client
from torrentcli.organize import (
    OrganizeRecord,
    OrganizeStore,
    apply_pending_selection,
    new_plan_gid,
)

HAVE_ARIA2C = shutil.which("aria2c") is not None
needs_aria2c = pytest.mark.skipif(not HAVE_ARIA2C, reason="aria2c not installed")

_DEAD_TRACKER = "http://127.0.0.1:1/announce"


class FakeConfig:
    def get(self, *keys, default=None):
        return default


def _bencode(obj) -> bytes:
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return _bencode(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(
            _bencode(k) + _bencode(obj[k]) for k in sorted(obj)
        ) + b"e"
    raise TypeError(obj)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def lab(tmp_path):
    """A real aria2c plus a local server holding one three-file torrent."""
    rpc_port, http_port = _free_port(), _free_port()
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()

    torrent = {
        "announce": _DEAD_TRACKER,
        "info": {
            "name": "www.FakeSite.com.Test.Show.S01E01.1080p.GROUP",
            "piece length": 16384,
            "pieces": b"\x00" * 20,
            "files": [
                {"length": 500, "path": ["Test.Show.S01E01.1080p.GROUP.mkv"]},
                {"length": 100, "path": ["release.nfo"]},
                {"length": 80, "path": ["Torrent downloaded from site.txt"]},
            ],
        },
    }
    (tmp_path / "t.torrent").write_bytes(_bencode(torrent))

    httpd = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(http_port),
         "--bind", "127.0.0.1", "--directory", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    aria2 = subprocess.Popen(
        ["aria2c", "--enable-rpc", "--rpc-listen-all=false",
         f"--rpc-listen-port={rpc_port}",
         f"--stop-with-process={os.getpid()}",
         f"--dir={dl_dir}",
         "--enable-dht=false", "--enable-dht6=false", "--bt-enable-lpd=false",
         "--listen-port=7861-7879", "--dht-listen-port=7861-7879"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    client = Aria2Client({
        "rpc_url": f"http://127.0.0.1:{rpc_port}/jsonrpc",
        "download_dir": str(dl_dir),
        "seed_ratio": 0,
    })

    async def _up() -> bool:
        for _ in range(75):
            if await client.check_connection():
                return True
            await asyncio.sleep(0.2)
        return False

    if not asyncio.run(_up()):
        for proc in (aria2, httpd):
            proc.kill()
        pytest.fail("scratch aria2c never answered RPC")

    lab_ns = type("Lab", (), {})()
    lab_ns.client = client
    lab_ns.url = f"http://127.0.0.1:{http_port}/t.torrent"
    lab_ns.dl_dir = dl_dir
    lab_ns.tmp = tmp_path
    yield lab_ns

    for proc in (aria2, httpd):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


@needs_aria2c
def test_junk_is_deselected_before_download_and_the_record_settles(lab):
    async def flow():
        gid = new_plan_gid()
        await lab.client.add_torrent_url(
            lab.url,
            download_dir=str(lab.dl_dir),
            extra_options={
                "gid": gid,
                "pause-metadata": "true",
                "bt-remove-unselected-file": "true",
                "bt-tracker": _DEAD_TRACKER,  # keep the test off real trackers
            },
        )
        store = OrganizeStore(lab.tmp / "organize.json")
        store.add(OrganizeRecord(
            gid=gid, dir=str(lab.dl_dir), category="tv",
            name="Test Show S01E01",
        ))

        # Metadata resolves locally within a moment; tick the applier the way
        # the refresh loops do until the record settles.
        deadline = time.monotonic() + 15
        while store.pending() and time.monotonic() < deadline:
            await apply_pending_selection(lab.client, store, FakeConfig())
            await asyncio.sleep(0.1)
        assert not store.pending(), "selection never applied"

        record = store.match("", str(lab.dl_dir))
        assert record is not None and record.active_gid

        files = await lab.client.get_files_detailed(record.active_gid)
        status = await lab.client.get_status(record.active_gid)
        await lab.client.force_remove(record.active_gid)
        return files, status, record

    files, status, record = asyncio.run(flow())

    selected = {Path(f["path"]).suffix.lower(): f["selected"] for f in files}
    assert selected[".mkv"] is True
    assert selected[".nfo"] is False, "junk survived selection"
    assert selected[".txt"] is False, "junk survived selection"
    # And the follow-up is no longer stuck paused.
    assert status.status in ("active", "waiting")

    # The kept file was remapped to its clean final path — no wrapper folder
    # in its way — while the junk stays at its torrent path, unselected.
    assert record.remapped is True
    by_suffix = {Path(f["path"]).suffix.lower(): Path(f["path"]) for f in files}
    assert by_suffix[".mkv"] == lab.dl_dir / "Test Show S01E01.mkv"
    assert by_suffix[".nfo"].parent != lab.dl_dir  # junk keeps the wrapper path
