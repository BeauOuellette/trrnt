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

    def find_vpn_interface(self) -> str | None:
        """Find active VPN tunnel interface."""
        try:
            if platform.system() == "Darwin":
                result = subprocess.run(
                    ["/sbin/ifconfig"], capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                # Find utun interfaces that are UP
                interfaces = re.findall(
                    rf"({self.interface_prefix}\d+):.*?flags=.*?<UP",
                    result.stdout,
                    re.DOTALL,
                )
                return interfaces[-1] if interfaces else None
            else:
                # Linux: check for tun/wg interfaces
                result = subprocess.run(
                    ["ip", "link", "show"], capture_output=True, text=True, timeout=5
                )
                for prefix in [self.interface_prefix, "tun", "wg", "proton"]:
                    match = re.search(rf"\d+: ({prefix}\d+):.*?state UP", result.stdout)
                    if match:
                        return match.group(1)
                return None
        except Exception as e:
            return None

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
