"""The onboarding path a developer machine cannot test: a real fresh install.

Everything here needs a real Jackett, so it is opt-in — set TRRNT_LIVE=1 (CI
does). The unit tests cover logic against mocks; these cover the two things a
mock can never tell us:

  * whether Jackett still behaves the way the wizard assumes (its admin API
    is the one its web UI uses, with no stability contract), and
  * whether CURATED_PUBLIC still matches real catalog ids — two entries had
    silently never appeared before a hand-run diff caught them.

The live Jackett is expected on TRRNT_LIVE_JACKETT (default :9118), and it
must be a throwaway: test_adds_indexers_for_real writes to it.

Sync tests driving asyncio.run, matching the rest of the suite — the project
has no pytest-asyncio and does not need one for this.
"""

import asyncio
import os

import pytest

from trrnt.onboard import (
    CURATED_PUBLIC,
    JackettAdmin,
    order_catalog,
    read_jackett_server_config,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("TRRNT_LIVE") != "1",
    reason="live Jackett required; set TRRNT_LIVE=1",
)

JACKETT = os.environ.get("TRRNT_LIVE_JACKETT", "http://localhost:9118")


def _with_admin(body):
    """Run body(admin) against a logged-in live Jackett."""
    async def go():
        admin = JackettAdmin(JACKETT)
        await admin.login()
        try:
            return await body(admin)
        finally:
            await admin.close()
    return asyncio.run(go())


def test_login_and_catalog():
    async def body(admin):
        return await admin.catalog()

    catalog = _with_admin(body)
    # The real catalog is ~600 indexers; a handful means we are looking at
    # something other than a working Jackett.
    assert len(catalog) > 100, f"suspiciously small catalog: {len(catalog)}"
    assert any(i.get("type") == "public" for i in catalog)


def test_every_curated_id_exists_in_the_catalog():
    """The check that would have caught torrentgalaxy/solidtorrents.

    A curated id that no longer matches the catalog does not error — it just
    quietly stops being offered, which is the kind of rot that survives for
    releases. Fail loudly instead.
    """
    async def body(admin):
        return {i["id"] for i in await admin.catalog()}

    ids = _with_admin(body)
    missing = [c for c in CURATED_PUBLIC if c not in ids]
    assert not missing, (
        f"CURATED_PUBLIC ids absent from Jackett's catalog: {missing} — "
        "they were renamed or removed upstream and are silently never offered"
    )


def test_quickpick_offers_only_public_indexers():
    async def body(admin):
        return order_catalog(await admin.catalog())

    ordered = _with_admin(body)
    assert ordered, "no public indexers offered"
    assert all(i.get("type") == "public" for i in ordered)
    # Curated entries lead the list, so a first run's defaults are the
    # popular ones rather than whatever sorts first alphabetically.
    lead = [i["id"] for i in ordered[: len(CURATED_PUBLIC)]]
    assert set(lead) & set(CURATED_PUBLIC), "curated picks are not leading"


def test_jackett_minted_its_own_api_key():
    """The step that lets onboarding skip 'copy the key from the web UI'."""
    server = read_jackett_server_config()
    if server is None:
        pytest.skip("Jackett's config is not on this machine (remote instance)")
    assert server["APIKey"], "Jackett started but minted no API key"
    assert len(server["APIKey"]) > 10


def test_adds_indexers_for_real():
    """A real POST to a real Jackett — the mock only proves our side."""
    async def body(admin):
        ids = {i["id"] for i in await admin.catalog()}
        target = next((c for c in CURATED_PUBLIC if c in ids), None)
        assert target, "no curated indexer available to add"
        before = await admin.configured_ids()
        await admin.add_indexer(target)
        return target, before, await admin.configured_ids()

    target, before, after = _with_admin(body)
    assert target in after, (
        f"{target} was not configured after add_indexer "
        f"(before={sorted(before)}, after={sorted(after)})"
    )
