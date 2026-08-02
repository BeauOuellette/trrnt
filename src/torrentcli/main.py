"""trrnt — Terminal torrent aggregator & downloader.

Usage:
    trrnt                        Launch interactive TUI (setup wizard on first run)
    trrnt setup                  Re-run the setup wizard
    trrnt search "query"         Quick search from CLI
    trrnt add <magnet/url>       Add a download directly
    trrnt status                 Show active downloads
    trrnt remove <gid>           Stop and remove a download
    trrnt pause / resume         Pause/resume all
    trrnt vpn                    Check VPN status
    trrnt plex scan              Trigger Plex library scan
    trrnt scan <path>            Scan file/dir for threats
    trrnt quarantine             List quarantined items
    trrnt quarantine release     Release false positive
    trrnt config                 Show/initialize config

`tget` remains as an alias for the same CLI. Config still lives at
~/.config/tget/config.yaml — the path is deliberately unchanged so existing
configs keep working.
"""

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .branding import SPLASH, SPLASH_COLORS, TAGLINE
from .config import Config
from .storage import DestinationUnavailable, resolve_destination, shorten

console = Console()


def _print_splash():
    """Print the logo while services start.

    Shown only while aria2/clamd are coming up — the TUI's home screen takes
    over the moment the app launches. Skipped when stdout is not a terminal so
    piping or redirecting output never picks up banner bytes.
    """
    if not console.is_terminal:
        return
    console.print()
    for line, color in zip(SPLASH, SPLASH_COLORS):
        console.print(f"  [bold {color}]{line}[/]")
    console.print(f"  [dim]{TAGLINE}[/]\n")


# Sentinel: VPN enforcement is on but no tunnel is carrying traffic.
_NO_TUNNEL = object()


def _resolve_bt_interface(config: Config):
    """Which interface aria2 should bind every socket to.

    Returns the interface name, "" for deliberately unbound, or _NO_TUNNEL
    when enforcement is on and there is nothing safe to bind to.

    Binding is what actually keeps BitTorrent traffic inside the tunnel. The
    VPN check alone only says a tunnel exists at that instant — it cannot stop
    aria2 from using the physical interface, and peers see the address the
    socket is bound to.
    """
    from .vpn import VPNGuard

    configured = (config.get("aria2", "bt_interface") or "").strip()
    if configured.lower() == "none":
        return ""  # explicit opt-out
    if configured:
        return configured  # pinned by hand

    if not config.get("vpn", "enabled", default=True):
        return ""

    interface = VPNGuard(config.get("vpn")).find_vpn_interface()
    if interface:
        return interface

    console.print(
        "[bold red]✗ No VPN tunnel is carrying traffic[/] — aria2 not started.\n"
        "  Binding it now would send BitTorrent over your ISP connection.\n"
        "  Connect the VPN and relaunch, or set [bold]aria2.bt_interface: "
        '"none"[/] to override.'
    )
    return _NO_TUNNEL


