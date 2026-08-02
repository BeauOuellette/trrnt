"""First-run onboarding: detection, Jackett's admin API, config writing.

The wizard screen in tui.py drives these pieces; everything here is UI-free
so it can be tested without a terminal. The design rule throughout: every
step must be safe to re-run (a user can quit the wizard half-way and run
`trrnt setup` again), and every automation has a manual fallback — nothing
here is allowed to be a dead end.
"""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx

from .paths import STATE_DIR

# formula name -> the binary that proves it works. jackett has no CLI binary
# on Homebrew (it ships a service), so its proof is the config it writes.
BREW_FORMULAS = ["aria2", "jackett", "clamav"]

# Public indexers surfaced first in the quick-pick, in this order. These are
# generic popular defaults, NOT anything read from an existing setup.
#
# Every id must match Jackett's catalog exactly or it silently never appears:
# "torrentgalaxy" and "solidtorrents" both sat here doing nothing until a
# catalog diff caught them (the former is "torrentgalaxyclone"; the latter
# does not exist). tests/test_onboard_live.py re-checks this list against a
# real catalog in CI so the next rename is caught rather than shipped.
CURATED_PUBLIC = [
    "thepiratebay", "therarbg", "torrentgalaxyclone", "knaben", "yts",
    "limetorrents", "nyaasi", "1337x", "eztv", "kickasstorrents-ws",
]

# Of those, the ones sitting behind Cloudflare: Jackett cannot reach them
# without FlareSolverr, because solving that challenge needs a real browser
# and Jackett deliberately isn't one. They stay in the list — they work fine
# once FlareSolverr is configured — but they are not ticked by default, so a
# first run with no solver ends up with indexers that actually answer.
#
# Measured 2026-08-01 against Jackett 0.24.2307 via its own test endpoint.
# Sites move on and off Cloudflare, so this is a starting guess, not truth:
# the wizard tests what it added and reports what really happened.
NEEDS_SOLVER = {"1337x", "eztv", "kickasstorrents-ws"}


# ── Detection ─────────────────────────────────────────────────────────────────

def brew_path() -> str | None:
    return shutil.which("brew")


def binary_present(name: str) -> bool:
    """PATH lookup plus the brew dirs PATH tends to miss (clamd lives in sbin)."""
    if shutil.which(name):
        return True
    for prefix in ("/opt/homebrew", "/usr/local"):
        for bindir in ("bin", "sbin"):
            if (Path(prefix) / bindir / name).exists():
                return True
    return False


