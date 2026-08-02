"""The Cloudflare solver: patching, layout, and the cookie hint.

The patches are the sharp edge here. trrnt downloads third-party code and
edits it before launching it as a subprocess, so the one thing these tests
guard hardest is that a patch which no longer fits fails loudly rather than
half-applying — a silently unpatched FlareSolverr would open a visible browser
window and reach for the user's own Chrome.
"""

import asyncio
import json
import socket
import time

import pytest

from trrnt import solver


# ── Patching ──────────────────────────────────────────────────────────────────

# The three anchors, in the shape v3.5.0 has them.
STOCK_UTILS = """\
import os
import undetected_chromedriver as uc


def get_webdriver(proxy=None):
    options = uc.ChromeOptions()
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--ignore-ssl-errors')

    windows_headless = False
    if get_config_headless():
        if os.name == 'nt':
            windows_headless = True
        else:
            start_xvfb_display()


def get_chrome_exe_path() -> str:
    global CHROME_EXE_PATH
    CHROME_EXE_PATH = uc.find_chrome_executable()
    return CHROME_EXE_PATH
"""


def test_patches_apply_to_the_pinned_shape(tmp_path):
    utils = tmp_path / "utils.py"
    utils.write_text(STOCK_UTILS)

    solver._apply_patches(utils)
    text = utils.read_text()

    assert "os.environ.get('CHROME_EXE_PATH')" in text
    assert "--window-position=-4000,-4000" in text
    assert "LocalNetworkAccessChecks" in text
    # The stock behaviour has to survive underneath: an unset override still
    # falls through to the system search, and Linux still gets Xvfb.
    assert "uc.find_chrome_executable()" in text
    assert "start_xvfb_display()" in text


def test_patching_twice_changes_nothing(tmp_path):
    """`trrnt setup` is re-runnable, so install is too."""
    utils = tmp_path / "utils.py"
    utils.write_text(STOCK_UTILS)

    solver._apply_patches(utils)
    once = utils.read_text()
    solver._apply_patches(utils)

    assert utils.read_text() == once


def test_a_moved_anchor_refuses_to_patch(tmp_path):
    """Upstream drifting must not yield a half-patched solver."""
    utils = tmp_path / "utils.py"
    utils.write_text(STOCK_UTILS.replace("            start_xvfb_display()", "            pass"))

    with pytest.raises(solver.SolverError, match="does not look the way trrnt expects"):
        solver._apply_patches(utils)


def test_an_ambiguous_anchor_refuses_to_patch(tmp_path):
    utils = tmp_path / "utils.py"
    utils.write_text(STOCK_UTILS + "\n" + STOCK_UTILS)

    with pytest.raises(solver.SolverError):
        solver._apply_patches(utils)


# ── Layout ────────────────────────────────────────────────────────────────────