def _ensure_services(config: Config):
    """Start aria2 and clamd if not already running.

    Returns the Aria2Daemon so the caller can shut it down on exit. We no longer
    detect aria2 with `pgrep -x aria2c`: that finds *any* aria2c, including one
    wedged on a different port or spinning without serving RPC, and it told us
    nothing about which process we would later be responsible for stopping.
    Aria2Daemon probes the actual RPC endpoint instead and tracks ownership.
    """
    import shutil
    import subprocess

    from .daemon import Aria2Daemon

    bind = _resolve_bt_interface(config)
    if bind is _NO_TUNNEL:
        return None  # nothing started; downloads are blocked anyway

    daemon = Aria2Daemon(config.get("aria2"), bind_interface=bind)
    result = daemon.ensure_running()

    if result == "adopted":
        console.print("[dim]Using already-running aria2 (not started by tget)[/]")
    elif result == "reclaimed":
        console.print("[dim]Reattached to aria2 left by a previous run[/]")
    elif result == "replaced":
        console.print("[yellow]Replaced a stale aria2 daemon[/] [green]✓[/]")
    elif result == "started":
        console.print("[dim]Starting aria2...[/] [green]✓[/]")
    elif result == "failed":
        console.print("[red]aria2 exited immediately[/] — is port "
                      f"{daemon.rpc_port} already in use?")
    elif result == "unavailable":
        console.print("[yellow]aria2 not installed (brew install aria2)[/]")

    # Confirm the binding against the daemon that is actually running, rather
    # than assuming the flag we passed took effect. An adopted or reclaimed
    # daemon predates this launch and may be bound to nothing, or to a tunnel
    # that has since gone away.
    if bind and result != "unavailable":
        actual = daemon.bound_interface()
        if actual == bind:
            console.print(f"[dim]aria2 bound to[/] [green]{bind}[/]")
        else:
            console.print(
                f"[bold red]⚠ aria2 is NOT bound to {bind}[/]"
                f"{f' (it reports {actual!r})' if actual else ' — it is unbound'}.\n"
                "  BitTorrent traffic can leave over your ISP connection.\n"
                "  Quit any aria2 you started yourself, then relaunch trrnt."
            )

    # Start clamd if not running
    try:
        subprocess.run(
            ["pgrep", "-x", "clamd"],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError:
        if shutil.which("clamd"):
            console.print("[dim]Starting clamd...[/]", end=" ")
            subprocess.Popen(
                ["clamd"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            console.print("[green]✓[/]")

    return daemon


def get_config(config_path: str | None = None) -> Config:
    return Config(config_path)


def run_async(coro):
    """Run an async function in the event loop."""
    return asyncio.run(coro)


def _shutdown_daemon(daemon):
    """Stop an aria2 daemon we own. Aria2Daemon.shutdown() is idempotent
    and a no-op for a daemon we merely adopted, so this is safe on every
    exit path — and it is what stops us orphaning aria2c.
    is_rpc_alive() first: if aria2 already died we can't know what was
    queued, and shouldn't claim anything was saved."""
    if daemon is None:
        return
    if (daemon.owns_daemon and daemon.is_rpc_alive()
            and not daemon.queue_is_empty()):
        console.print(
            "[dim]Stopping aria2 — unfinished downloads are saved "
            "and resume next launch.[/]"
        )
    daemon.shutdown()


@click.group(invoke_without_command=True)
@click.option("--config", "-c", "config_path", default=None, help="Path to config file")
@click.pass_context
def cli(ctx, config_path):
    """trrnt — search, select, and download torrents from your terminal."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = get_config(config_path)

    if ctx.invoked_subcommand is None:
        config = ctx.obj["config"]
        # No config file means a first run: hand the whole job — installs,
        # services, config — to the in-TUI wizard. _ensure_services would
        # only print noise about things that aren't installed yet.
        first_run = not config.path.exists()
        daemon = None
        if first_run:
            console.print("[dim]First run — opening setup…[/]")
        else:
            # Auto-start services before launching TUI
            _print_splash()
            daemon = _ensure_services(config)
        from .tui import run_tui
        wizard_daemon = None
        try:
            wizard_daemon = run_tui(config, setup=first_run, daemon=daemon)
        finally:
            # daemon is None when we refused to start it unbound, or on a
            # first run; the wizard's daemon comes back from run_tui.
            _shutdown_daemon(daemon)
            _shutdown_daemon(wizard_daemon)


@cli.command("setup")
@click.pass_context
def setup_cmd(ctx):
    """Re-run the first-run setup wizard."""
    from .tui import run_tui
    daemon = None
    try:
        daemon = run_tui(ctx.obj["config"], setup=True)
    finally:
        _shutdown_daemon(daemon)


# ─── Search ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("query")
@click.option("--limit", "-n", default=20, help="Max results")
@click.option("--sort", "-s", type=click.Choice(["seeders", "size", "name"]), default="seeders")
@click.option("--download", "-d", is_flag=True, help="Interactive download selection")
@click.pass_context
def search(ctx, query, limit, sort, download):
    """Search for torrents across all indexers."""
    config = ctx.obj["config"]

    async def _search():
        from .vpn import VPNGuard
        from .search import JackettSearch

        # VPN check
        vpn = VPNGuard(config.get("vpn"))
        if config.get("vpn", "enabled"):
            status = await vpn.check()
            if not status.connected:
                console.print(f"[bold red]✗ VPN not connected:[/] {status.error}")
                console.print("Connect your VPN and try again.")
                sys.exit(1)
            console.print(f"[green]✓ VPN active[/] — {status.interface} ({status.vpn_ip})")

        # Search
        jackett = JackettSearch(config.get("jackett"))
        quality_exclude = config.get("quality_exclude", default=[])

        with console.status(f"Searching for '{query}'..."):
            results = await jackett.search(query, quality_exclude=quality_exclude, max_results=limit)

        if jackett.last_errors:
            console.print(
                f"[dim]{len(jackett.last_errors)} indexer(s) skipped:[/] "
                + ", ".join(f"{name} ({reason})" for name, reason in jackett.last_errors)
            )

        if not results:
            console.print("[yellow]No results found.[/]")
            return

        # Sort
        if sort == "size":
            results.sort(key=lambda r: r.size_bytes, reverse=True)
        elif sort == "name":
            results.sort(key=lambda r: r.title)
        # Default: already sorted by seeders

        # Display
        table = Table(title=f"Results for '{query}'", show_lines=False, expand=True)
        table.add_column("#", style="dim", width=3)
        table.add_column("Name", ratio=1, no_wrap=True)
        table.add_column("Size", justify="right", width=8)
        table.add_column("S", justify="right", width=4, style="green")
        table.add_column("L", justify="right", width=4, style="red")
        table.add_column("Source", width=12, style="dim", no_wrap=True)

        for i, r in enumerate(results, 1):
            table.add_row(
                str(i), r.title[:65], r.size_human,
                str(r.seeders), str(r.leechers), r.indexer[:15],
            )

        console.print(table)

        # Interactive download selection
        if download:
            await _interactive_download(config, results)

    run_async(_search())


async def _interactive_download(config: Config, results):
    """Prompt user to select results for download."""
    from .download import Aria2Client
    from .plex import PlexClient

    console.print(
        "\n[bold]Enter numbers to download[/] (comma-separated, e.g. 1,3,5) or 'q' to cancel:"
    )
    selection = input("> ").strip()

    if selection.lower() in ("q", "quit", ""):
        return

    # Parse selection
    indices = set()
    for part in selection.split(","):
        part = part.strip()
        if "-" in part:
            # Range: 1-5
            try:
                start, end = part.split("-")
                indices.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                indices.add(int(part))
            except ValueError:
                continue

    selected = [(i, results[i - 1]) for i in sorted(indices) if 1 <= i <= len(results)]
    if not selected:
        console.print("[yellow]No valid selections.[/]")
        return

    # Confirm
    console.print(f"\n[bold]Adding {len(selected)} torrent(s):[/]")
    for i, result in selected:
        console.print(f"  {i}. {result.title[:70]}")

    # Add to aria2
    aria2 = Aria2Client(config.get("aria2"))
    plex = PlexClient(config.get("plex"))
    scanned_cats = set()

    for i, result in selected:
        url = result.download_url
        if not url:
            console.print(f"  [red]✗[/] {result.title[:50]} — no download URL")
            continue

        try:
            dest = resolve_destination(config, result.category, aria2.download_dir)
        except DestinationUnavailable as e:
            console.print(f"  [red]✗[/] {result.title[:50]} — {e}")
            continue

        try:
            if url.startswith("magnet:"):
                gid = await aria2.add_magnet(url, download_dir=dest.path)
            else:
                gid = await aria2.add_torrent_url(url, download_dir=dest.path)
            console.print(f"  [green]✓[/] {result.title[:50]} → {result.category} (gid: {gid})")
            if dest.redirected:
                console.print(f"     [yellow]⚠ {dest.notice}[/]")
                continue  # not in the library, so nothing for Plex to find

            # Plex scan
            if config.get("plex", "enabled") and result.category not in scanned_cats:
                await plex.scan_for_category(result.category)
                scanned_cats.add(result.category)

        except Exception as e:
            console.print(f"  [red]✗[/] {result.title[:50]} — {e}")


# ─── Add ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("url")
@click.option("--category", "-cat", default="other", help="Content category")
@click.pass_context
def add(ctx, url, category):
    """Add a magnet link or torrent URL directly."""
    config = ctx.obj["config"]

    async def _add():
        from .vpn import VPNGuard
        from .download import Aria2Client

        # VPN check
        vpn = VPNGuard(config.get("vpn"))
        if config.get("vpn", "enabled"):
            status = await vpn.check()
            if not status.connected:
                console.print(f"[bold red]✗ VPN not connected[/]")
                sys.exit(1)

        aria2 = Aria2Client(config.get("aria2"))

        try:
            dest = resolve_destination(config, category, aria2.download_dir)
        except DestinationUnavailable as e:
            console.print(f"[red]✗ Failed:[/] {e}")
            sys.exit(1)

        try:
            if url.startswith("magnet:"):
                gid = await aria2.add_magnet(url, download_dir=dest.path)
            else:
                gid = await aria2.add_torrent_url(url, download_dir=dest.path)
            console.print(f"[green]✓ Added[/] (gid: {gid}) → {dest.path}")
            if dest.redirected:
                console.print(f"[yellow]⚠ {dest.notice}[/]")
        except Exception as e:
            console.print(f"[red]✗ Failed:[/] {e}")

    run_async(_add())


# ─── Status ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--watch", "-w", is_flag=True, help="Continuously refresh")
@click.pass_context
def status(ctx, watch):
    """Show current download status."""
    config = ctx.obj["config"]

    async def _status():
        from .download import Aria2Client, DownloadStatus, predict_category
        from .organize import (
            OrganizeStore,
            apply_pending_selection,
            cleanup_wrapper,
            configured_junk,
            effective_junk,
            find_orphan_records,
            organize_download,
        )
        from .security import SecurityScanner

        aria2 = Aria2Client(config.get("aria2"))
        scanner = SecurityScanner(config.get("security"))
        store = OrganizeStore()
        scanned_gids: set[str] = set()
        routed_gids: set[str] = set()
        refresh_tick = 0

        while True:
            try:
                active = await aria2.get_active()
                waiting = await aria2.get_waiting(count=10)
                # 25, not 5: metadata parents also land in the stopped list
                # and can push a real completion out of the window unscanned.
                stopped = await aria2.get_stopped(count=25)
                stats = await aria2.get_global_stat()
            except Exception as e:
                console.print(f"[red]Cannot reach aria2:[/] {e}")
                console.print("Make sure aria2 is running: [bold]aria2c --enable-rpc[/]")
                return

            # Deselect junk on follow-up downloads whose metadata resolved
            # (plans made in the TUI are serviced here too). No VPN gate:
            # aria2's interface binding is what encloses torrent traffic,
            # and it applies regardless of what this loop does.
            if watch:
                try:
                    store.reload()
                    if store.pending():
                        await apply_pending_selection(aria2, store, config)
                except Exception:
                    pass

            if watch:
                console.clear()

            table = Table(title="Downloads", show_lines=False)
            table.add_column("Name", max_width=40, no_wrap=True)
            table.add_column("Size", justify="right", width=10)
            table.add_column("Progress", width=25)
            table.add_column("Speed", justify="right", width=12)
            table.add_column("ETA", width=8)
            table.add_column("Status", width=10)

            for dl in active + waiting:
                pct = dl.progress
                bar_w = 15
                filled = int(bar_w * pct / 100)
                bar = "█" * filled + "░" * (bar_w - filled)
                progress = f"{bar} {pct:.1f}%"

                color = {"active": "green", "waiting": "yellow", "paused": "dim"}.get(
                    dl.status, "white"
                )

                table.add_row(
                    dl.name[:40] or dl.gid[:12],
                    dl.size_human,
                    progress,
                    dl.speed_human if dl.status == "active" else "—",
                    dl.eta if dl.status == "active" else "—",
                    f"[{color}]{dl.status}[/{color}]",
                )

            if stopped:
                for dl in stopped:
                    color = "green" if dl.status == "complete" else "red"
                    table.add_row(
                        dl.name[:40] or dl.gid[:12],
                        dl.size_human, "done" if dl.status == "complete" else dl.error_message[:20],
                        "—", "—", f"[{color}]{dl.status}[/{color}]",
                    )

            console.print(table)

            dl_speed = int(stats.get("downloadSpeed", 0))
            ul_speed = int(stats.get("uploadSpeed", 0))
            console.print(
                f"  ↓ {_human_speed(dl_speed)}  ↑ {_human_speed(ul_speed)}"
                f"  │  Active: {stats.get('numActive', 0)}"
                f"  Waiting: {stats.get('numWaiting', 0)}"
            )

            # Flag where each download will be filed once it finishes. A
            # torrent's folder is fixed by aria2 when it is created, so this
            # reports the destination rather than changing it.
            if watch:
                for dl in active:
                    if dl.gid in routed_gids or not dl.total_bytes:
                        continue
                    routed_gids.add(dl.gid)
                    try:
                        predicted = await predict_category(aria2, config, dl)
                    except Exception:
                        continue
                    if predicted:
                        console.print(
                            f"[green]→ {dl.name[:40]} is {predicted} — "
                            f"filing there when done[/]"
                        )

            # Scan-on-complete for --watch mode. Seeding torrents have finished
            # downloading but stay in the active list, so they must be picked
            # up here too — waiting for seed-ratio can mean waiting forever.
            if watch and config.get("security", "scan_on_complete"):
                finished = [d for d in stopped if d.status == "complete"]

                # Fold in downloads aria2 has already forgotten (result
                # cleared before the scan saw it) — the plan record is the
                # surviving evidence. First pass and every ~2 minutes.
                refresh_tick += 1
                if refresh_tick % 60 == 1:
                    try:
                        store.reload()
                        finished += [
                            DownloadStatus(
                                gid=r.active_gid or r.gid, status="complete",
                                name=r.wrapper, dir=r.dir,
                            )
                            for r in await find_orphan_records(aria2, store)
                        ]
                    except Exception:
                        pass
                finished += [
                    d for d in active
                    if d.total_bytes > 0 and d.completed_bytes >= d.total_bytes
                ]
                for dl in finished:
                    if dl.gid not in scanned_gids:
                        scanned_gids.add(dl.gid)
                        if not dl.dir:
                            continue
                        dl_dir = Path(dl.dir)
                        async def _stop_seeding(gid=dl.gid):
                            """Release the files before moving them — aria2
                            loses track of a torrent the moment it is moved."""
                            for call in (aria2.force_remove, aria2.remove_result):
                                try:
                                    await call(gid)
                                except Exception:
                                    pass  # already stopped

                        record = None
                        try:
                            store.reload()
                            record = store.match(dl.gid, dl.dir)
                        except Exception:
                            record = None

                        # Remapped downloads were written straight to their
                        # final names: scan the files where they lie, drop
                        # the junk-placeholder wrapper, keep seeding. Mirrors
                        # the TUI's _finish_remapped.
                        if record is not None and record.remapped and record.name:
                            try:
                                rfiles = await aria2.get_files_detailed(dl.gid)
                            except Exception:
                                rfiles = []
                            rpaths = [
                                Path(f["path"]) for f in rfiles
                                if f["selected"] and f["path"]
                            ]
                            rpaths = [p for p in rpaths if p.exists()]
                            if rpaths:
                                flagged = False
                                for rpath in rpaths:
                                    scan_result = await scanner.full_scan(rpath)
                                    if scan_result.threats or scan_result.blocked_files:
                                        console.print(
                                            f"[red]✗ FLAGGED:[/] {rpath.name} — "
                                            f"{scan_result.summary}"
                                        )
                                        await _stop_seeding()
                                        scanner.quarantine(rpath, scan_result)
                                        flagged = True
                                        break
                                store.remove(record.gid)
                                if not flagged:
                                    junk = effective_junk(
                                        record.category, configured_junk(config)
                                    )
                                    note = " — still seeding"
                                    if dl.name and dl.name != dl.gid:
                                        wrapper = dl_dir / dl.name
                                        if not cleanup_wrapper(wrapper, junk):
                                            await _stop_seeding()
                                            try:
                                                for message in organize_download(
                                                    wrapper, Path(record.dir),
                                                    record.name, junk,
                                                ):
                                                    console.print(f"[yellow]⚠ {message}[/]")
                                            except OSError as e:
                                                console.print(
                                                    f"[red]Filing stragglers failed:[/] {e}"
                                                )
                                            note = ""
                                    console.print(
                                        f"[green]📁 Filed: {record.name} → "
                                        f"{shorten(record.dir)}{note}[/]"
                                    )
                                continue
                            # aria2 lost the file list — fall through to the
                            # locate-and-move path below.

                        download_path = None
                        if dl.name and dl.name != dl.gid:
                            candidate = dl_dir / dl.name
                            if candidate.exists():
                                download_path = candidate
                        if download_path is None and dl_dir.exists():
                            children = [c for c in dl_dir.iterdir() if c.name != ".DS_Store"]
                            if children:
                                download_path = max(children, key=lambda p: p.stat().st_mtime)
                        if download_path is None or not download_path.exists():
                            continue
                        console.print(f"\n[bold]Scanning completed download:[/] {download_path.name}")
                        scan_result = await scanner.full_scan(download_path, expected_bytes=dl.total_bytes)
                        if scan_result.clean:
                            console.print(f"[green]✓ Clean:[/] {scan_result.summary}")
                        elif scan_result.threats or scan_result.blocked_files:
                            console.print(f"[red]✗ FLAGGED:[/] {scan_result.summary}")
                            await _stop_seeding()  # quarantine moves the files
                            scanner.quarantine(download_path, scan_result)
                        else:
                            console.print(f"[yellow]⚠ Scan warning:[/] {scan_result.error}")

                        # File the download under the name chosen at add time
                        # (in the TUI). The plan wins over content detection —
                        # the user picked this destination by hand.
                        if not scan_result.clean:
                            record = None
                        if record is not None:
                            store.remove(record.gid)
                            if record.name:
                                await _stop_seeding()
                                junk = effective_junk(
                                    record.category, configured_junk(config)
                                )
                                try:
                                    messages = organize_download(
                                        download_path, Path(record.dir),
                                        record.name, junk,
                                    )
                                except OSError as e:
                                    console.print(f"[red]Filing failed:[/] {e}")
                                    continue
                                console.print(
                                    f"[green]📁 Filed: {record.name} → "
                                    f"{shorten(record.dir)}[/]"
                                )
                                for message in messages:
                                    console.print(f"[yellow]⚠ {message}[/]")
                                continue

                        # Safety net for whatever the in-flight re-route could
                        # not settle. Usually finds the download already in the
                        # right folder and does nothing — which is what leaves
                        # it seeding, since only the move below must stop that.
                        if scan_result.clean:
                            import shutil
                            from .security import detect_content_category
                            content_cat = detect_content_category(download_path)
                            if content_cat:
                                try:
                                    dest = resolve_destination(config, content_cat)
                                except DestinationUnavailable as e:
                                    console.print(
                                        f"[yellow]⚠ {content_cat} not routed:[/] {e}"
                                    )
                                    continue
                                root = Path(dest.path)
                                if root not in download_path.parents:
                                    root.mkdir(parents=True, exist_ok=True)
                                    new_path = root / download_path.name
                                    if new_path.exists():
                                        console.print(
                                            f"[yellow]⚠ {content_cat} target already "
                                            f"exists, leaving in place:[/] {new_path}"
                                        )
                                    else:
                                        await _stop_seeding()
                                        shutil.move(str(download_path), str(new_path))
                                        Path(f"{download_path}.aria2").unlink(
                                            missing_ok=True
                                        )
                                        console.print(
                                            f"[green]Routed {content_cat} → {new_path}[/]"
                                        )

            if not watch:
                break
            await asyncio.sleep(2)

    run_async(_status())


def _human_speed(bps: int) -> str:
    speed = float(bps)
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if speed < 1024:
            return f"{speed:.1f} {unit}"
        speed /= 1024
    return f"{speed:.1f} TB/s"


# ─── Clear ───────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def clear(ctx):
    """Clear completed/errored downloads from the queue."""
    config = ctx.obj["config"]

    async def _clear():
        from .download import Aria2Client
        aria2 = Aria2Client(config.get("aria2"))
        await aria2.purge_completed()
        console.print("[green]Cleared finished downloads[/]")

    run_async(_clear())


# ─── Remove ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("gid")
@click.option("--force", "-f", is_flag=True, help="Force remove (skip confirmation)")
@click.pass_context
def remove(ctx, gid, force):
    """Stop and remove a download by GID."""
    config = ctx.obj["config"]

    async def _remove():
        from .download import Aria2Client
        aria2 = Aria2Client(config.get("aria2"))

        # Show what we're about to remove
        try:
            dl = await aria2.get_status(gid)
        except Exception:
            # GID might be in stopped list — try removing the result
            try:
                await aria2.remove_result(gid)
                console.print(f"[green]✓ Removed result[/] {gid}")
                return
            except Exception as e:
                console.print(f"[red]✗ Download not found:[/] {gid}")
                return

        name = dl.name or gid
        if not force:
            console.print(f"Remove [bold]{name}[/] ({dl.status}, {dl.progress:.1f}%)? [y/N]: ", end="")
            if input().strip().lower() != "y":
                console.print("Cancelled.")
                return

        try:
            await aria2.force_remove(gid)
            console.print(f"[green]✓ Removed[/] {name}")
        except Exception as e:
            console.print(f"[red]✗ Failed:[/] {e}")

    run_async(_remove())


# ─── Pause / Resume ──────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def pause(ctx):
    """Pause all downloads."""
    config = ctx.obj["config"]

    async def _pause():
        from .download import Aria2Client
        aria2 = Aria2Client(config.get("aria2"))
        await aria2.pause_all()
        console.print("[yellow]All downloads paused[/]")

    run_async(_pause())


@cli.command()
@click.pass_context
def resume(ctx):
    """Resume all downloads."""
    config = ctx.obj["config"]

    async def _resume():
        from .download import Aria2Client
        aria2 = Aria2Client(config.get("aria2"))
        await aria2.unpause_all()
        console.print("[green]All downloads resumed[/]")

    run_async(_resume())


# ─── VPN ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def vpn(ctx):
    """Check VPN connection status."""
    config = ctx.obj["config"]

    async def _vpn():
        from .vpn import VPNGuard
        guard = VPNGuard(config.get("vpn"))
        status = await guard.check()

        if status.connected:
            console.print(f"[bold green]✓ VPN connected[/]")
            console.print(f"  Interface: {status.interface}")
            console.print(f"  External IP: {status.vpn_ip}")
        else:
            console.print(f"[bold red]✗ VPN not connected[/]")
            console.print(f"  Error: {status.error}")

    run_async(_vpn())


@cli.command()
@click.pass_context
def jackett(ctx):
    """Open Jackett's UI in your browser (start the service if needed)."""
    import shutil as _shutil
    import subprocess
    import time
    import webbrowser

    import httpx

    config = ctx.obj["config"]
    url = config.get("jackett", "url") or "http://localhost:9117"

    def is_up() -> bool:
        try:
            r = httpx.get(url, timeout=2)
            return r.status_code < 500
        except Exception:
            return False

    if is_up():
        console.print(f"[green]✓ Jackett is up at {url}[/]")
    else:
        console.print(f"[yellow]Jackett not reachable at {url}[/]")
        if _shutil.which("brew"):
            console.print("[dim]Starting via brew services...[/]")
            try:
                subprocess.run(
                    ["brew", "services", "start", "jackett"],
                    capture_output=True, timeout=10,
                )
            except Exception as e:
                console.print(f"[yellow]brew services start failed: {e}[/]")
            for _ in range(10):
                time.sleep(0.5)
                if is_up():
                    console.print(f"[green]✓ Jackett is up at {url}[/]")
                    break
            else:
                console.print(
                    "[yellow]Jackett didn't come up in time — opening the URL anyway "
                    "(if you run it via Docker or another method, that's fine).[/]"
                )
        else:
            console.print(
                "[dim]brew not found — start Jackett yourself, then re-run.[/]"
            )

    webbrowser.open(url)
    console.print(f"[blue]Opened {url}[/]")


# ─── Plex ─────────────────────────────────────────────────────────────────────

@cli.group()
def plex():
    """Plex Media Server commands."""
    pass


@plex.command("scan")
@click.option("--section", "-s", default=None, help="Library section ID")
@click.pass_context
def plex_scan(ctx, section):
    """Trigger Plex library scan."""
    config = ctx.obj["config"]

    async def _scan():
        from .plex import PlexClient
        client = PlexClient(config.get("plex"))

        if not client.enabled:
            console.print("[yellow]Plex integration not configured[/]")
            return

        if section:
            ok = await client.scan_library(section)
            status = "[green]✓[/]" if ok else "[red]✗[/]"
            console.print(f"{status} Scanned section {section}")
        else:
            results = await client.scan_all()
            for name, ok in results.items():
                status = "[green]✓[/]" if ok else "[red]✗[/]"
                console.print(f"{status} {name}")

    run_async(_scan())


@plex.command("libraries")
@click.pass_context
def plex_libraries(ctx):
    """List Plex library sections."""
    config = ctx.obj["config"]

    async def _libs():
        from .plex import PlexClient
        client = PlexClient(config.get("plex"))
        libs = await client.get_libraries()
        if not libs:
            console.print("[yellow]No libraries found (check Plex config)[/]")
            return
        for lib in libs:
            console.print(f"  [{lib['id']}] {lib['title']} ({lib['type']})")

    run_async(_libs())


# ─── Config ───────────────────────────────────────────────────────────────────

@cli.command("config")
@click.option("--init", "init_config", is_flag=True, help="Create default config file")
@click.pass_context
def config_cmd(ctx, init_config):
    """Show config info or initialize config file."""
    config = ctx.obj["config"]

    if init_config:
        created = config.ensure_config_exists()
        if created:
            console.print(f"[green]✓ Config created at:[/] {config.path}")
            console.print("Edit it with your Jackett API key, Plex token, etc.")
        else:
            console.print(f"[yellow]Config already exists:[/] {config.path}")
    else:
        console.print(f"Config path: {config.path}")
        exists = config.path.exists()
        console.print(f"Exists: {'[green]yes[/]' if exists else '[red]no[/] (run tget config --init)'}")

        if exists:
            console.print(f"\nJackett: {config.get('jackett', 'url')}")
            console.print(f"aria2: {config.get('aria2', 'rpc_url')}")
            console.print(f"VPN enforcement: {'enabled' if config.get('vpn', 'enabled') else 'disabled'}")
            console.print(f"Plex: {'enabled' if config.get('plex', 'enabled') else 'disabled'}")

            # Connection checks
            async def _check():
                from .search import JackettSearch
                from .download import Aria2Client
                from .plex import PlexClient
                from .security import SecurityScanner

                console.print("\n[bold]Connection checks:[/]")

                jackett = JackettSearch(config.get("jackett"))
                ok = await jackett.check_connection()
                console.print(f"  Jackett: {'[green]✓[/]' if ok else '[red]✗[/]'}")

                aria2 = Aria2Client(config.get("aria2"))
                ok = await aria2.check_connection()
                console.print(f"  aria2: {'[green]✓[/]' if ok else '[red]✗[/]'}")

                plex = PlexClient(config.get("plex"))
                ok = await plex.check_connection()
                console.print(f"  Plex: {'[green]✓[/]' if ok else '[red]✗[/]'}")

                scanner = SecurityScanner(config.get("security"))
                clam_status = await scanner.check_clamav_available()
                if clam_status["installed"]:
                    daemon = "[green]daemon running[/]" if clam_status["daemon_running"] else "[yellow]daemon stopped[/]"
                    console.print(f"  ClamAV: [green]✓[/] ({daemon})")
                    console.print(f"          {clam_status['version']}")
                else:
                    console.print(f"  ClamAV: [red]✗ not installed[/] (brew install clamav)")

            run_async(_check())


# ─── Security ─────────────────────────────────────────────────────────────────

@cli.command("scan")
@click.argument("path")
@click.option("--expected-size", "-s", default=0, help="Expected size in bytes for verification")
@click.pass_context
def scan_path(ctx, path, expected_size):
    """Scan a file or directory for threats."""
    config = ctx.obj["config"]

    async def _scan():
        from .security import SecurityScanner

        scanner = SecurityScanner(config.get("security"))
        target = Path(path).expanduser().resolve()

        if not target.exists():
            console.print(f"[red]Path not found: {target}[/]")
            sys.exit(1)

        console.print(f"[bold]Scanning:[/] {target}")
        result = await scanner.full_scan(target, expected_bytes=expected_size)

        if result.clean:
            console.print(f"[bold green]✓[/] {result.summary}")
        else:
            console.print(f"[bold red]✗[/] {result.summary}")
            if result.threats:
                console.print("[bold red]Threats:[/]")
                for t in result.threats:
                    console.print(f"  [red]•[/] {t}")
            if result.blocked_files:
                console.print("[bold yellow]Blocked files:[/]")
                for b in result.blocked_files:
                    console.print(f"  [yellow]•[/] {b}")
            if result.error:
                console.print(f"[yellow]Note:[/] {result.error}")

            # Offer to quarantine
            console.print("\nQuarantine this download? [y/N]: ", end="")
            if input().strip().lower() == "y":
                scanner.quarantine(target, result)

    run_async(_scan())


@cli.group("quarantine")
def quarantine_cmd():
    """Manage quarantined downloads."""
    pass


@quarantine_cmd.command("list")
@click.pass_context
def quarantine_list(ctx):
    """List all quarantined items."""
    config = ctx.obj["config"]

    from .security import SecurityScanner
    scanner = SecurityScanner(config.get("security"))
    items = scanner.list_quarantine()

    if not items:
        console.print("[green]Quarantine is empty[/]")
        return

    table = Table(title="Quarantine", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", max_width=50)
    table.add_column("Size", justify="right", width=10)
    table.add_column("Type", width=6)
    table.add_column("Date", width=20)

    for i, item in enumerate(items, 1):
        size = item["size"]
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                size_str = f"{size:.1f} {unit}"
                break
            size /= 1024
        else:
            size_str = f"{size:.1f} TB"

        table.add_row(
            str(i),
            item["name"],
            size_str,
            "dir" if item["is_dir"] else "file",
            item["modified"][:19],
        )

    console.print(table)
    console.print(f"\nQuarantine dir: {scanner.quarantine_dir}")
    console.print(f"Log: {scanner._log_path}")


@quarantine_cmd.command("release")
@click.argument("name")
@click.argument("dest_dir")
@click.pass_context
def quarantine_release(ctx, name, dest_dir):
    """Release a quarantined item (false positive)."""
    config = ctx.obj["config"]

    from .security import SecurityScanner
    scanner = SecurityScanner(config.get("security"))

    console.print(f"[yellow]⚠ Releasing from quarantine:[/] {name}")
    console.print("Are you sure this is a false positive? [y/N]: ", end="")
    if input().strip().lower() != "y":
        console.print("Cancelled.")
        return

    ok = scanner.release_from_quarantine(name, dest_dir)
    if not ok:
        sys.exit(1)


@quarantine_cmd.command("log")
@click.pass_context
def quarantine_log(ctx):
    """View the quarantine log."""
    config = ctx.obj["config"]

    from .security import SecurityScanner
    scanner = SecurityScanner(config.get("security"))

    if scanner._log_path.exists():
        console.print(scanner._log_path.read_text())
    else:
        console.print("[dim]No quarantine events logged yet.[/]")


if __name__ == "__main__":
    cli()
