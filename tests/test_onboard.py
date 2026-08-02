"""Onboarding logic: the YAML writer, Jackett discovery, and the admin API.

The admin API tests run against a mini-Jackett built on httpx.MockTransport
that reproduces the auth behavior verified against a live 0.22 instance:
API calls redirect to /UI/Login until the dashboard has handed out a session
cookie. If Jackett ever changes that dance, these tests keep passing — the
wizard's open-the-UI fallback is what covers real-world drift.
"""

import asyncio
import json

import httpx
import pytest

from trrnt.onboard import (
    JackettAdmin,
    JackettAdminError,
    clamav_conf_bootstrap,
    jackett_url,
    order_catalog,
    read_jackett_server_config,
    set_yaml_values,
)


# ── set_yaml_values ───────────────────────────────────────────────────────────

CONFIG = """\
# trrnt configuration

jackett:
  # Find this in the Jackett UI
  api_key: ""
  url: "http://localhost:9117"

vpn:
  # Refuse to download without a tunnel
  enabled: true

plex:
  enabled: true
"""


def test_sets_value_in_place_and_keeps_comments():
    out = set_yaml_values(CONFIG, {("jackett", "api_key"): "abc123"})
    assert '  api_key: "abc123"' in out
    assert "# Find this in the Jackett UI" in out
    assert "# trrnt configuration" in out
    assert '  url: "http://localhost:9117"' in out


def test_same_key_name_only_touches_the_named_section():
    out = set_yaml_values(CONFIG, {("vpn", "enabled"): False})
    vpn_part = out[out.index("vpn:"):out.index("plex:")]
    plex_part = out[out.index("plex:"):]
    assert "enabled: false" in vpn_part
    assert "enabled: true" in plex_part


def test_missing_key_is_inserted_under_its_section():
    out = set_yaml_values(CONFIG, {("vpn", "interface_prefix"): "utun"})
    vpn_part = out[out.index("vpn:"):out.index("plex:")]
    assert '  interface_prefix: "utun"' in vpn_part


def test_missing_section_is_appended():
    out = set_yaml_values(CONFIG, {("aria2", "bt_interface"): "none"})
    assert out.rstrip().endswith('aria2:\n  bt_interface: "none"')


def test_multiple_values_in_one_pass():
    out = set_yaml_values(CONFIG, {
        ("jackett", "api_key"): "k",
        ("vpn", "enabled"): False,
    })
    assert '  api_key: "k"' in out
    assert "  enabled: false" in out


# ── Jackett discovery ─────────────────────────────────────────────────────────

