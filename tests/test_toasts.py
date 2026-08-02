"""Toast placement and badging.

Textual puts toasts in the top-right corner, where a 44-column box sat across
the Name column of every download for the life of the message. They live at the
bottom centre now, over the foot of the downloads panel, and carry their
severity as an emoji so the type reads before the words do.
"""

import asyncio
import sys

from textual.widgets._toast import Toast

from trrnt.tui import _TOAST_EMOJI

sys.path.insert(0, "tests")
from test_search_placeholder import _app  # noqa: E402

SIZE = (110, 40)


async def _toast(app, message, severity="information"):
    """Show one toast and hand back the widget and the screen it sits on."""
    async with app.run_test(size=SIZE, notifications=True) as pilot:
        app.notify(message, severity=severity)
        for _ in range(4):
            await pilot.pause()
        toast = app.query_one(Toast)
        return str(toast.render()), toast.region, app.screen.region, toast.classes


# ── badging ───────────────────────────────────────────────────────────────────

def test_every_severity_is_badged():
    for severity, emoji in _TOAST_EMOJI.items():
        rendered, _, _, classes = asyncio.run(
            _toast(_app(), "Skipping 1337x", severity=severity))
        assert rendered.startswith(emoji), f"{severity} toast was not badged"
        assert f"-{severity}" in classes, "severity must still reach the CSS"
        assert "Skipping 1337x" in rendered, "the message itself was mangled"


def test_the_badges_are_all_the_same_width():
    """Emoji that need a variation selector are one cell in some terminals and
    two in others, which leaves the text ragged from toast to toast."""
    from rich.cells import cell_len
    assert len({cell_len(e) for e in _TOAST_EMOJI.values()}) == 1


def test_a_screen_toast_is_badged_too():
    """Widget.notify defers to the app, so the wizard's toasts come through
    the same override — worth pinning, since it is why this is not done at
    the call sites."""
    async def go():
        app = _app()
        async with app.run_test(size=SIZE, notifications=True) as pilot:
            app.screen.notify("Jackett is not answering", severity="error")
            for _ in range(4):
                await pilot.pause()
            return str(app.query_one(Toast).render())

    assert asyncio.run(go()).startswith(_TOAST_EMOJI["error"])


# ── placement ─────────────────────────────────────────────────────────────────

def test_the_toast_is_centred():
    _, toast, screen, _ = asyncio.run(_toast(_app(), "Download added"))
    left = toast.x
    right = screen.width - (toast.x + toast.width)
    assert abs(left - right) <= 1, f"{left} columns of margin against {right}"


def test_the_toast_clears_the_key_and_info_bars():
    """Two docked bars and the table's own bottom margin: the toast has to
    land on the panel, not on the chrome under it."""
    _, toast, screen, _ = asyncio.run(_toast(_app(), "Download added"))
    assert toast.bottom <= screen.height - 3


def test_the_toast_does_not_reach_the_results_panel():
    """It is bottom-anchored, so a long message grows up the downloads panel.
    If it ever reaches the results it is covering the thing being searched."""
    long = ("Skipping torrentgalaxy, kickasstorrents and limetorrents — "
            "press ^y to repair them, or ^n to test or exclude indexers")
    _, toast, _, _ = asyncio.run(_toast(_app(), long, severity="warning"))
    downloads_top = 21  # header, search box, results panel and its label
    assert toast.y >= downloads_top, "the toast climbed into the results"


# ── the searching toast ───────────────────────────────────────────────────────

def test_running_a_search_no_longer_toasts():
    """The placeholder rows and the spinner say it, without covering the
    downloads panel on every single press of enter."""
    async def go():
        app = _app()
        toasts = []
        async with app.run_test(size=SIZE, notifications=True) as pilot:
            app.notify = lambda msg, **kw: toasts.append(msg)

            async def search(query, **kw):
                return []

            app.jackett.search = search
            await app.run_search.__wrapped__(app, "super mario galaxy")
            await pilot.pause()
        return toasts

    assert asyncio.run(go()) == []