def formula_installed(formula: str) -> bool:
    """Ask brew, not the PATH — jackett installs no binary at all."""
    try:
        return subprocess.run(
            ["brew", "list", "--versions", formula],
            capture_output=True, timeout=15,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def component_installed(formula: str) -> bool:
    binary = {"aria2": "aria2c", "clamav": "clamd"}.get(formula)
    if binary and binary_present(binary):
        return True  # however it got there — brew is not the only way in
    return formula_installed(formula)


# ── Jackett on disk ───────────────────────────────────────────────────────────

def jackett_config_paths() -> list[Path]:
    """Both layouts: macOS proper, and the XDG path Linux (and docs) use."""
    home = Path.home()
    return [
        home / "Library" / "Application Support" / "Jackett" / "ServerConfig.json",
        home / ".config" / "Jackett" / "ServerConfig.json",
    ]


def read_jackett_server_config() -> dict | None:
    """Jackett's own config, if it has started at least once.

    The APIKey in here is minted by Jackett on first launch — reading it is
    what lets onboarding skip the copy-a-key-from-a-web-page step entirely.
    """
    for path in jackett_config_paths():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("APIKey"):
            return data
    return None


def jackett_url(server_config: dict | None) -> str:
    port = (server_config or {}).get("Port") or 9117
    return f"http://localhost:{port}"


# ── Jackett's admin API ───────────────────────────────────────────────────────

class JackettAdminError(Exception):
    """Any admin-API failure. Callers treat it as 'open the UI instead'."""


class JackettAdmin:
    """The API Jackett's own web UI runs on: cookie login, catalog, adds.

    This is not the documented Torznab surface — it has no stability
    contract, which is why every caller must keep the open-the-UI fallback
    alive. It has, in practice, been stable for years, and it is the only
    road to one-keystroke indexer adds.

    Auth model (verified against a live 0.22 instance): every /api/v2.0/*
    call 302s to /UI/Login until the session cookie exists; a GET of
    /UI/Dashboard hands the cookie out when no admin password is set, and
    the same URL takes a form POST of the password when one is.
    """

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=10.0,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self, password: str | None = None) -> None:
        try:
            if password:
                await self._client.post("/UI/Dashboard", data={"password": password})
            r = await self._client.get("/UI/Dashboard")
        except httpx.HTTPError as e:
            raise JackettAdminError(f"Jackett unreachable: {e}") from e
        if r.status_code != 200 or "/UI/Login" in str(r.url):
            raise JackettAdminError(
                "Jackett wants its admin password" if not password
                else "Jackett rejected the admin password"
            )

    async def catalog(self) -> list[dict]:
        """Every indexer Jackett knows, configured or not."""
        r = await self._get_json("/api/v2.0/indexers", params={"configured": "false"})
        if not isinstance(r, list):
            raise JackettAdminError("catalog came back in an unexpected shape")
        return r

    async def configured_ids(self) -> set[str]:
        return {i["id"] for i in await self.catalog() if i.get("configured")}

    async def add_indexer(self, indexer_id: str) -> None:
        """One-click add: fetch the config template, post it straight back.

        For public indexers the template needs no edits — posting it
        unchanged is exactly what the web UI's plus-button does.
        """
        template = await self._get_json(f"/api/v2.0/indexers/{indexer_id}/config")
        try:
            r = await self._client.post(
                f"/api/v2.0/indexers/{indexer_id}/config", json=template,
            )
        except httpx.HTTPError as e:
            raise JackettAdminError(f"saving {indexer_id} failed: {e}") from e
        if r.status_code >= 400:
            raise JackettAdminError(f"{indexer_id}: HTTP {r.status_code}")

    async def delete_indexer(self, indexer_id: str) -> None:
        """Unconfigure an indexer in Jackett — the UI's trash button."""
        try:
            r = await self._client.delete(f"/api/v2.0/indexers/{indexer_id}")
        except httpx.HTTPError as e:
            raise JackettAdminError(f"removing {indexer_id} failed: {e}") from e
        if r.status_code >= 400:
            raise JackettAdminError(f"{indexer_id}: HTTP {r.status_code}")

    async def server_config(self) -> dict:
        """Jackett's own settings, in the lowercase shape its UI posts back."""
        cfg = await self._get_json("/api/v2.0/server/config")
        if not isinstance(cfg, dict):
            raise JackettAdminError("server config came back in an unexpected shape")
        return cfg

    async def set_flaresolverr(self, url: str, max_timeout_ms: int | None = None) -> None:
        """Point Jackett at a Cloudflare solver (or at nothing, with "").

        Posting the whole config back is what the dashboard's Save does; the
        keys are Jackett's, read straight from the GET above so we never
        invent one. Jackett restarts itself on save, so callers must expect a
        few seconds of unavailability afterwards.
        """
        cfg = await self.server_config()
        cfg["flaresolverrurl"] = url
        if max_timeout_ms is not None:
            cfg["flaresolverr_maxtimeout"] = max_timeout_ms
        try:
            r = await self._client.post(
                "/api/v2.0/server/config", json=cfg, timeout=httpx.Timeout(60.0),
            )
        except httpx.HTTPError as e:
            raise JackettAdminError(f"saving Jackett's config failed: {e}") from e
        if r.status_code >= 400:
            raise JackettAdminError(f"Jackett rejected the config: HTTP {r.status_code}")

    async def wait_until_up(self, timeout: float = 45.0) -> bool:
        """Poll the dashboard back to life after a config save restarts it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                r = await self._client.get("/UI/Dashboard", timeout=httpx.Timeout(5.0))
                if r.status_code == 200 and "/UI/Login" not in str(r.url):
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0)
        return False

    async def test_indexer(self, indexer_id: str) -> tuple[str, str]:
        """Ask Jackett to actually query an indexer. (verdict, detail).

        This is the same check the web UI's Test button runs: a real search
        against the live site, so it catches the failures an add cannot —
        chiefly Cloudflare, which is invisible until something is fetched.
        204 means it worked; anything else carries a reason.

        Verdicts: "ok", "cloudflare", "error".
        """
        try:
            r = await self._client.post(
                f"/api/v2.0/indexers/{indexer_id}/test", json={},
                timeout=httpx.Timeout(120.0),
            )
        except httpx.HTTPError as e:
            return "error", str(e)[:80]
        if r.status_code == 204:
            return "ok", ""
        try:
            detail = r.json().get("error", "") or f"HTTP {r.status_code}"
        except ValueError:
            detail = f"HTTP {r.status_code}"
        if "FlareSolverr" in detail:
            return "cloudflare", "needs FlareSolverr"
        return "error", detail.split(":")[-1].strip()[:80] or detail[:80]

    async def _get_json(self, path: str, params: dict | None = None):
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise JackettAdminError(f"Jackett unreachable: {e}") from e
        # An expired/absent cookie surfaces as a redirect to the login page
        # (followed to a 200 HTML body), not as a 401.
        if "/UI/Login" in str(r.url):
            raise JackettAdminError("not logged in to Jackett")
        if r.status_code != 200:
            raise JackettAdminError(f"{path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise JackettAdminError(f"{path}: not JSON") from e


def order_catalog(catalog: list[dict]) -> list[dict]:
    """Public indexers only, curated picks first, the rest alphabetical."""
    public = [i for i in catalog if i.get("type") == "public"]
    rank = {name: n for n, name in enumerate(CURATED_PUBLIC)}
    return sorted(
        public,
        key=lambda i: (rank.get(i["id"], len(rank)), (i.get("name") or i["id"]).lower()),
    )


# ── Writing config.yaml ───────────────────────────────────────────────────────

def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        # Flow style keeps this a one-line edit, which is what the line-based
        # writer below can safely replace. A block list would need to own
        # several lines and know how many the old value occupied.
        inner = ", ".join(f'"{v}"' for v in value)
        return f"[{inner}]"
    return f'"{value}"'


def set_yaml_values(text: str, values: dict[tuple[str, str], object]) -> str:
    """Set section.key values in YAML text without disturbing anything else.

    yaml.safe_load + dump would flatten the example file's comments, and the
    comments ARE the documentation — so this edits lines instead. It only
    handles the shape config.yaml actually has: top-level sections with
    two-space-indented keys. A key missing from its section is inserted
    right under the section header; a missing section is appended.
    """
    lines = text.splitlines()
    remaining = dict(values)
    section = None
    section_starts: dict[str, int] = {}

    for n, line in enumerate(lines):
        header = re.match(r"^([A-Za-z_][\w-]*):\s*(#.*)?$", line)
        if header:
            section = header.group(1)
            section_starts[section] = n
            continue
        if section is None:
            continue
        key_match = re.match(r"^(  [\w-]+):", line)
        if not key_match:
            continue
        key = key_match.group(1).strip().rstrip(":")
        target = (section, key)
        if target in remaining:
            lines[n] = f"  {key}: {_scalar(remaining.pop(target))}"

    # Keys whose line didn't exist: insert under their section header.
    for (section, key), value in sorted(
        remaining.items(), key=lambda kv: section_starts.get(kv[0][0], -1), reverse=True
    ):
        line = f"  {key}: {_scalar(value)}"
        if section in section_starts:
            lines.insert(section_starts[section] + 1, line)
        else:
            lines.extend(["", f"{section}:", line])

    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def write_config_values(path: Path, values: dict[tuple[str, str], object]) -> None:
    path.write_text(set_yaml_values(path.read_text(), values))


# ── Long-running commands, streamed ──────────────────────────────────────────

async def run_streaming(cmd: list[str], on_line) -> int:
    """Run a command, feeding each output line to on_line; returns the rc.

    brew installs run minutes, and a wizard that sits silent for minutes
    reads as hung — the streamed lines are the progress bar.
    """
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


async def wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    """Poll a sync predicate until true or the clock runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ── Keeping Homebrew formulas current ────────────────────────────────────────

