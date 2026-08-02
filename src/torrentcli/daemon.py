"""aria2c daemon lifecycle: start, adopt, and guaranteed shutdown.

Background
----------
tget used to spawn ``aria2c --daemon=true`` and then forget about it.
``--daemon=true`` makes aria2 fork and detach, so the process we actually
launched exited immediately and the real daemon was reparented to launchd/init.
Nothing in the codebase ever stopped it again, so every ``tget`` run could leave
another aria2c behind holding the RPC port and the BitTorrent listen ports —
and aria2 is known to spin a core in a tight poll loop once it gets into a bad
idle state.

This module owns that lifecycle instead:

* we start a daemon **only** when the configured RPC endpoint is actually dead;
* we keep the ``Popen`` handle and record the PID in a state file, so a daemon
  left over from a previous run can be found and reaped;
* we guarantee a shutdown on every exit path — normal return, ``SIGINT``,
  ``SIGTERM``, ``SIGHUP``, and unhandled exceptions — via an escalating ladder
  (``aria2.shutdown`` → ``aria2.forceShutdown`` → ``SIGTERM`` → ``SIGKILL``);
* we pass ``--stop-with-process=<our pid>`` so aria2 reaps *itself* even if we
  die in a way that runs none of our handlers (``SIGKILL``, power loss, a hard
  crash in the TUI).

We never touch an aria2c the user started themselves. If something is already
answering on the configured RPC endpoint and we have no record of starting it,
we adopt it read-only and leave it running when we exit.
"""

import atexit
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import settings

# Where we remember the PID of the daemon we started, so a later run can find a
# leftover from a crashed session. Follows the XDG state convention.
STATE_DIR = Path.home() / ".local" / "state" / "tget"

# Escalation budget for the shutdown ladder. These are deliberately short: the
# whole point is that quitting tget must not hang, and --stop-with-process is
# the backstop if we run out of patience.
GRACEFUL_TIMEOUT = 5.0   # aria2.shutdown — unregisters from trackers, saves state
FORCE_TIMEOUT = 3.0      # aria2.forceShutdown — skips tracker contact
SIGTERM_TIMEOUT = 3.0    # SIGTERM before we resort to SIGKILL

# How long to wait for a freshly spawned daemon to start answering RPC.
STARTUP_TIMEOUT = 10.0

# Download tuning that nobody has asked to change. The rest moved into
# config.yaml and arrives via download_flags().
_STATIC_FLAGS = [
    "--enable-dht=true",
    "--enable-dht6=true",
    "--max-connection-per-server=16",
    "--split=16",
    "--min-split-size=1M",
    "--bt-max-peers=100",
    "--bt-request-peer-speed-limit=5M",
    "--enable-peer-exchange=true",
]


def download_flags(aria2_config: dict | None = None) -> list[str]:
    """Every tunable spawn flag, derived from the aria2 config section.

    Spawn is the one moment all three setting tiers land at once — it is the
    only place a restart-tier setting like the listen port can take hold at
    all — so this deliberately asks settings.aria2_options for the lot.

    Missing keys fall back to DEFAULTS rather than to aria2's own defaults:
    an older config.yaml written before these keys existed should still get
    trrnt's seeding and encryption behaviour, not aria2's.
    """
    from .config import DEFAULTS

    cfg = aria2_config or {}
    fallback = DEFAULTS["aria2"]
    values = {f.key: cfg.get(f.key, fallback.get(f.key)) for f in settings.FIELDS}
    options = settings.aria2_options(
        values,
        tiers=(settings.TIER_LIVE, settings.TIER_NEW, settings.TIER_RESTART),
    )
    return [*_STATIC_FLAGS, *(f"--{key}={value}" for key, value in sorted(options.items()))]


def _parse_etime(text: str) -> float | None:
    """Seconds from ps(1) elapsed time: ``[[DD-]HH:]MM:SS``."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    while len(numbers) < 3:
        numbers.insert(0, 0)  # MM:SS -> 0:MM:SS
    hours, minutes, seconds = numbers[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _aria2c_binary() -> str | None:
    """Locate aria2c. TGET_ARIA2C_BIN overrides PATH lookup (also used by tests)."""
    override = os.environ.get("TGET_ARIA2C_BIN")
    if override:
        return override
    return shutil.which("aria2c")


def _pid_alive(pid: int) -> bool:
    """True if `pid` exists and we are allowed to signal it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else — not ours to manage, but alive.
        return True
    return True