def test_reads_server_config_from_either_layout(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    linux = tmp_path / ".config" / "Jackett"
    linux.mkdir(parents=True)
    (linux / "ServerConfig.json").write_text(
        json.dumps({"APIKey": "zzz", "Port": 9117})
    )
    cfg = read_jackett_server_config()
    assert cfg and cfg["APIKey"] == "zzz"
    assert jackett_url(cfg) == "http://localhost:9117"


def test_blank_api_key_reads_as_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    mac = tmp_path / "Library" / "Application Support" / "Jackett"
    mac.mkdir(parents=True)
    (mac / "ServerConfig.json").write_text(json.dumps({"APIKey": "", "Port": 9117}))
    assert read_jackett_server_config() is None


def test_jackett_url_default_port():
    assert jackett_url(None) == "http://localhost:9117"
    assert jackett_url({"Port": 9200}) == "http://localhost:9200"


# ── The admin API against a mini-Jackett ─────────────────────────────────────

class MiniJackett:
    """Just enough of Jackett's UI API: cookie gate, catalog, config posts."""

    def __init__(self, password=None, cloudflare=(), broken=()):
        self.password = password
        self.cloudflare = set(cloudflare)
        self.broken = set(broken)
        self.saved_configs = {}
        self.catalog = [
            {"id": "1337x", "name": "1337x", "type": "public", "configured": True},
            {"id": "eztv", "name": "EZTV", "type": "public", "configured": False},
            {"id": "zeta", "name": "Zeta", "type": "public", "configured": False},
            {"id": "alpha", "name": "Alpha", "type": "public", "configured": False},
            {"id": "priv", "name": "Priv", "type": "private", "configured": False},
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        authed = request.headers.get("cookie", "").find("Jackett=ok") >= 0

        if path == "/UI/Dashboard":
            if request.method == "POST":
                if request.url.params or self.password is None:
                    return httpx.Response(400)
                body = request.read().decode()
                if f"password={self.password}" in body:
                    return httpx.Response(
                        302, headers={"location": "/UI/Dashboard",
                                      "set-cookie": "Jackett=ok; path=/"})
                return httpx.Response(302, headers={"location": "/UI/Login"})
            if self.password and not authed:
                return httpx.Response(302, headers={"location": "/UI/Login"})
            return httpx.Response(
                200, text="<dashboard>",
                headers={"set-cookie": "Jackett=ok; path=/"})

        if path == "/UI/Login":
            return httpx.Response(200, text="<login>")

        if not authed:
            return httpx.Response(
                302, headers={"location": f"/UI/Login?ReturnUrl={path}"})

        if path == "/api/v2.0/indexers":
            return httpx.Response(200, json=self.catalog)
        if path.startswith("/api/v2.0/indexers/") and path.endswith("/test"):
            idx = path.split("/")[4]
            # Mirrors the live shapes: 204 for a working indexer, a JSON
            # error body otherwise (Cloudflare included).
            if idx in self.cloudflare:
                return httpx.Response(500, json={
                    "result": "error",
                    "error": f"Exception ({idx}): Challenge detected but "
                             "FlareSolverr is not configured",
                })
            if idx in self.broken:
                return httpx.Response(500, json={
                    "result": "error", "error": f"Exception ({idx}): site down",
                })
            return httpx.Response(204)
        if path.startswith("/api/v2.0/indexers/") and path.endswith("/config"):
            idx = path.split("/")[4]
            if request.method == "GET":
                return httpx.Response(
                    200, json=[{"id": "sitelink", "value": f"https://{idx}.example/"}])
            self.saved_configs[idx] = json.loads(request.read())
            return httpx.Response(204)

        return httpx.Response(404)


def _admin(mini):
    return JackettAdmin(
        "http://localhost:9117", transport=httpx.MockTransport(mini.handler))


def test_api_calls_before_login_fail_cleanly():
    async def go():
        admin = _admin(MiniJackett())
        try:
            with pytest.raises(JackettAdminError):
                await admin.catalog()
        finally:
            await admin.close()
    asyncio.run(go())


def test_login_then_catalog():
    async def go():
        admin = _admin(MiniJackett())
        try:
            await admin.login()
            ids = {i["id"] for i in await admin.catalog()}
            assert "1337x" in ids and "priv" in ids
            assert await admin.configured_ids() == {"1337x"}
        finally:
            await admin.close()
    asyncio.run(go())


def test_password_login_posts_then_gets():
    async def go():
        mini = MiniJackett(password="hunter2")
        admin = _admin(mini)
        try:
            with pytest.raises(JackettAdminError):
                await admin.login()          # password required
            await admin.login("hunter2")     # now the cookie exists
            assert len(await admin.catalog()) == 5
        finally:
            await admin.close()
    asyncio.run(go())


def test_add_indexer_posts_the_template_back():
    async def go():
        mini = MiniJackett()
        admin = _admin(mini)
        try:
            await admin.login()
            await admin.add_indexer("eztv")
            assert mini.saved_configs["eztv"] == [
                {"id": "sitelink", "value": "https://eztv.example/"}]
        finally:
            await admin.close()
    asyncio.run(go())


def test_order_catalog_curates_then_alphabetizes():
    ordered = order_catalog(MiniJackett().catalog)
    assert [i["id"] for i in ordered] == ["1337x", "eztv", "alpha", "zeta"]


# ── ClamAV conf bootstrap ─────────────────────────────────────────────────────

SAMPLE = """\
## Example config
Example
#LocalSocket /tmp/clamd.socket
#TCPSocket 3310
"""


def test_clamav_bootstrap_activates_samples(tmp_path):
    (tmp_path / "clamd.conf.sample").write_text(SAMPLE)
    (tmp_path / "freshclam.conf.sample").write_text("Example\n#DatabaseMirror x\n")
    note = clamav_conf_bootstrap(etc=tmp_path)
    assert note == "created clamd.conf and freshclam.conf"
    clamd = (tmp_path / "clamd.conf").read_text()
    assert "\n#Example\n" in "\n" + clamd
    assert "\nLocalSocket /tmp/clamd.socket" in clamd
    assert "#TCPSocket" in clamd  # only the first socket line is activated
    fresh = (tmp_path / "freshclam.conf").read_text()
    assert fresh.startswith("#Example")


def test_clamav_bootstrap_leaves_existing_confs_alone(tmp_path):
    (tmp_path / "clamd.conf.sample").write_text(SAMPLE)
    (tmp_path / "clamd.conf").write_text("MINE")
    assert clamav_conf_bootstrap(etc=tmp_path) is None
    assert (tmp_path / "clamd.conf").read_text() == "MINE"


# ── brew update checking ─────────────────────────────────────────────────────

import subprocess as _subprocess

from trrnt import onboard as _onboard
from trrnt.onboard import brew_outdated, update_check_due


def _fake_brew(payload, monkeypatch, returncode=0):
    def run(cmd, **kwargs):
        return _subprocess.CompletedProcess(cmd, returncode, stdout=payload, stderr="")
    monkeypatch.setattr(_onboard.subprocess, "run", run)


def test_brew_outdated_reports_versions(monkeypatch):
    _fake_brew(json.dumps({"formulae": [
        {"name": "jackett", "installed_versions": ["0.24.1385"],
         "current_version": "0.24.2307", "pinned": False}
    ], "casks": []}), monkeypatch)
    assert brew_outdated("jackett") == ("0.24.1385", "0.24.2307")


def test_brew_outdated_none_when_current(monkeypatch):
    _fake_brew(json.dumps({"formulae": [], "casks": []}), monkeypatch)
    assert brew_outdated("jackett") is None


def test_pinned_formula_is_left_alone(monkeypatch):
    """A pin is a deliberate choice; offering to undo it would be wrong."""
    _fake_brew(json.dumps({"formulae": [
        {"name": "aria2", "installed_versions": ["1.0"],
         "current_version": "2.0", "pinned": True}
    ], "casks": []}), monkeypatch)
    assert brew_outdated("aria2") is None


def test_brew_garbage_does_not_raise(monkeypatch):
    _fake_brew("not json at all", monkeypatch)
    assert brew_outdated("jackett") is None


def test_update_check_interval(monkeypatch):
    now = 1_000_000.0
    assert update_check_due(now, {}) is True
    assert update_check_due(now, {"checked_at": now - 60}) is False
    assert update_check_due(now, {"checked_at": now - 25 * 3600}) is True


# ── indexer health ────────────────────────────────────────────────────────────

def test_indexer_test_classifies_each_outcome():
    """Adding an indexer never touches the site; only a test does.

    Cloudflare is the case that matters: it looks like a successful add and
    then silently returns nothing, so it must be distinguishable from both
    a working indexer and an ordinary outage.
    """
    async def go():
        mini = MiniJackett(cloudflare={"1337x"}, broken={"zeta"})
        admin = _admin(mini)
        try:
            await admin.login()
            return (
                await admin.test_indexer("eztv"),
                await admin.test_indexer("1337x"),
                await admin.test_indexer("zeta"),
            )
        finally:
            await admin.close()

    ok, blocked, broken = asyncio.run(go())
    assert ok == ("ok", "")
    assert blocked[0] == "cloudflare"
    assert "FlareSolverr" in blocked[1]
    assert broken[0] == "error"


def test_curated_cloudflare_picks_are_not_default_on():
    """A first run with no FlareSolverr should end with working indexers."""
    from trrnt.onboard import CURATED_PUBLIC, NEEDS_SOLVER

    default_on = [c for c in CURATED_PUBLIC if c not in NEEDS_SOLVER]
    assert len(default_on) >= 5, "too few indexers work without a solver"
    assert NEEDS_SOLVER < set(CURATED_PUBLIC), (
        "NEEDS_SOLVER lists ids that are not offered at all")
    # The solver-needing ones sort last, so the quick-pick leads with
    # indexers that answer.
    ranks = [CURATED_PUBLIC.index(c) for c in NEEDS_SOLVER]
    assert min(ranks) > max(CURATED_PUBLIC.index(c) for c in default_on)