# Jackett's own auto-updater cannot help here: Homebrew's launcher passes
# --NoUpdates, deliberately, because a self-update rewriting the Cellar would
# desync brew's idea of what is installed. So brew is the only update path,
# and something has to remember to use it.
UPDATE_CACHE = STATE_DIR / "update-check.json"
UPDATE_CHECK_INTERVAL = 24 * 60 * 60  # seconds


def brew_outdated(formula: str) -> tuple[str, str] | None:
    """(installed, available) when a formula is behind, else None.

    HOMEBREW_NO_AUTO_UPDATE keeps this from silently turning into a network
    sync of every formula — that can take a minute, and this runs behind a
    TUI. The index is refreshed by the user's own brew usage.
    """
    import os

    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
    try:
        proc = subprocess.run(
            ["brew", "outdated", "--json", formula],
            capture_output=True, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        entries = json.loads(proc.stdout or "{}").get("formulae", [])
    except ValueError:
        return None
    for entry in entries:
        if entry.get("name") == formula:
            if entry.get("pinned"):
                return None  # pinned on purpose; not ours to override
            installed = (entry.get("installed_versions") or ["?"])[0]
            return installed, entry.get("current_version", "?")
    return None


def read_update_cache() -> dict:
    try:
        return json.loads(UPDATE_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def write_update_cache(data: dict) -> None:
    try:
        UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_CACHE.write_text(json.dumps(data))
    except OSError:
        pass  # a cache we cannot write just means we check again next launch


def update_check_due(now: float, cache: dict | None = None) -> bool:
    """True when the last check is older than the interval (or never ran)."""
    cache = read_update_cache() if cache is None else cache
    return (now - cache.get("checked_at", 0)) >= UPDATE_CHECK_INTERVAL


async def upgrade_formula(formula: str, on_line=lambda _l: None) -> bool:
    """brew upgrade, then restart the service — both, always.

    `brew upgrade` leaves the running daemon on the old binary; because the
    old Cellar directory is deleted, the process keeps executing files that
    no longer exist and stays on stale indexer definitions indefinitely.
    Skipping the restart is how an "updated" Jackett stays un-updated.
    """
    rc = await run_streaming(["brew", "upgrade", formula], on_line)
    if rc != 0:
        return False
    # Only formulas we actually run as services need the restart, and a
    # failure here is not fatal — the new binary is installed either way.
    await run_streaming(["brew", "services", "restart", formula], on_line)
    return True


# ── ClamAV bootstrap (best-effort, never blocking) ───────────────────────────

def clamav_conf_bootstrap(etc: Path | None = None) -> str | None:
    """Give brew's clamav the config files it refuses to start without.

    Homebrew ships clamd.conf.sample / freshclam.conf.sample and leaves the
    activation to the user: copy them, drop the 'Example' guard line, give
    clamd a socket. Deterministic file edits, so the wizard does them.
    Returns a note describing what happened, or None if nothing was needed.
    Failures return a note too — AV setup must never sink onboarding.
    """
    if etc is None:
        try:
            prefix = subprocess.run(
                ["brew", "--prefix"], capture_output=True, text=True, timeout=15,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "brew not answering — ClamAV config left alone"
        etc = Path(prefix) / "etc" / "clamav"
    if not etc.is_dir():
        return f"no {etc} — is clamav installed?"

    made = []
    for name in ("clamd.conf", "freshclam.conf"):
        conf, sample = etc / name, etc / f"{name}.sample"
        if conf.exists() or not sample.exists():
            continue
        try:
            text = sample.read_text()
            # The sample's single active line is the word "Example", which
            # makes the daemon refuse to run the file as-is.
            text = re.sub(r"^Example$", "#Example", text, flags=re.M)
            if name == "clamd.conf":
                text = re.sub(
                    r"^#(LocalSocket .*)$", r"\1", text, count=1, flags=re.M,
                )
            conf.write_text(text)
            made.append(name)
        except OSError as e:
            return f"couldn't write {name}: {e}"
    return f"created {' and '.join(made)}" if made else None