def _pid_is_aria2c(pid: int) -> bool:
    """Guard against PID reuse before we signal anything.

    A PID we recorded hours ago may since have been recycled by an unrelated
    process, and killing that would be a serious bug. Only act if the process
    still looks like an aria2c.

    We check both the executable name and argv[0], because `ps -o comm=`
    resolves symlinks: an aria2c reached through a symlink (or a wrapper on
    PATH) reports the real target there. Deliberately *not* the whole command
    line — that would match innocent processes like `grep aria2c`.
    """
    def field(fmt: str) -> str:
        # One `ps` call per field: comm and command can both contain spaces, so
        # asking for them together would be ambiguous to parse.
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", fmt],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip()

    if "aria2c" in field("comm="):
        return True
    cmdline = field("command=")
    argv0 = cmdline.split(" ", 1)[0] if cmdline else ""
    return "aria2c" in argv0


class Aria2Daemon:
    """Owns at most one aria2c process for the lifetime of this CLI run."""

    def __init__(
        self,
        aria2_config: dict | None = None,
        *,
        state_dir: Path | str = STATE_DIR,
        startup_timeout: float = STARTUP_TIMEOUT,
        bind_interface: str = "",
    ):
        cfg = aria2_config or {}
        # Kept whole so _spawn can rebuild its tunable flags from it; the
        # settings screen edits config.yaml and a restart re-reads them.
        self._cfg = cfg
        self.rpc_url = cfg.get("rpc_url") or "http://localhost:6800/jsonrpc"
        self.secret = cfg.get("rpc_secret", "") or ""
        # Optional escape hatch: aria2 has historically shipped idle busy-loop
        # bugs in some event backends. Leave unset to use aria2's own default
        # (kqueue on macOS); set aria2.event_poll to "poll" or "select" in
        # config.yaml if a spinning daemon ever comes back.
        self.event_poll = cfg.get("event_poll", "") or ""
        # Interface every aria2 socket is bound to. Set by the caller after it
        # has resolved which tunnel is actually carrying traffic; empty means
        # unbound, which the caller only allows with VPN enforcement off.
        self.bind_interface = bind_interface or ""

        self._state_dir = Path(state_dir)
        self._pidfile = self._state_dir / "aria2.pid"
        # Session persistence. The daemon now dies with the CLI, so without this
        # a queued-but-unfinished torrent would be forgotten between runs.
        # aria2 reloads it on the next start, preserving download continuity.
        self._session_file = self._state_dir / "aria2.session"
        self._startup_timeout = startup_timeout

        self._proc: subprocess.Popen | None = None
        # PID of a daemon *we* are responsible for stopping. Stays None when we
        # adopted a daemon the user started, which we must leave alone.
        self._owned_pid: int | None = None
        self._shutdown_done = False
        self._hooks_installed = False

    @property
    def owns_daemon(self) -> bool:
        """True if we are the ones responsible for stopping this daemon."""
        return self._owned_pid is not None

    # ── RPC ──────────────────────────────────────────────────────────────────

    @property
    def rpc_port(self) -> int:
        """RPC port taken from the configured URL (default 6800)."""
        return urlparse(self.rpc_url).port or 6800

    def _rpc(self, method: str, timeout: float = 2.0):
        """Synchronous JSON-RPC call.

        Deliberately sync rather than async: this runs from atexit and signal
        handlers, where spinning up an asyncio loop is unreliable (the loop may
        already be running, or already torn down).
        """
        params: list = []
        if self.secret:
            params.append(f"token:{self.secret}")
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": f"aria2.{method}",
            "params": params,
        }
        resp = httpx.post(self.rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", data["error"]))
        return data.get("result")

    def is_rpc_alive(self, timeout: float = 2.0) -> bool:
        """True if something is serving aria2 JSON-RPC at the configured URL."""
        try:
            return "version" in (self._rpc("getVersion", timeout=timeout) or {})
        except Exception:
            return False

    def bound_interface(self) -> str:
        """The interface the *running* daemon has its sockets bound to.

        Asked of aria2 rather than inferred from what we passed, because the
        daemon may have been started by someone else or left over from a run
        when a different tunnel was up. aria2 omits the key entirely when
        unbound, which reads as "" here.
        """
        try:
            return (self._rpc("getGlobalOption") or {}).get("interface", "") or ""
        except Exception:
            return ""

    def daemon_pid(self) -> int | None:
        """PID of the daemon we are talking to, when we can know it.

        A daemon we adopted from someone else was never recorded anywhere, so
        this is None for it — which is honest, and why the settings header
        simply omits uptime rather than guessing.
        """
        if self._owned_pid is not None:
            return self._owned_pid
        return self._read_pidfile()

    def uptime_seconds(self) -> float | None:
        """How long the daemon process has been up, or None if unknowable.

        Read from ps(1) rather than remembered from our own spawn, so that a
        reclaimed daemon reports its real age. Uptime is the only thing that
        distinguishes a healthy daemon from one that silently crashed and was
        respawned under us — a reset to near-zero means anything mid-flight
        was interrupted.
        """
        pid = self.daemon_pid()
        if pid is None:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "etime=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return _parse_etime(out.stdout)

    def queue_is_empty(self) -> bool:
        """True if the daemon has nothing active or waiting.

        Used to decide whether a shutdown is a no-op for the user or whether it
        will interrupt work in progress.
        """
        try:
            stat = self._rpc("getGlobalStat") or {}
            return int(stat.get("numActive", 0)) == 0 and int(stat.get("numWaiting", 0)) == 0
        except Exception:
            return False

    # ── PID file ─────────────────────────────────────────────────────────────

    def _read_pidfile(self) -> int | None:
        """Return the recorded PID if it is still a live aria2c, else None.

        Also cleans up the file when the recorded process is gone, so a stale
        file never accumulates.
        """
        try:
            pid = int(self._pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
        if not _pid_alive(pid) or not _pid_is_aria2c(pid):
            self._clear_pidfile()
            return None
        return pid

    def _write_pidfile(self, pid: int) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._pidfile.write_text(str(pid))
        except OSError:
            pass  # Losing the pidfile costs us cross-run cleanup, not correctness.

    def _clear_pidfile(self) -> None:
        try:
            self._pidfile.unlink(missing_ok=True)
        except OSError:
            pass

    # ── Startup ──────────────────────────────────────────────────────────────

    def ensure_running(self) -> str:
        """Make sure exactly one aria2c is serving our RPC endpoint.

        Returns one of:
          ``"adopted"``   — a daemon we did not start is already serving; left alone
          ``"reclaimed"`` — our own daemon from a previous run; we now own it again
          ``"started"``   — we spawned a fresh daemon
          ``"replaced"``  — a wedged daemon of ours was reaped and replaced
          ``"failed"``    — we tried to spawn one and it exited immediately
          ``"unavailable"`` — aria2c is not installed
        """
        recorded = self._read_pidfile()

        if self.is_rpc_alive():
            if recorded is not None:
                # Healthy leftover from a previous tget run. Take ownership so
                # that *this* run is the one that finally shuts it down, rather
                # than orphaning it a second time.
                self._owned_pid = recorded
                self._install_exit_hooks()
                return "reclaimed"
            # Someone else's aria2c (brew services, a manual aria2c, ...).
            # Use it, but never start a second one and never kill it.
            return "adopted"

        # Nothing is answering RPC. If we recorded a PID that is still alive,
        # it is wedged — holding the RPC and BitTorrent ports without serving
        # anything, which is exactly the runaway state we are fixing. Reap it
        # before starting a replacement, or the new daemon cannot bind.
        replaced = False
        if recorded is not None:
            self._reap(recorded)
            self._clear_pidfile()
            replaced = True

        result = self._spawn()
        if result == "started" and replaced:
            return "replaced"
        return result

    def _spawn(self) -> str:
        binary = _aria2c_binary()
        if not binary:
            return "unavailable"

        args = [
            binary,
            "--enable-rpc",
            "--rpc-listen-all=false",
            # Bind the port the client will actually talk to. Default stays 6800.
            f"--rpc-listen-port={self.rpc_port}",
            # Belt and suspenders: aria2 polls this PID and exits on its own if
            # we vanish without running any handler (SIGKILL, hard crash).
            f"--stop-with-process={os.getpid()}",
            # Keep the queue across the now CLI-scoped daemon lifetime.
            f"--save-session={self._session_file}",
            "--save-session-interval=60",
            # Without this, --save-session skips anything aria2 considers
            # completed — which includes a torrent that has finished
            # downloading and is only seeding. Those were dropped on every
            # restart, taking with them the chance to file the download by
            # its contents. aria2's own docs name this exact case.
            "--force-save=true",
            # aria2's default is already 60s; pin it so a stray value in the
            # user's ~/.aria2/aria2.conf cannot disable control-file saving and
            # cost them resume data when we shut the daemon down.
            "--auto-save-interval=60",
            *download_flags(self._cfg),
        ]

        if self.bind_interface:
            # Bind every socket — peers, trackers, DHT — to the tunnel. A
            # per-download `interface` option would leave DHT and tracker
            # announces on the default route, which is where the identifying
            # traffic is. aria2 refuses to start if the interface is gone,
            # which is the failure mode we want.
            args.append(f"--interface={self.bind_interface}")

        # Resume whatever the previous run left queued.
        if self._session_file.exists():
            args.append(f"--input-file={self._session_file}")

        if self.event_poll:
            args.append(f"--event-poll={self.event_poll}")

        if self.secret:
            # Note: this is visible in `ps`. aria2 offers no fd/stdin channel for
            # the secret, and the RPC listener is bound to localhost only, so we
            # accept that tradeoff rather than overriding the user's conf file.
            args.append(f"--rpc-secret={self.secret}")

        self._state_dir.mkdir(parents=True, exist_ok=True)

        # No --daemon=true, and no start_new_session: aria2 stays a direct child
        # in our process group. That is what makes it reapable at all, and it
        # means a terminal Ctrl-C reaches it too.
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._owned_pid = self._proc.pid
        self._write_pidfile(self._proc.pid)
        self._install_exit_hooks()

        # Wait for RPC to come up so callers can add downloads immediately.
        #
        # Liveness of *our* process is the authority here, not the RPC probe: if
        # some other daemon already holds the port, the probe would succeed
        # while our aria2c is busy exiting with a bind error, and we would claim
        # ownership of a process that is already gone.
        deadline = time.monotonic() + self._startup_timeout
        rpc_ready_at: float | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                return self._spawn_failed()
            if rpc_ready_at is None and self.is_rpc_alive(timeout=1.0):
                rpc_ready_at = time.monotonic()
            # Declare success only once RPC answers *and* our own process has
            # outlived that by a moment.
            if rpc_ready_at is not None and time.monotonic() - rpc_ready_at >= 0.5:
                return "started"
            time.sleep(0.1)

        # Timed out waiting for RPC. Still fine if the process is up — it may
        # just be slow — but not if it has already exited.
        if self._proc.poll() is not None:
            return self._spawn_failed()
        return "started"

    def _spawn_failed(self) -> str:
        """aria2c exited during startup — drop the ownership we provisionally took."""
        self._clear_pidfile()
        self._owned_pid = None
        return "failed"

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def _install_exit_hooks(self) -> None:
        """Register the handlers that guarantee we never orphan the daemon."""
        if self._hooks_installed:
            return
        self._hooks_installed = True

        # Covers normal exit, sys.exit(), and unhandled exceptions.
        atexit.register(self.shutdown)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                previous = signal.getsignal(sig)
            except (ValueError, OSError):
                continue

            def handler(signum, frame, _previous=previous, _sig=sig):
                self.shutdown()
                # Chain so we don't swallow the signal: preserve whatever the
                # app (or Python's default KeyboardInterrupt) would have done.
                if callable(_previous):
                    _previous(signum, frame)
                elif _previous == signal.SIG_IGN:
                    return  # the app deliberately ignores this signal
                else:
                    signal.signal(_sig, signal.SIG_DFL)
                    os.kill(os.getpid(), _sig)

            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # signal.signal() only works on the main thread; if we are not
                # there, atexit + --stop-with-process still cover us.
                continue

    def shutdown(self) -> None:
        """Stop the daemon we own, escalating until it is actually gone.

        Idempotent, and a no-op for an adopted daemon we did not start.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True

        pid = self._owned_pid
        if pid is None:
            return  # Adopted someone else's daemon — not ours to stop.

        # 1. Graceful: aria2 contacts trackers to unregister and flushes both the
        #    session file and the .aria2 control files, so downloads resume.
        try:
            self._rpc("shutdown")
            if self._wait_gone(pid, GRACEFUL_TIMEOUT):
                self._finish(pid)
                return
        except Exception:
            pass

        # 2. Forceful RPC: same shutdown minus the slow tracker round-trips.
        try:
            self._rpc("forceShutdown")
            if self._wait_gone(pid, FORCE_TIMEOUT):
                self._finish(pid)
                return
        except Exception:
            pass

        # 3. Signals, for a daemon whose RPC loop is wedged — which is precisely
        #    the busy-loop case that started all this.
        self._reap(pid)
        self._finish(pid)

    def _reap(self, pid: int) -> None:
        """SIGTERM then SIGKILL, with the PID-reuse guard applied first."""
        if not _pid_alive(pid) or not _pid_is_aria2c(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        if self._wait_gone(pid, SIGTERM_TIMEOUT):
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        self._wait_gone(pid, 1.0)

    def _finish(self, pid: int) -> None:
        """Clear our records and reap the zombie if it was a direct child."""
        self._clear_pidfile()
        self._owned_pid = None
        if self._proc is not None and self._proc.pid == pid:
            try:
                self._proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        """Poll until `pid` exits or `timeout` elapses. True if it is gone."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Reap our own child so it doesn't linger as a zombie and keep
            # answering os.kill(pid, 0).
            if self._proc is not None and self._proc.pid == pid:
                if self._proc.poll() is not None:
                    return True
            elif not _pid_alive(pid):
                return True
            time.sleep(0.1)

        if self._proc is not None and self._proc.pid == pid:
            return self._proc.poll() is not None
        return not _pid_alive(pid)
