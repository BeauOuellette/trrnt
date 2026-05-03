"""tget — Terminal torrent aggregator & downloader.

Usage:
    tget                        Launch interactive TUI
    tget search "query"         Quick search from CLI
    tget add <magnet/url>       Add a download directly
    tget status                 Show active downloads
    tget remove <gid>           Stop and remove a download
    tget pause / resume         Pause/resume all
    tget vpn                    Check VPN status
    tget plex scan              Trigger Plex library scan
    tget scan <path>            Scan file/dir for threats
    tget quarantine             List quarantined items
    tget quarantine release     Release false positive
    tget config                 Show/initialize config
"""

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import Config

console = Console()


def _ensure_services():
    """Start aria2 and clamd if not already running."""
    import shutil
    import subprocess

    # Start aria2 if not running
    try:
        subprocess.run(
            ["pgrep", "-x", "aria2c"],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError:
        if shutil.which("aria2c"):
            console.print("[dim]Starting aria2...[/]", end=" ")
            subprocess.Popen(
                ["aria2c", "--enable-rpc", "--rpc-listen-all=false",
                 "--enable-dht=true", "--enable-dht6=true",
                 "--seed-ratio=2.0",
                 "--max-concurrent-downloads=5",
                 "--max-connection-per-server=16",
                 "--split=16",
                 "--min-split-size=1M",
                 "--bt-max-peers=100",
                 "--bt-request-peer-speed-limit=5M",
                 "--enable-peer-exchange=true",
                 "--bt-enable-lpd=true",
                 "--listen-port=6881-6999",
                 "--dht-listen-port=6881-6999",
                 "--daemon=true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            console.print("[green]✓[/]")
        else:
            console.print("[yellow]aria2 not installed (brew install aria2)[/]")

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


def get_config(config_path: str | None = None) -> Config:
    return Config(config_path)


def run_async(coro):
    """Run an async function in the event loop."""
    return asyncio.run(coro)


@click.group(invoke_without_command=True)
@click.option("--config", "-c", "config_path", default=None, help="Path to config file")
@click.pass_context
def cli(ctx, config_path):
    """tget — search, select, and download torrents from your terminal."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = get_config(config_path)

    if ctx.invoked_subcommand is None:
        # Auto-start services before launching TUI
        _ensure_services()
        from .tui import run_tui
        run_tui(ctx.obj["config"])


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
    categories = config.get("categories", default={})
    plex = PlexClient(config.get("plex"))
    scanned_cats = set()

    for i, result in selected:
        url = result.download_url
        if not url:
            console.print(f"  [red]✗[/] {result.title[:50]} — no download URL")
            continue

        cat_config = categories.get(result.category, categories.get("other", {}))
        download_dir = cat_config.get("path", aria2.download_dir)

        try:
            if url.startswith("magnet:"):
                gid = await aria2.add_magnet(url, download_dir=download_dir)
            else:
                gid = await aria2.add_torrent_url(url, download_dir=download_dir)
            console.print(f"  [green]✓[/] {result.title[:50]} → {result.category} (gid: {gid})")

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
        categories = config.get("categories", default={})
        cat_config = categories.get(category, categories.get("other", {}))
        download_dir = cat_config.get("path", aria2.download_dir)

        try:
            if url.startswith("magnet:"):
                gid = await aria2.add_magnet(url, download_dir=download_dir)
            else:
                gid = await aria2.add_torrent_url(url, download_dir=download_dir)
            console.print(f"[green]✓ Added[/] (gid: {gid}) → {download_dir}")
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
        from .download import Aria2Client
        from .security import SecurityScanner

        aria2 = Aria2Client(config.get("aria2"))
        scanner = SecurityScanner(config.get("security"))
        scanned_gids: set[str] = set()

        while True:
            try:
                active = await aria2.get_active()
                waiting = await aria2.get_waiting(count=10)
                stopped = await aria2.get_stopped(count=5)
                stats = await aria2.get_global_stat()
            except Exception as e:
                console.print(f"[red]Cannot reach aria2:[/] {e}")
                console.print("Make sure aria2 is running: [bold]aria2c --enable-rpc[/]")
                return

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

            # Scan-on-complete for --watch mode
            if watch and config.get("security", "scan_on_complete"):
                for dl in stopped:
                    if dl.status == "complete" and dl.gid not in scanned_gids:
                        scanned_gids.add(dl.gid)
                        if not dl.dir:
                            continue
                        dl_dir = Path(dl.dir)
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
                            scanner.quarantine(download_path, scan_result)
                        else:
                            console.print(f"[yellow]⚠ Scan warning:[/] {scan_result.error}")

                        # Re-route audiobooks based on file content. A clean
                        # download containing .m4b files belongs in the
                        # audiobooks folder regardless of how its title was
                        # tagged at search time.
                        if scan_result.clean:
                            import shutil
                            from .security import is_audiobook_dir
                            if is_audiobook_dir(download_path):
                                ab_root = Path(
                                    config.get("categories", "audiobooks", "path")
                                ).expanduser()
                                if ab_root not in download_path.parents:
                                    ab_root.mkdir(parents=True, exist_ok=True)
                                    new_path = ab_root / download_path.name
                                    if new_path.exists():
                                        console.print(
                                            f"[yellow]⚠ Audiobook target already exists, "
                                            f"leaving in place:[/] {new_path}"
                                        )
                                    else:
                                        shutil.move(str(download_path), str(new_path))
                                        console.print(
                                            f"[green]🎧 Routed audiobook → {new_path}[/]"
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
