"""A private Cloudflare solver: FlareSolverr with its own browser, on demand.

Jackett cannot answer a Cloudflare interstitial — that needs a real browser,
and Jackett deliberately is not one. FlareSolverr is one: it takes a URL, gets
past the challenge, and hands back the `cf_clearance` cookie Jackett then
replays on its own requests.

Three decisions shape this module, every one of them measured on a live setup
rather than assumed (macOS 15.6, 2026-08-02):

* **Its own Chrome, always.** Chrome for Testing is downloaded into the state
  dir even when the machine already has Chrome. Borrowing someone's daily
  browser means their profile and their session, and a second Chrome appearing
  while they are using the first. Shipping the browser also pins the
  chromedriver that has to match it, so nothing breaks the day Chrome
  auto-updates underneath us.

* **Head-full, parked off-screen.** `HEADLESS=true` cannot work here:
  FlareSolverr hides the browser behind Xvfb, which macOS does not have — that
  virtual display is what running this in Docker was really buying. Chrome's
  own `--headless=new` gets detected and killed by Cloudflare mid-challenge
  ("target window already closed"). A real window at -4000,-4000 solves
  reliably and never appears on screen.

* **Spawned per repair, never supervised.** Jackett stores the clearance
  cookie per indexer on disk and replays it over plain HTTP — a full search
  returns 50 results with the solver stopped entirely. The solver is only
  needed to *mint* a cookie, so it runs for the seconds that takes and exits.
  Nothing idles, and no browser sits in memory between searches.

Clearance was measured expiring inside 26 minutes, so a cold solve is the
normal case at launch rather than an edge case. That is why nothing here runs
at startup: priming is something onboarding or the user asks for, and a stale
cookie degrades to one skipped indexer rather than a stalled app.
"""

import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx

from .paths import STATE_DIR

# ── Layout on disk ────────────────────────────────────────────────────────────

SOLVER_DIR = STATE_DIR / "solver"
SRC_DIR = SOLVER_DIR / "flaresolverr"
VENV_DIR = SOLVER_DIR / "venv"
CHROME_DIR = SOLVER_DIR / "chrome"
MANIFEST = SOLVER_DIR / "manifest.json"

# Pinned to a release rather than a branch: this is third-party code we launch
# as a subprocess, and "whatever master happens to be" is not a thing to ship
# to strangers. Bumping it means re-checking the patch anchors below.
FLARESOLVERR_TAG = "v3.5.0"
FLARESOLVERR_URL = (
    "https://github.com/FlareSolverr/FlareSolverr/archive/refs/tags/"
    f"{FLARESOLVERR_TAG}.tar.gz"
)

# Google's own index of Chrome for Testing builds. Resolved at install time so
# a fresh install gets a current browser; whatever it resolves to is written
# into the manifest, so an installed solver stays on a known version.
CFT_VERSIONS_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)

DEFAULT_PORT = 8191

# A cold solve ran 38.7s against 1337x and 39.3s against a KickassTorrents
# mirror, and Jackett's own default ceiling (55s) was measured being grazed at
# 56.3s. This is what we raise Jackett to, so a slow solve finishes instead of
# being cut off one second from the answer.
JACKETT_SOLVE_TIMEOUT_MS = 120_000

# The floor a per-indexer search timeout has to clear for a solver-backed
# tracker to be able to answer at all. A *warm* 1337x — cookie already minted,
# no solving involved — took 14.1s and 14.4s on consecutive runs, because the
# site itself is slow, not because of Cloudflare. Anything under this repairs
# an indexer that then times out on every search regardless.
MIN_INDEXER_TIMEOUT = 30


class SolverError(Exception):
    """Install or launch failed. Callers degrade; they never crash on this."""


# ── Patches ───────────────────────────────────────────────────────────────────

