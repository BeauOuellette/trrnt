#!/usr/bin/env python3
"""Drive the real setup wizard against a real Jackett, end to end.

This is the check the unit tests structurally cannot make: they run the
wizard's steps against an httpx MockTransport, which proves our side of the
conversation and nothing about Jackett's. Here the steps run unmodified
against a live instance, and the assertions are made against *Jackett* and a
*real search*, not against the wizard's own return values.

Used by .github/workflows/onboarding.yml on a fresh macOS runner. Runs
locally too:

    scripts/sandbox-setup.sh --reset config >/dev/null   # start a throwaway
    TRRNT_LIVE_JACKETT=http://localhost:9118 python scripts/ci_wizard_e2e.py

The target Jackett must be disposable — this configures indexers on it.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from textual.widgets import SelectionList

from trrnt.config import Config
from trrnt.onboard import JackettAdmin
from trrnt.search import JackettSearch
from trrnt.tui import SetupScreen, TGetApp

JACKETT = os.environ.get("TRRNT_LIVE_JACKETT", "http://localhost:9118")
# A query with results on essentially any public tracker, and legal to name.
SEARCH_TERM = os.environ.get("TRRNT_LIVE_QUERY", "ubuntu")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


async def wait_for_ask(pilot, screen, timeout=30) -> bool:
    """Wait until a step parks on its answer Future."""
    deadline = time.monotonic() + timeout
    while screen._answer is None and time.monotonic() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.05)
    return screen._answer is not None


async def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="trrnt-e2e-"))
    config_path = sandbox / "config.yaml"
    config = Config(config_path)

    app = TGetApp(config)
    # Mount-time workers shell out and are not what this exercises.
    app.check_clamav_status = lambda *a, **k: None
    app.check_vpn_status = lambda *a, **k: None
    app.refresh_downloads_loop = lambda *a, **k: None
    app.push_home_screen = lambda *a, **k: None
    app._run_kill_switch = lambda *a, **k: None

    print(f"jackett: {JACKETT}\nsandbox: {sandbox}\n")

    async with app.run_test(size=(110, 40)) as pilot:
        screen = SetupScreen()  # no injection: the real JackettAdmin
        screen.run_wizard = lambda *a, **k: None
        app.push_screen(screen)
        await pilot.pause()

        config.ensure_config_exists()
        config.reload()

        print("step: api_key")
        # The runner's Jackett lives in a temp data folder that
        # read_jackett_server_config() cannot know about, so point the
        # config at it directly — the auto-read itself is covered by
        # test_jackett_minted_its_own_api_key in the live suite.
        from trrnt.onboard import write_config_values

        write_config_values(config_path, {("jackett", "url"): JACKETT})
        config.reload()
        text = config_path.read_text()
        check("config keeps its comments", "#" in text)
        check("url written", JACKETT in text)

        print("step: indexers")
        task = asyncio.ensure_future(screen._step_indexers())
        if not await wait_for_ask(pilot, screen):
            check("quick-pick appeared", False, "step never asked for input")
            task.cancel()
            return 1
        picker = screen.query_one(SelectionList)
        preselected = list(picker.selected)
        check("quick-pick populated", picker.option_count > 50,
              f"{picker.option_count} rows")
        check("curated picks preselected", len(preselected) >= 5,
              f"{len(preselected)}: {preselected}")
        screen._reply(("button", "setup-add"))
        mark = await asyncio.wait_for(task, timeout=180)
        check("indexer step succeeded", mark == "✓", f"mark={mark}")

    # Ask Jackett, not the wizard.
    admin = JackettAdmin(JACKETT)
    try:
        await admin.login()
        configured = await admin.configured_ids()
    finally:
        await admin.close()
    check("jackett reports them configured", len(configured) >= 5,
          f"{sorted(configured)}")

    # The real proof: a search driven entirely by the wizard-written config.
    config.reload()
    search = JackettSearch(config.get("jackett"))
    check("search connects", await search.check_connection())
    results = await search.search(SEARCH_TERM, max_results=5)
    # Public trackers go down; an empty result set is a warning, not a
    # failure, or this job fails for reasons that are nobody's bug.
    if results:
        check(f"search returned results for {SEARCH_TERM!r}", True,
              f"{len(results)} hits, top: {results[0].title[:50]!r}")
    else:
        print(f"  WARN  search for {SEARCH_TERM!r} returned nothing "
              "(public trackers may be down; not failing on it)")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::live search for {SEARCH_TERM!r} returned no results")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
