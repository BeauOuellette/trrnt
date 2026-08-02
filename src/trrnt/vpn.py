"""VPN enforcement: interface check, IP verification, and kill switch daemon."""

import asyncio
import platform
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

import httpx
from rich.console import Console

console = Console()


@dataclass
class VPNStatus:
    """Current VPN state."""
    connected: bool = False
    interface: str = ""
    vpn_ip: str = ""
    error: str = ""


class VPNGuard:
    """VPN enforcement and monitoring."""

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", True)
        self.interface_prefix = config.get("interface_prefix", "utun")
        self.real_ip = config.get("real_ip", "")
        self.ip_check_url = config.get("ip_check_url", "https://ipinfo.io/ip")
        self.poll_interval = config.get("kill_switch_interval", 5)
        self._kill_switch_task: asyncio.Task | None = None
        self._on_vpn_drop: list[Callable] = []

    def _run(self, *argv: str) -> str:
        """Run a command, returning stdout or "" on any failure."""
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            return result.stdout
        except Exception:
            return ""

    def default_route_interface(self) -> str | None:
        """The interface carrying default traffic right now."""
        if platform.system() == "Darwin":
            out = self._run("/sbin/route", "-n", "get", "default")
            match = re.search(r"^\s*interface:\s*(\S+)", out, re.MULTILINE)
        else:
            out = self._run("ip", "route", "show", "default")
            match = re.search(r"\bdev\s+(\S+)", out)
        return match.group(1) if match else None

    def interface_ipv4(self, interface: str) -> str | None:
        """The IPv4 address bound to an interface, if it has one."""
        if not interface:
            return None
        if platform.system() == "Darwin":
            out = self._run("/sbin/ifconfig", interface)
            match = re.search(r"^\s*inet\s+(\d+\.\d+\.\d+\.\d+)", out, re.MULTILINE)
        else:
            out = self._run("ip", "-4", "addr", "show", interface)
            match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", out)
        return match.group(1) if match else None

    def _is_tunnel(self, interface: str) -> bool:
        prefixes = (self.interface_prefix,)
        if platform.system() != "Darwin":
            prefixes += ("tun", "wg", "proton")
        return interface.startswith(prefixes)

    def find_vpn_interface(self) -> str | None:
        """The tunnel actually carrying traffic, or None.

        "Some utun is UP" proves nothing on macOS: it keeps several utun
        devices up permanently for iCloud Private Relay and Handoff, none of
        which carry ordinary traffic and none of which have an IPv4 address.
        Matching those made the VPN check pass with the VPN switched off.

        The tunnel that matters is the one the default route points at, and
        it must have an address we can bind a socket to.
        """
        interface = self.default_route_interface()
        if not interface or not self._is_tunnel(interface):
            return None  # traffic is going out the physical interface
        if not self.interface_ipv4(interface):
            return None  # nothing to bind to
        return interface

    async def get_external_ip(self) -> str:
        """Get current external IP."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self.ip_check_url)
            return resp.text.strip()

    async def check(self) -> VPNStatus:
        """Full VPN status check: interface + IP verification."""
        if not self.enabled:
            return VPNStatus(connected=True, interface="disabled", vpn_ip="unchecked")

        status = VPNStatus()

        # Step 1: Check for tunnel interface
        iface = self.find_vpn_interface()
        if not iface:
            status.error = f"No {self.interface_prefix}* tunnel interface found"
            return status

        status.interface = iface

        # Step 2: Verify external IP differs from real IP
        try:
            current_ip = await self.get_external_ip()
            status.vpn_ip = current_ip

            if self.real_ip and current_ip == self.real_ip:
                status.error = "External IP matches real IP — VPN may not be routing"
                return status

            status.connected = True
        except Exception as e:
            # Can't reach IP check service — might be fine if VPN is up
            # Trust the interface check alone
            status.connected = True
            status.vpn_ip = "unknown"

        return status

    def on_vpn_drop(self, callback: Callable):
        """Register callback for VPN disconnection events."""
        self._on_vpn_drop.append(callback)

    async def kill_switch_loop(self):
        """Continuously monitor VPN; fire callbacks if it drops."""
        console.print(
            f"[bold green]Kill switch active[/] — polling every {self.poll_interval}s"
        )
        while True:
            await asyncio.sleep(self.poll_interval)
            status = await self.check()
            if not status.connected:
                console.print(
                    f"[bold red]⚠ VPN DROPPED[/]: {status.error}"
                )
                for cb in self._on_vpn_drop:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb()
                        else:
                            cb()
                    except Exception as e:
                        console.print(f"[red]Kill switch callback error: {e}[/]")

    def start_kill_switch(self) -> asyncio.Task:
        """Start the kill switch as a background task."""
        self._kill_switch_task = asyncio.create_task(self.kill_switch_loop())
        return self._kill_switch_task

    def stop_kill_switch(self):
        """Stop the kill switch monitor."""
        if self._kill_switch_task:
            self._kill_switch_task.cancel()
            self._kill_switch_task = None