def test_platform_names_match_chrome_for_testing(monkeypatch):
    monkeypatch.setattr(solver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(solver.platform, "machine", lambda: "arm64")
    assert solver.cft_platform() == "mac-arm64"

    monkeypatch.setattr(solver.platform, "machine", lambda: "x86_64")
    assert solver.cft_platform() == "mac-x64"

    monkeypatch.setattr(solver.platform, "system", lambda: "Linux")
    assert solver.cft_platform() == "linux64"


def test_unsupported_platform_is_an_error(monkeypatch):
    monkeypatch.setattr(solver.platform, "system", lambda: "Windows")
    with pytest.raises(solver.SolverError):
        solver.cft_platform()


def _fake_mac_chrome(root):
    """The layout Chrome for Testing's mac zip actually unpacks to."""
    binary = (
        root / "chrome-mac-arm64" / "Google Chrome for Testing.app"
        / "Contents" / "MacOS" / "Google Chrome for Testing"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_chrome_is_found_inside_the_app_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(solver, "CHROME_DIR", tmp_path)
    expected = _fake_mac_chrome(tmp_path)

    assert solver.chrome_binary() == expected


def test_a_non_executable_browser_does_not_count(tmp_path, monkeypatch):
    """An extract that lost the exec bit is broken, not installed."""
    monkeypatch.setattr(solver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(solver, "CHROME_DIR", tmp_path)
    _fake_mac_chrome(tmp_path).chmod(0o644)

    assert solver.chrome_binary() is None


def test_half_an_install_reads_as_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(solver, "CHROME_DIR", tmp_path / "chrome")
    monkeypatch.setattr(solver, "SRC_DIR", tmp_path / "src")
    monkeypatch.setattr(solver, "VENV_DIR", tmp_path / "venv")
    monkeypatch.setattr(solver, "MANIFEST", tmp_path / "manifest.json")

    (tmp_path / "manifest.json").write_text("{}")
    assert not solver.installed()

    (tmp_path / "src" / "src").mkdir(parents=True)
    (tmp_path / "src" / "src" / "flaresolverr.py").write_text("")
    assert not solver.installed()  # no venv, no browser

    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").write_text("")
    assert not solver.installed()  # still no browser

    (tmp_path / "chrome").mkdir()
    _fake_mac_chrome(tmp_path / "chrome")
    assert solver.installed()


# ── Port selection ────────────────────────────────────────────────────────────

def test_preferred_port_is_used_when_free():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert solver._free_port(free) == free


def test_a_taken_port_is_stepped_around():
    """Someone else's FlareSolverr on 8191 must not break ours."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]

        chosen = solver._free_port(taken)

        assert chosen != taken
        assert chosen > 0


# ── Cookie age hint ───────────────────────────────────────────────────────────

def _write_jackett_indexer(home, indexer_id, cookieheader):
    d = home / "Library" / "Application Support" / "Jackett" / "Indexers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{indexer_id}.json").write_text(json.dumps({
        "list": [
            {"id": "sitelink", "value": "https://1337x.to/"},
            {"id": "cookieheader", "value": cookieheader},
            {"id": "lasterror", "value": None},
        ]
    }))


def test_cookie_age_reads_the_embedded_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.Path, "home", staticmethod(lambda: tmp_path))
    issued = int(time.time()) - 600
    _write_jackett_indexer(
        tmp_path, "1337x", f"cf_clearance=abc.def-{issued}-1.2.1.1-xyz",
    )

    age = solver.cookie_age_seconds("1337x")

    assert age is not None
    assert 590 <= age <= 610


def test_no_cookie_means_no_age(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.Path, "home", staticmethod(lambda: tmp_path))
    _write_jackett_indexer(tmp_path, "thepiratebay", "")

    assert solver.cookie_age_seconds("thepiratebay") is None


def test_an_unknown_indexer_has_no_age(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.Path, "home", staticmethod(lambda: tmp_path))

    assert solver.cookie_age_seconds("nope") is None


# ── Priming ───────────────────────────────────────────────────────────────────

class FakeAdmin:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.tested = []

    async def test_indexer(self, indexer_id):
        self.tested.append(indexer_id)
        return self.verdicts.get(indexer_id, ("ok", ""))


def test_prime_tests_every_indexer_and_reports_each():
    admin = FakeAdmin({"1337x": ("cloudflare", "needs FlareSolverr")})
    seen = []

    results = asyncio.run(
        solver.prime(admin, ["1337x", "thepiratebay"], on_result=seen.append)
    )

    assert sorted(admin.tested) == ["1337x", "thepiratebay"]
    assert ("1337x", "cloudflare", "needs FlareSolverr") in results
    assert ("thepiratebay", "ok", "") in results
    # Callers stream progress; a slow prime must report as it goes.
    assert len(seen) == 2


def test_prime_of_nothing_is_not_an_error():
    assert asyncio.run(solver.prime(FakeAdmin({}), [])) == []


# ── Launch guards ─────────────────────────────────────────────────────────────

def test_running_an_uninstalled_solver_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(solver, "installed", lambda: False)

    async def go():
        async with solver.SolverProcess():
            pass

    with pytest.raises(solver.SolverError, match="not installed"):
        asyncio.run(go())