# FlareSolverr assumes Linux-in-a-container. Three edits make it a well-behaved
# macOS subprocess. Each is a candidate for upstream rather than a fork: none
# changes how solving works, only where the browser comes from and where its
# window goes.
#
# Anchors are matched exactly and must appear exactly once — if upstream moves
# one, the install fails loudly here instead of silently launching a visible
# browser or the wrong Chrome.
_PATCHES = [
    (
        # 1. Name the browser explicitly. FlareSolverr's own bundled-browser
        # hook (src/chrome/chrome) cannot be used: on macOS Chrome has to be
        # launched from inside its .app, and a symlink to the binary launches
        # a GUI instead of honouring --version.
        "    CHROME_EXE_PATH = uc.find_chrome_executable()\n"
        "    return CHROME_EXE_PATH",
        "    override = os.environ.get('CHROME_EXE_PATH')\n"
        "    if override:\n"
        "        CHROME_EXE_PATH = override\n"
        "        return CHROME_EXE_PATH\n"
        "    CHROME_EXE_PATH = uc.find_chrome_executable()\n"
        "    return CHROME_EXE_PATH",
    ),
    (
        # 2. Off-screen instead of Xvfb. macOS has no Xvfb, so the stock
        # headless path raises before the first request is served.
        "        else:\n"
        "            start_xvfb_display()",
        "        elif os.environ.get('OFFSCREEN', '').lower() == 'true':\n"
        "            options.add_argument('--window-position=-4000,-4000')\n"
        "        else:\n"
        "            start_xvfb_display()",
    ),
    (
        # 3. Upstream's own fix, landed after v3.5.0 was cut. Chrome 148+
        # raises a Local Network Access prompt that interrupts a solve, and
        # the browser we ship is newer than the pinned release expects.
        "    options.add_argument('--ignore-ssl-errors')",
        "    options.add_argument('--ignore-ssl-errors')\n"
        "    # disable breaking popup\n"
        '    options.add_argument("--disable-features=LocalNetworkAccessChecks")',
    ),
]


def _apply_patches(utils_py: Path) -> None:
    text = utils_py.read_text()
    for old, new in _PATCHES:
        if new in text:
            continue  # already patched; install is safe to re-run
        if text.count(old) != 1:
            raise SolverError(
                f"FlareSolverr {FLARESOLVERR_TAG} does not look the way trrnt "
                f"expects (anchor found {text.count(old)}× in utils.py). "
                "Refusing to patch it blindly."
            )
        text = text.replace(old, new)
    utils_py.write_text(text)


# ── Platform ──────────────────────────────────────────────────────────────────

def cft_platform() -> str:
    """Chrome for Testing's name for this machine."""
    system, machine = platform.system(), platform.machine()
    if system == "Darwin":
        return "mac-arm64" if machine == "arm64" else "mac-x64"
    if system == "Linux":
        return "linux64"
    raise SolverError(f"no Chrome for Testing build for {system}/{machine}")


def uses_offscreen() -> bool:
    """macOS hides the browser by position; Linux has Xvfb and should use it."""
    return platform.system() == "Darwin"


def chrome_binary() -> Path | None:
    """The bundled browser, or None if it isn't unpacked yet.

    Globbed rather than hardcoded so a rename inside the archive surfaces as
    "not installed" instead of a path that silently fails to launch.
    """
    if not CHROME_DIR.is_dir():
        return None
    if platform.system() == "Darwin":
        for app in CHROME_DIR.glob("*/*.app/Contents/MacOS/*"):
            if app.is_file() and os.access(app, os.X_OK):
                return app
        return None
    for name in ("chrome", "chrome-headless-shell"):
        for found in CHROME_DIR.glob(f"*/{name}"):
            if found.is_file() and os.access(found, os.X_OK):
                return found
    return None


def venv_python() -> Path:
    return VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"


def installed() -> bool:
    """Every piece present. A half-finished install must read as absent."""
    return (
        MANIFEST.exists()
        and (SRC_DIR / "src" / "flaresolverr.py").exists()
        and venv_python().exists()
        and chrome_binary() is not None
    )


def manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


# ── Install ───────────────────────────────────────────────────────────────────

