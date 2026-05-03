"""Plex Media Server integration."""

import httpx
from rich.console import Console

console = Console()


class PlexClient:
    """Interact with Plex Media Server API."""

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", False)
        self.url = config.get("url", "http://localhost:32400").rstrip("/")
        self.token = config.get("token", "")
        self.library_sections = config.get("library_sections", {})

    @property
    def headers(self) -> dict:
        return {
            "X-Plex-Token": self.token,
            "Accept": "application/json",
        }

    async def scan_library(self, section_id: int | str) -> bool:
        """Trigger a library scan for a specific section."""
        if not self.enabled or not self.token:
            return False

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.url}/library/sections/{section_id}/refresh",
                    headers=self.headers,
                )
                return resp.status_code == 200
        except Exception as e:
            console.print(f"[yellow]Plex scan failed: {e}[/]")
            return False

    async def scan_for_category(self, category: str) -> bool:
        """Scan the Plex library section matching a content category."""
        section_id = self.library_sections.get(category)
        if section_id is None:
            return False
        return await self.scan_library(section_id)

    async def scan_all(self) -> dict[str, bool]:
        """Scan all configured library sections."""
        results = {}
        for name, section_id in self.library_sections.items():
            results[name] = await self.scan_library(section_id)
        return results

    async def get_libraries(self) -> list[dict]:
        """List all Plex library sections."""
        if not self.enabled or not self.token:
            return []

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.url}/library/sections",
                    headers=self.headers,
                )
                resp.raise_for_status()
                data = resp.json()
                directories = (
                    data.get("MediaContainer", {}).get("Directory", [])
                )
                return [
                    {"id": d["key"], "title": d["title"], "type": d["type"]}
                    for d in directories
                ]
        except Exception as e:
            console.print(f"[yellow]Failed to get Plex libraries: {e}[/]")
            return []

    async def check_connection(self) -> bool:
        """Verify Plex server is reachable."""
        if not self.enabled or not self.token:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.url}/identity",
                    headers=self.headers,
                )
                return resp.status_code == 200
        except Exception:
            return False
