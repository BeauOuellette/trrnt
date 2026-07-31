"""Every binding must be pressable, reachable, and actually do something.

Two bindings shipped that could never fire: ctrl+i and ctrl+h are the ASCII
codes for Tab and Backspace, so the terminal hands the app those keys instead
and the action is never reached. Nothing errors — the key simply does nothing,
which is exactly how it went unnoticed.
"""

import asyncio

import pytest

from torrentcli.tui import (
    UNDELIVERABLE_KEYS,
    InspectScreen,
    KeysScreen,
    TGetApp,
)

from test_downloads_table import _app, _result


ALL_SCREENS = [TGetApp, InspectScreen, KeysScreen]


# ── pressable ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("owner", ALL_SCREENS, ids=lambda c: c.__name__)
def test_no_binding_uses_a_key_the_terminal_cannot_deliver(owner):
    for binding in owner.BINDINGS:
        collision = UNDELIVERABLE_KEYS.get(binding.key)
        assert collision is None, (
            f"{owner.__name__}: {binding.key} ({binding.description}) can never "
            f"fire — the terminal sends {collision} for those bytes"
        )


def test_bindings_do_not_collide_with_each_other():
    seen = {}
    for b in TGetApp.BINDINGS:
        assert b.key not in seen, (
            f"{b.key} is bound to both {seen.get(b.key)} and {b.description}")
        seen[b.key] = b.description


# ── wired ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("owner", ALL_SCREENS, ids=lambda c: c.__name__)
def test_every_binding_has_the_action_it_names(owner):
    for binding in owner.BINDINGS:
        assert hasattr(owner, f"action_{binding.action}"), (
            f"{owner.__name__}: {binding.key} calls action_{binding.action}, "
            f"which does not exist")


# ── actually fires ────────────────────────────────────────────────────────────

def _press(key, action, focus):
    """Press a key with `focus` focused; report whether its action ran."""
    async def go():
        app = _app()
        fired = []
        async with app.run_test(size=(102, 30)) as pilot:
            app.search_results = [_result("Some Release 1080p x265-GRP")]
            app._render_results()
            await app._update_downloads()
            setattr(app, f"action_{action}", lambda *a, **k: fired.append(action))
            try:
                app.query_one(focus).focus()
            except Exception:
                pass
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
        return bool(fired)
    return asyncio.run(go())


@pytest.mark.parametrize(
    "key,action",
    [(b.key, b.action) for b in TGetApp.BINDINGS],
    ids=[b.description.replace(" ", "-") for b in TGetApp.BINDINGS],
)
@pytest.mark.parametrize("focus", ["#results-table", "#search-input"])
def test_binding_fires_from_either_pane(key, action, focus):
    """A binding shadowed by the focused widget is as dead as an unbound one —
    an Input eats most plain keys, which is why these are priority bindings."""
    assert _press(key, action, focus), (
        f"{key} did not reach action_{action} with {focus} focused")


# ── observable ────────────────────────────────────────────────────────────────

def _effect(key, check, focus="#results-table"):
    async def go():
        app = _app()
        async with app.run_test(size=(102, 30)) as pilot:
            app.search_results = [_result("Some Release 1080p x265-GRP")]
            app._render_results()
            await app._update_downloads()
            try:
                app.query_one(focus).focus()
            except Exception:
                pass
            await pilot.pause()
            await pilot.press(key)
            for _ in range(3):
                await pilot.pause()
            return check(app)
    return asyncio.run(go())


def _stack(app):
    return [type(s).__name__ for s in app.screen_stack]


def test_inspect_opens_the_detail_modal():
    """The binding the user reported dead. It was, but only in a terminal."""
    assert "InspectScreen" in _effect("ctrl+e", _stack)


def test_keys_opens_the_overlay():
    assert "KeysScreen" in _effect("ctrl+k", _stack)


def test_select_all_marks_every_row():
    assert _effect("ctrl+a", lambda a: sorted(a.selected_indices)) == [0]


def test_search_moves_focus_to_the_input():
    assert _effect("ctrl+s", lambda a: a.focused.id) == "search-input"


def test_health_reports_through_notifications():
    """It writes one line to the info bar but notifies for every download, so
    the output survives a bar only tall enough for one line."""
    def check(app):
        return len(app._notifications) > 0
    assert _effect("ctrl+g", check)


# ── force reconnect ───────────────────────────────────────────────────────────

def test_reconnect_pauses_then_resumes_every_active_download():
    """aria2 has no force-announce RPC, so pause-then-resume is the only way
    to shed stale peers and pull a fresh set."""
    calls = []

    class Recording:
        download_dir = "/tmp"
        async def get_active(self):
            from torrentcli.download import DownloadStatus
            return [DownloadStatus(gid="a", status="active"),
                    DownloadStatus(gid="b", status="active")]
        async def get_waiting(self, *a, **k): return []
        async def get_stopped(self, *a, **k): return []
        async def get_global_stat(self): return {"downloadSpeed": "0", "uploadSpeed": "0"}
        async def get_files(self, gid): return []
        async def pause(self, gid): calls.append(("pause", gid))
        async def unpause(self, gid): calls.append(("unpause", gid))

    async def go():
        app = _app()
        app.aria2 = Recording()
        async with app.run_test(size=(102, 30)) as pilot:
            await app.action_force_reconnect().wait()
        return calls

    got = asyncio.run(go())
    assert got == [("pause", "a"), ("pause", "b"),
                   ("unpause", "a"), ("unpause", "b")], got
    # every pause must precede every unpause, or peers are never actually shed
    assert [c[0] for c in got] == ["pause", "pause", "unpause", "unpause"]


