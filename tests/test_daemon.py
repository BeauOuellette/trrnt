"""Lifecycle tests for the aria2c daemon manager.

The bug these guard against: tget used to spawn `aria2c --daemon=true`, which
detaches and reparents to launchd, and then nothing ever stopped it — leaving a
runaway daemon holding the RPC port and burning a CPU core.

Two layers of testing:

* pure unit tests (argv construction, the shutdown ladder, the PID-reuse guard)
  that run everywhere;
* process-level tests driving a **real aria2c**, skipped when it is not
  installed. Faking aria2c turned out to be worse than useless — a copy of a
  SIP-signed system binary lands in an unkillable state on macOS, and only the
  real thing can honour `--stop-with-process`.

Every aria2c started here is confined to a scratch state dir, a throwaway
BitTorrent port range, and an ephemeral RPC port, so tests never touch the
user's real daemon on 6800.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import trrnt.daemon as daemon_mod
from trrnt.daemon import Aria2Daemon

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Never executed: capture_spawn() intercepts any argv mentioning aria2c.
FAKE_ARIA2C = "/nonexistent/bin/aria2c"

HAVE_ARIA2C = shutil.which("aria2c") is not None
needs_aria2c = pytest.mark.skipif(not HAVE_ARIA2C, reason="aria2c not installed")

# Inert replacement for the real download tuning: no DHT, no LPD, and a BT port
# range well away from the 6881-6999 the app really uses.
TEST_FLAGS = [
    "--enable-dht=false",
    "--enable-dht6=false",
    "--bt-enable-lpd=false",
    "--listen-port=7881-7899",
    "--dht-listen-port=7881-7899",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def free_port() -> int:
    """An unused localhost port, so tests never collide with a real aria2."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_rpc(d: Aria2Daemon, timeout: float = 15.0) -> bool:
    """Poll until the daemon's RPC endpoint answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if d.is_rpc_alive(timeout=1.0):
            return True
        time.sleep(0.2)
    return False


def wait_gone(pid: int, timeout: float = 30.0) -> bool:
    """For processes this test process did NOT spawn (so they get auto-reaped)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def wait_proc_gone(proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    """For processes pytest itself spawned.

    `os.kill(pid, 0)` keeps succeeding for a zombie, and anything pytest starts
    stays a zombie until pytest wait()s on it — so ownership-based polling is
    the only honest check here. Not a concern in production: aria2c is either
    our own child (reaped via Popen) or an orphan from a previous run that
    launchd reaps immediately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


class FakeProc:
    """Stand-in for a spawned aria2c when we only care about the argv."""
    pid = 424242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def capture_spawn(monkeypatch) -> dict:
    """Record the argv/kwargs of the next aria2c spawn instead of running it.

    Only intercepts aria2c: helper commands such as `ps` (used by the PID-reuse
    guard) must still really run.
    """
    real_popen = subprocess.Popen
    captured: dict = {}

    def fake_popen(args, **kwargs):
        if not any("aria2c" in str(a) for a in args):
            return real_popen(args, **kwargs)
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return captured


def forbid_spawn(monkeypatch) -> None:
    """Fail the test if aria2c is launched, while leaving `ps` etc. working."""
    real_popen = subprocess.Popen

    def guard(args, **kwargs):
        if any("aria2c" in str(a) for a in args):
            pytest.fail("must not spawn a second aria2c daemon")
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guard)


@pytest.fixture
def make_daemon(tmp_path, monkeypatch):
    """Factory for Aria2Daemon instances that clean themselves up."""
    monkeypatch.setattr(daemon_mod, "download_flags", lambda cfg=None: TEST_FLAGS)
    created: list[Aria2Daemon] = []

    def _make(port: int | None = None, **kwargs) -> Aria2Daemon:
        d = Aria2Daemon(
            {"rpc_url": f"http://localhost:{port or free_port()}/jsonrpc"},
            state_dir=tmp_path / "state",
            **kwargs,
        )
        created.append(d)
        return d

    yield _make

    for d in created:
        try:
            d.shutdown()
        except Exception:
            pass


@pytest.fixture
def daemon(make_daemon):
    return make_daemon()


@pytest.fixture
def loose_aria2c(tmp_path):
    """A real aria2c we start ourselves, outside the manager's knowledge."""
    started: list[subprocess.Popen] = []

    def _start(port: int) -> subprocess.Popen:
        proc = subprocess.Popen(
            [shutil.which("aria2c"), "--enable-rpc", "--rpc-listen-all=false",
             f"--rpc-listen-port={port}", f"--dir={tmp_path}", *TEST_FLAGS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        started.append(proc)
        return proc

    yield _start

    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


# ── spawn flags (no aria2c needed) ───────────────────────────────────────────

def test_spawn_does_not_detach_and_ties_aria2_to_our_pid(daemon, monkeypatch):
    """The core fix: no --daemon=true, and --stop-with-process=<our pid>."""
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    monkeypatch.setattr(daemon, "is_rpc_alive", lambda timeout=2.0: False)
    daemon._spawn()

    args = captured["args"]
    assert not any(a.startswith("--daemon") for a in args), \
        "--daemon=true detaches aria2 and is what orphaned it"
    assert f"--stop-with-process={os.getpid()}" in args
    # Must stay a direct child in our process group to be reapable.
    assert not captured["kwargs"].get("start_new_session")
    daemon._owned_pid = None  # don't let the fixture chase a fake pid


def test_rpc_port_default_is_unchanged():
    assert Aria2Daemon({}).rpc_port == 6800
    assert Aria2Daemon({"rpc_url": "http://localhost:6800/jsonrpc"}).rpc_port == 6800


def test_rpc_listen_port_follows_config(make_daemon, monkeypatch):
    """A non-default rpc_url must actually bind that port, not silently 6800."""
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    d = make_daemon(port=6999, startup_timeout=0)
    monkeypatch.setattr(d, "is_rpc_alive", lambda timeout=2.0: False)
    d._spawn()
    assert "--rpc-listen-port=6999" in captured["args"]
    d._owned_pid = None


def test_download_tuning_flags_are_preserved(tmp_path, monkeypatch):
    """A config with no tunables set still spawns aria2 the way trrnt intends.

    Deliberately uses the real download_flags, not the inert test set. The
    tunables now come from config.yaml, so this pins what an unconfigured
    daemon gets — which is what every existing install has.
    """
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    d = Aria2Daemon({"rpc_url": "http://localhost:6800/jsonrpc"},
                    state_dir=tmp_path, startup_timeout=0)
    monkeypatch.setattr(d, "is_rpc_alive", lambda timeout=2.0: False)
    d._spawn()

    for flag in ("--seed-ratio=2.0", "--split=16", "--bt-max-peers=100",
                 "--min-split-size=1M", "--listen-port=6881-6999",
                 "--dht-listen-port=6881-6999", "--enable-dht=true",
                 "--enable-dht6=true", "--max-connection-per-server=16",
                 "--bt-request-peer-speed-limit=5M", "--enable-peer-exchange=true",
                 # Was hardcoded to 5, which silently ignored the config key
                 # of the same name. It now honours it; 3 is the default.
                 "--max-concurrent-downloads=3",
                 # Was hardcoded on. LPD announces your torrents to the LAN,
                 # which is outside the tunnel — aria2's own default is off.
                 "--bt-enable-lpd=false"):
        assert flag in captured["args"], f"lost download flag {flag}"
    d._owned_pid = None


def test_auto_save_interval_is_not_zero(daemon, monkeypatch):
    """auto-save-interval=0 would drop resume data when we shut the daemon down."""
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    monkeypatch.setattr(daemon, "is_rpc_alive", lambda timeout=2.0: False)
    daemon._spawn()
    assert "--auto-save-interval=60" in captured["args"]
    daemon._owned_pid = None


def test_seeding_torrents_survive_a_restart(daemon, monkeypatch):
    """A finished-but-seeding torrent counts as 'completed' to aria2, so
    plain --save-session drops it — and with it, the chance to file the
    download by its contents on the next launch."""
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    monkeypatch.setattr(daemon, "is_rpc_alive", lambda timeout=2.0: False)
    daemon._spawn()
    assert "--force-save=true" in captured["args"]
    daemon._owned_pid = None


def test_event_poll_is_opt_in(tmp_path, monkeypatch):
    """Unset by default (aria2's own choice); passed through when configured."""
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)

    captured = capture_spawn(monkeypatch)
    d = Aria2Daemon({}, state_dir=tmp_path, startup_timeout=0)
    monkeypatch.setattr(d, "is_rpc_alive", lambda timeout=2.0: False)
    d._spawn()
    assert not any(a.startswith("--event-poll") for a in captured["args"])
    d._owned_pid = None

    captured2 = capture_spawn(monkeypatch)
    d2 = Aria2Daemon({"event_poll": "poll"}, state_dir=tmp_path, startup_timeout=0)
    monkeypatch.setattr(d2, "is_rpc_alive", lambda timeout=2.0: False)
    d2._spawn()
    assert "--event-poll=poll" in captured2["args"]
    d2._owned_pid = None


# ── PID-reuse guard ──────────────────────────────────────────────────────────

def test_recycled_pid_is_never_killed(daemon):
    """Our recorded PID may have been reused by an unrelated process."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        daemon._write_pidfile(victim.pid)
        # Neither comm nor argv[0] mentions aria2c, so the guard must reject it.
        assert daemon._read_pidfile() is None
        assert alive(victim.pid), "killed an unrelated process that reused the PID"
    finally:
        victim.kill()
        victim.wait()


def test_stale_pidfile_for_dead_process_is_cleaned_up(daemon):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    daemon._write_pidfile(dead.pid)
    assert daemon._read_pidfile() is None
    assert not daemon._pidfile.exists()


def test_missing_pidfile_is_not_an_error(daemon):
    assert daemon._read_pidfile() is None


# ── shutdown ladder (no aria2c needed) ───────────────────────────────────────

def test_shutdown_prefers_graceful_rpc(daemon, monkeypatch):
    """aria2.shutdown first so trackers are notified and state is flushed."""
    calls = []
    monkeypatch.setattr(daemon, "_rpc", lambda m, **kw: calls.append(m))
    monkeypatch.setattr(daemon, "_wait_gone", lambda pid, t: True)
    daemon._owned_pid = 12345
    daemon.shutdown()
    assert calls == ["shutdown"]


def test_shutdown_escalates_to_force_then_signals(daemon, monkeypatch):
    """Full ladder when a wedged daemon ignores everything."""
    calls, kills = [], []
    monkeypatch.setattr(daemon, "_rpc", lambda m, **kw: calls.append(m))
    monkeypatch.setattr(daemon, "_wait_gone", lambda pid, t: False)  # never dies
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon_mod, "_pid_is_aria2c", lambda pid: True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append(sig))

    daemon._owned_pid = 12345
    daemon._proc = None
    daemon.shutdown()

    assert calls == ["shutdown", "forceShutdown"]
    assert kills == [signal.SIGTERM, signal.SIGKILL]


def test_shutdown_is_idempotent(daemon, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_rpc", lambda m, **kw: calls.append(m))
    monkeypatch.setattr(daemon, "_wait_gone", lambda pid, t: True)
    daemon._owned_pid = 12345
    daemon.shutdown()
    daemon.shutdown()
    daemon.shutdown()
    assert calls == ["shutdown"]


def test_shutdown_of_unowned_daemon_is_a_noop(daemon, monkeypatch):
    """We must never stop an aria2c we merely adopted."""
    calls, kills = [], []
    monkeypatch.setattr(daemon, "_rpc", lambda m, **kw: calls.append(m))
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append(sig))
    daemon._owned_pid = None
    daemon.shutdown()
    assert calls == [] and kills == []


# ── stale / existing daemon detection (real aria2c) ──────────────────────────

@needs_aria2c
def test_adopts_foreign_daemon_and_never_kills_it(make_daemon, loose_aria2c, monkeypatch):
    """An aria2c the user started must be used, not duplicated and not killed."""
    port = free_port()
    foreign = loose_aria2c(port)
    d = make_daemon(port=port)
    assert wait_rpc(d), "helper aria2c never came up"

    forbid_spawn(monkeypatch)
    assert d.ensure_running() == "adopted"
    assert d._owned_pid is None

    d.shutdown()
    time.sleep(1.0)
    assert foreign.poll() is None, "adopted daemons belong to the user, not to us"


@needs_aria2c
def test_reclaims_our_own_healthy_daemon_from_a_previous_run(
    make_daemon, loose_aria2c, monkeypatch
):
    """A daemon we left behind is re-owned so this run finally shuts it down."""
    port = free_port()
    leftover = loose_aria2c(port)
    d = make_daemon(port=port)
    assert wait_rpc(d)

    # Simulate the pidfile a previous tget run would have left.
    d._write_pidfile(leftover.pid)

    forbid_spawn(monkeypatch)
    assert d.ensure_running() == "reclaimed"
    assert d._owned_pid == leftover.pid

    d.shutdown()
    assert wait_proc_gone(leftover), "reclaimed daemon must actually be stopped"


@needs_aria2c
def test_stale_wedged_daemon_is_reaped_and_replaced(make_daemon, loose_aria2c):
    """Running but not answering our RPC endpoint — the runaway case."""
    # A real aria2c on some *other* port: alive, but dead as far as we care.
    wedged = loose_aria2c(free_port())
    time.sleep(1.0)
    assert wedged.poll() is None

    d = make_daemon()  # different RPC port, so is_rpc_alive() is False
    d._write_pidfile(wedged.pid)

    assert d.ensure_running() == "replaced"
    assert wait_proc_gone(wedged), "the wedged daemon must be killed, not left spinning"
    assert d._owned_pid is not None and d._owned_pid != wedged.pid
    assert d.is_rpc_alive(), "replacement daemon should be serving"


# ── full lifecycle (real aria2c) ─────────────────────────────────────────────

@needs_aria2c
def test_start_serve_and_shutdown(make_daemon):
    """Starts, answers RPC, and is actually gone after shutdown()."""
    d = make_daemon()
    assert d.ensure_running() == "started"
    pid = d._owned_pid
    assert pid and alive(pid)
    assert d.is_rpc_alive(), "spawned daemon never answered RPC"
    assert d.queue_is_empty()

    d.shutdown()
    assert wait_gone(pid), "aria2c survived shutdown"
    assert not d.is_rpc_alive()
    assert not d._pidfile.exists()


@needs_aria2c
def test_startup_failure_is_reported_and_not_owned(make_daemon, loose_aria2c):
    """If aria2c dies on startup we must not claim to own a dead process."""
    port = free_port()
    blocker = loose_aria2c(port)  # already holding the RPC port
    d = make_daemon(port=port, startup_timeout=10)
    assert wait_rpc(d), "blocker aria2c never came up"

    # Spawn directly: the port is occupied, so aria2c must fail to bind.
    assert d._spawn() == "failed"
    assert d._owned_pid is None
    assert not d._pidfile.exists()
    assert blocker.poll() is None, "must not have taken down the blocking daemon"


# ── process-level: CLI exit and signals (real aria2c) ────────────────────────

def _runner_script(port: int, state_dir: Path, linger: bool) -> str:
    """A miniature tget: start the daemon, report its PID, then exit or wait."""
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {SRC!r})
        import trrnt.daemon as dmod
        dmod.download_flags = lambda cfg=None: {TEST_FLAGS!r}
        d = dmod.Aria2Daemon(
            {{"rpc_url": "http://localhost:{port}/jsonrpc"}},
            state_dir={str(state_dir)!r},
        )
        assert d.ensure_running() == "started"
        print(d._owned_pid, flush=True)
        {"time.sleep(300)" if linger else "pass"}
    """)


def _start_runner(port: int, state_dir: Path, linger: bool):
    proc = subprocess.Popen(
        [sys.executable, "-c", _runner_script(port, state_dir, linger)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    line = proc.stdout.readline().strip()
    if not line.isdigit():
        proc.kill()
        pytest.fail(f"runner did not report a daemon pid: {proc.stderr.read()}")
    return proc, int(line)


@needs_aria2c
def test_cli_exit_stops_the_daemon(tmp_path):
    """Normal CLI exit must not leave aria2c behind — this is the reported bug."""
    proc, aria_pid = _start_runner(free_port(), tmp_path / "state", linger=False)
    proc.wait(timeout=60)
    assert wait_gone(aria_pid), "aria2c outlived the CLI — it was orphaned again"


@needs_aria2c
def test_sigint_stops_the_daemon(tmp_path):
    """Ctrl-C must take the daemon with it."""
    proc, aria_pid = _start_runner(free_port(), tmp_path / "state", linger=True)
    assert alive(aria_pid)

    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=60)
    assert wait_gone(aria_pid), "aria2c survived SIGINT"


@needs_aria2c
def test_sigterm_stops_the_daemon(tmp_path):
    proc, aria_pid = _start_runner(free_port(), tmp_path / "state", linger=True)
    assert alive(aria_pid)

    proc.terminate()
    proc.wait(timeout=60)
    assert wait_gone(aria_pid), "aria2c survived SIGTERM"


@needs_aria2c
def test_sigkilled_cli_still_loses_its_daemon(tmp_path):
    """--stop-with-process is the backstop when none of our handlers can run."""
    proc, aria_pid = _start_runner(free_port(), tmp_path / "state", linger=True)
    assert alive(aria_pid)

    proc.kill()  # SIGKILL: no atexit, no signal handler, nothing of ours runs
    proc.wait(timeout=60)
    assert wait_gone(aria_pid, timeout=60), \
        "aria2c did not honour --stop-with-process after the CLI was SIGKILLed"


def test_bind_interface_is_passed_to_aria2(tmp_path, monkeypatch):
    """Binding every socket to the tunnel is the only thing that actually keeps
    BitTorrent inside the VPN — a per-download option leaves DHT and tracker
    announces on the default route."""
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    d = Aria2Daemon({"rpc_url": "http://localhost:6800/jsonrpc"},
                    state_dir=tmp_path, startup_timeout=0,
                    bind_interface="utun4")
    monkeypatch.setattr(d, "is_rpc_alive", lambda timeout=2.0: False)
    d._spawn()
    assert "--interface=utun4" in captured["args"]
    d._owned_pid = None


def test_no_interface_flag_when_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("TRRNT_ARIA2C_BIN", FAKE_ARIA2C)
    captured = capture_spawn(monkeypatch)
    d = Aria2Daemon({"rpc_url": "http://localhost:6800/jsonrpc"},
                    state_dir=tmp_path, startup_timeout=0)
    monkeypatch.setattr(d, "is_rpc_alive", lambda timeout=2.0: False)
    d._spawn()
    assert not any(a.startswith("--interface") for a in captured["args"])
    d._owned_pid = None