async def _download(url: str, dest: Path, on_line, label: str) -> None:
    """Stream a download, reporting progress on whole percents only.

    The wizard's log is a terminal widget, not a progress bar — a line per
    chunk would be thousands of lines for a 180MB browser.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = -1
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
                    if not total:
                        continue
                    pct = resp.num_bytes_downloaded * 100 // total
                    if pct != last and pct % 5 == 0:
                        last = pct
                        on_line(f"  {label}: {pct}% of {total / 1e6:.0f}MB")


def _unzip(archive: Path, dest: Path) -> None:
    """Extract with the system unzip.

    Python's zipfile restores neither the executable bit nor symlinks, and a
    macOS .app is full of both — extracting it with zipfile produces a bundle
    that cannot launch. unzip ships with macOS and every Linux we target.
    """
    dest.mkdir(parents=True, exist_ok=True)
    unzip = shutil.which("unzip")
    if not unzip:
        raise SolverError("unzip not found — cannot unpack the browser")
    proc = subprocess.run(
        [unzip, "-q", "-o", str(archive), "-d", str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SolverError(f"unpacking {archive.name} failed: {proc.stderr[:200]}")


async def _fetch_flaresolverr(on_line) -> None:
    tarball = SOLVER_DIR / f"flaresolverr-{FLARESOLVERR_TAG}.tar.gz"
    on_line(f"Fetching FlareSolverr {FLARESOLVERR_TAG}…")
    await _download(FLARESOLVERR_URL, tarball, on_line, "flaresolverr")

    if SRC_DIR.exists():
        shutil.rmtree(SRC_DIR)
    staging = SOLVER_DIR / "_src_staging"
    if staging.exists():
        shutil.rmtree(staging)
    with tarfile.open(tarball) as tf:
        # The GitHub tarball wraps everything in one FlareSolverr-<ver>/ dir.
        tf.extractall(staging, filter="data")
    roots = [p for p in staging.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise SolverError("unexpected FlareSolverr archive layout")
    roots[0].rename(SRC_DIR)
    shutil.rmtree(staging, ignore_errors=True)
    tarball.unlink(missing_ok=True)

    on_line("Patching for macOS…" if uses_offscreen() else "Patching…")
    _apply_patches(SRC_DIR / "src" / "utils.py")


async def _make_venv(on_line) -> None:
    """A venv of its own, so eight extra packages never reach trrnt's."""
    on_line("Building the solver's environment…")
    if VENV_DIR.exists():
        shutil.rmtree(VENV_DIR)
    rc = await _run([sys.executable, "-m", "venv", str(VENV_DIR)], on_line)
    if rc != 0:
        raise SolverError("could not create the solver venv")
    rc = await _run(
        [str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         "-r", str(SRC_DIR / "requirements.txt")],
        on_line,
    )
    if rc != 0:
        raise SolverError("could not install the solver's dependencies")


async def _fetch_chrome(on_line) -> str:
    plat = cft_platform()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(CFT_VERSIONS_URL)
        resp.raise_for_status()
        stable = resp.json()["channels"]["Stable"]
    url = next(
        (d["url"] for d in stable["downloads"]["chrome"] if d["platform"] == plat),
        None,
    )
    if not url:
        raise SolverError(f"Chrome for Testing has no {plat} build")

    version = stable["version"]
    on_line(f"Fetching Chrome for Testing {version} ({plat})…")
    archive = SOLVER_DIR / "chrome.zip"
    await _download(url, archive, on_line, "chrome")
    if CHROME_DIR.exists():
        shutil.rmtree(CHROME_DIR)
    on_line("Unpacking the browser…")
    _unzip(archive, CHROME_DIR)
    archive.unlink(missing_ok=True)
    if chrome_binary() is None:
        raise SolverError("browser unpacked but no executable was found in it")
    return version


async def install(on_line=lambda _l: None) -> dict:
    """Download, patch and build everything. Safe to re-run over a broken try."""
    SOLVER_DIR.mkdir(parents=True, exist_ok=True)
    await _fetch_flaresolverr(on_line)
    await _make_venv(on_line)
    chrome_version = await _fetch_chrome(on_line)
    info = {
        "flaresolverr": FLARESOLVERR_TAG,
        "chrome": chrome_version,
        "platform": cft_platform(),
        "installed_at": int(time.time()),
    }
    MANIFEST.write_text(json.dumps(info, indent=2))
    on_line(f"Solver ready — FlareSolverr {FLARESOLVERR_TAG}, Chrome {chrome_version}.")
    return info


def uninstall() -> None:
    shutil.rmtree(SOLVER_DIR, ignore_errors=True)


# ── Running it ────────────────────────────────────────────────────────────────

async def _run(cmd: list[str], on_line) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        on_line(raw.decode(errors="replace").rstrip())
    return await proc.wait()