def test_reconnect_says_so_when_there_is_nothing_to_reconnect():
    class Empty:
        download_dir = "/tmp"
        async def get_active(self): return []
        async def get_waiting(self, *a, **k): return []
        async def get_stopped(self, *a, **k): return []
        async def get_global_stat(self): return {"downloadSpeed": "0", "uploadSpeed": "0"}
        async def get_files(self, gid): return []

    async def go():
        app = _app()
        app.aria2 = Empty()
        async with app.run_test(size=(102, 30)) as pilot:
            await app.action_force_reconnect().wait()
            for _ in range(3):
                await pilot.pause()
            return [str(n.message) for n in app._notifications]

    assert any("Nothing downloading" in m for m in asyncio.run(go()))


def test_reconnect_is_in_the_footer_right_of_download():
    from torrentcli.tui import _FOOTER_LEFT
    assert _FOOTER_LEFT.index("force_reconnect") == \
           _FOOTER_LEFT.index("download_selected") + 1


def test_the_old_refresh_binding_is_gone():
    """It only re-checked the VPN despite being labelled Refresh, and the
    table already redraws every two seconds."""
    assert not hasattr(TGetApp, "action_refresh_downloads")
    assert "refresh_downloads" not in {b.action for b in TGetApp.BINDINGS}


# ── a failed reconnect must never strand a download ───────────────────────────

class _Aria2:
    """Configurable fake: choose which calls blow up."""
    download_dir = "/tmp"

    def __init__(self, gids=("a", "b"), pause_fails=(), unpause_fails=()):
        from torrentcli.download import DownloadStatus
        self._active = [DownloadStatus(gid=g, status="active") for g in gids]
        self.pause_fails, self.unpause_fails = set(pause_fails), set(unpause_fails)
        self.paused, self.resumed, self.unpause_all_called = [], [], 0

    async def get_active(self): return list(self._active)
    async def get_waiting(self, *a, **k): return []
    async def get_stopped(self, *a, **k): return []
    async def get_global_stat(self): return {"downloadSpeed": "0", "uploadSpeed": "0"}
    async def get_files(self, gid): return []

    async def pause(self, gid):
        if gid in self.pause_fails:
            raise RuntimeError(f"cannot pause {gid}")
        self.paused.append(gid)

    async def unpause(self, gid):
        if gid in self.unpause_fails:
            raise RuntimeError(f"cannot unpause {gid}")
        self.resumed.append(gid)

    async def unpause_all(self):
        self.unpause_all_called += 1
        self.resumed.extend(g for g in self.paused if g not in self.resumed)


def _reconnect(aria2):
    async def go():
        app = _app()
        app.aria2 = aria2
        async with app.run_test(size=(102, 30)) as pilot:
            await app.action_force_reconnect().wait()
            for _ in range(3):
                await pilot.pause()
            return [str(n.message) for n in app._notifications]
    return asyncio.run(go())


def test_everything_paused_is_resumed_when_a_pause_fails_midway():
    """The failure that stranded a real download: the first version returned
    early on error, leaving whatever it had already paused paused forever."""
    aria2 = _Aria2(gids=("a", "b", "c"), pause_fails={"b"})
    _reconnect(aria2)
    assert aria2.paused == ["a", "c"], "a failing pause must not abort the rest"
    assert sorted(aria2.resumed) == ["a", "c"], "everything paused must be resumed"


def test_a_failing_unpause_falls_back_to_unpause_all():
    aria2 = _Aria2(gids=("a", "b"), unpause_fails={"a"})
    msgs = _reconnect(aria2)
    assert aria2.unpause_all_called == 1
    assert sorted(aria2.resumed) == ["a", "b"]
    assert not any("left paused" in m for m in msgs)


def test_a_download_is_never_left_paused_without_saying_so():
    """If even unpause_all fails, the user must be told which key recovers."""
    aria2 = _Aria2(gids=("a",), unpause_fails={"a"})
    async def boom(): raise RuntimeError("rpc down")
    aria2.unpause_all = boom
    msgs = _reconnect(aria2)
    assert any("left paused" in m and "^p" in m for m in msgs), msgs


# ── pause is a toggle, so there is always a way back ──────────────────────────

def _pause_toggle(waiting):
    class A(_Aria2):
        async def get_waiting(self, *a, **k): return waiting
        async def pause_all(self): self.paused.append("ALL")
    aria2 = A()
    async def go():
        app = _app()
        app.aria2 = aria2
        async with app.run_test(size=(102, 30)) as pilot:
            await app.action_pause_all()
            for _ in range(3):
                await pilot.pause()
            return aria2, [str(n.message) for n in app._notifications]
    return asyncio.run(go())


def test_pause_pauses_when_nothing_is_paused():
    aria2, msgs = _pause_toggle([])
    assert aria2.paused == ["ALL"]
    assert any("paused" in m for m in msgs)


def test_pause_resumes_when_something_is_paused():
    """Without this there is no key anywhere that can resume a download."""
    from torrentcli.download import DownloadStatus
    aria2, msgs = _pause_toggle([DownloadStatus(gid="a", status="paused")])
    assert aria2.unpause_all_called == 1
    assert aria2.paused == []
    assert any("Resumed" in m for m in msgs)