def _free_port(preferred: int) -> int:
    """`preferred` if nothing holds it, else whatever the OS hands out.

    Someone may already run FlareSolverr on 8191. Rather than fight them for
    the port, we take another one and tell Jackett where we actually are.
    """
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SolverProcess:
    """FlareSolverr, alive for exactly as long as the `async with` block.

    Bound to loopback: this speaks an unauthenticated fetch-any-URL API, and
    it has no business being reachable from the network.
    """

    def __init__(self, port: int = DEFAULT_PORT, ready_timeout: float = 90.0):
        self.port = _free_port(port)
        self.url = f"http://127.0.0.1:{self.port}"
        self._ready_timeout = ready_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._drainer: asyncio.Task | None = None
        self.log: list[str] = []

    async def __aenter__(self) -> "SolverProcess":
        if not installed():
            raise SolverError("the solver is not installed")
        chrome = chrome_binary()
        assert chrome is not None
        env = {
            **os.environ,
            "PORT": str(self.port),
            "HOST": "127.0.0.1",
            "HEADLESS": "true",
            "LOG_LEVEL": "info",
            "CHROME_EXE_PATH": str(chrome),
            "PROMETHEUS_ENABLED": "false",
        }
        if uses_offscreen():
            env["OFFSCREEN"] = "true"
        self._proc = await asyncio.create_subprocess_exec(
            str(venv_python()), str(SRC_DIR / "src" / "flaresolverr.py"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=str(SRC_DIR),
        )
        self._drainer = asyncio.create_task(self._drain(self._proc))
        try:
            await self._wait_ready()
        except Exception:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _drain(self, proc: asyncio.subprocess.Process) -> None:
        """Keep the pipe empty; a full one would wedge the child.

        Holds its own reference to the process: shutdown clears the
        attribute, and reading it here would race that to an AttributeError
        every time the solver stopped normally.
        """
        if proc.stdout is None:
            return
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            self.log.append(raw.decode(errors="replace").rstrip())
            del self.log[:-200]

    async def _wait_ready(self) -> None:
        """Poll until it answers. Startup includes a real browser launch."""
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        async with httpx.AsyncClient(timeout=5.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._proc is not None and self._proc.returncode is not None:
                    tail = " / ".join(self.log[-3:]) or "no output"
                    raise SolverError(f"solver exited at startup: {tail}")
                try:
                    r = await client.post(
                        f"{self.url}/v1", json={"cmd": "sessions.list"}
                    )
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        tail = " / ".join(self.log[-3:]) or "no output"
        raise SolverError(f"solver did not come up in {self._ready_timeout:.0f}s: {tail}")

    async def __aexit__(self, *_exc) -> None:
        proc, self._proc = self._proc, None
        drainer, self._drainer = self._drainer, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if drainer is not None:
            drainer.cancel()

    async def solve(self, url: str, max_timeout_ms: int = 120_000) -> dict:
        """Solve one URL directly. Used to self-test after install."""
        async with httpx.AsyncClient(timeout=max_timeout_ms / 1000 + 30) as client:
            r = await client.post(
                f"{self.url}/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": max_timeout_ms},
            )
            r.raise_for_status()
            return r.json()


# ── Jackett wiring ────────────────────────────────────────────────────────────

async def prime(admin, indexer_ids: list[str], on_result=None) -> list[tuple[str, str, str]]:
    """Make Jackett query each indexer, minting a clearance cookie if needed.

    This is both the check and the repair — there is no way to know a cookie
    is good except by using it. A warm indexer answers in well under a second
    (0.1s measured); a cold one solves and takes tens of seconds.
    """
    async def one(indexer_id: str) -> tuple[str, str, str]:
        verdict, detail = await admin.test_indexer(indexer_id)
        result = (indexer_id, verdict, detail)
        if on_result:
            on_result(result)
        return result

    return list(await asyncio.gather(*(one(i) for i in indexer_ids)))


def cookie_age_seconds(indexer_id: str) -> float | None:
    """Rough age of Jackett's stored clearance, or None if it has none.

    A hint for skipping needless work, never a source of truth: Cloudflare
    sets the lifetime, not us, and this reads a file Jackett owns. The
    timestamp is the one embedded in the cf_clearance value itself.
    """
    config = (
        Path.home() / "Library" / "Application Support" / "Jackett" / "Indexers"
        / f"{indexer_id}.json"
    )
    if not config.exists():
        config = Path.home() / ".config" / "Jackett" / "Indexers" / f"{indexer_id}.json"
    try:
        data = json.loads(config.read_text())
    except (OSError, ValueError):
        return None
    items = data.get("list", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == "cookieheader":
            value = str(item.get("value") or "")
            if "cf_clearance" not in value:
                return None
            for part in value.replace("=", "-").split("-"):
                if part.isdigit() and len(part) == 10:
                    return max(0.0, time.time() - int(part))
            return None
    return None
