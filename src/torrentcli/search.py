"""Jackett/Torznab search aggregation."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import xmltodict


@dataclass
class TorrentResult:
    """Single search result."""
    title: str = ""
    magnet: str = ""
    torrent_url: str = ""
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0
    indexer: str = ""
    category: str = ""
    pub_date: str = ""
    info_url: str = ""

    @property
    def size_human(self) -> str:
        """Human-readable file size."""
        size = self.size_bytes
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    @property
    def download_url(self) -> str:
        """Best available download URL (prefer magnet)."""
        return self.magnet or self.torrent_url


def _extract_torznab_attrs(item: dict) -> dict[str, str]:
    """Extract torznab:attr values from an item."""
    attrs = {}
    raw = item.get("torznab:attr", [])
    if isinstance(raw, dict):
        raw = [raw]
    for attr in raw:
        if isinstance(attr, dict) and "@name" in attr:
            attrs[attr["@name"]] = attr.get("@value", "")
    return attrs


def _detect_category(title: str, jackett_cats: list[str] | None = None) -> str:
    """Guess content category from title and Jackett category IDs."""
    title_lower = title.lower()

    # Audiobook signals take precedence — they overlap with the music range
    # (Jackett 3030 sits inside 3000s) and audiobook releases can ship as MP3.
    if jackett_cats:
        for cat in jackett_cats:
            try:
                if int(cat) == 3030:
                    return "audiobooks"
            except ValueError:
                pass
    if re.search(r"\b(audiobook|m4b|abook|audible)\b", title_lower):
        return "audiobooks"

    # Jackett category ranges: 2000s = Movies, 5000s = TV, 3000s = Audio
    if jackett_cats:
        for cat in jackett_cats:
            try:
                num = int(cat)
                if 2000 <= num < 3000:
                    return "movies"
                elif 5000 <= num < 6000:
                    return "tv"
                elif 3000 <= num < 4000:
                    return "music"
            except ValueError:
                pass

    # Fallback: title pattern matching
    tv_patterns = [
        r"s\d{2}e\d{2}", r"season.\d+", r"complete.series",
        r"\d{1,2}x\d{2}", r"episode.\d+",
    ]
    for pat in tv_patterns:
        if re.search(pat, title_lower):
            return "tv"

    if any(kw in title_lower for kw in ["flac", "mp3", "album", "discography"]):
        return "music"

    return "movies"  # default assumption for video content


class JackettSearch:
    """Search torrents via Jackett's Torznab API."""

    def __init__(self, config: dict):
        self.base_url = config.get("url", "http://localhost:9117").rstrip("/")
        self.api_key = config.get("api_key", "")
        self.indexers = config.get("indexers", [])
        self.timeout = config.get("timeout", 30)
        # Per-indexer ceiling. A single slow/broken indexer (e.g. one hitting a
        # Cloudflare challenge) must not be able to stall the whole search past
        # this, regardless of the overall `timeout`. Defaults to `timeout`.
        self.indexer_timeout = config.get("indexer_timeout", self.timeout)
        # Populated after each search() with (indexer, reason) for any indexer
        # that failed or timed out, so callers can distinguish "no results"
        # from "the search itself broke". Reset at the start of every search.
        self.last_errors: list[tuple[str, str]] = []

    def _build_url(self, query: str, indexer: str = "all") -> str:
        """Build Jackett Torznab search URL."""
        params = {
            "apikey": self.api_key,
            "q": query,
        }
        return f"{self.base_url}/api/v2.0/indexers/{indexer}/results/torznab/api?{urlencode(params)}"

    async def _list_indexers(self, client: httpx.AsyncClient) -> list[str]:
        """Fetch the IDs of indexers configured in Jackett.

        Querying each indexer by ID lets one slow indexer fail in isolation,
        unlike the aggregate `all` endpoint where Jackett blocks on the slowest
        one and a single hang times out the entire request.
        """
        try:
            resp = await client.get(
                f"{self.base_url}/api/v2.0/indexers/all/results/torznab/api",
                params={"apikey": self.api_key, "t": "indexers", "configured": "true"},
            )
            resp.raise_for_status()
            parsed = xmltodict.parse(resp.text)
            idx = parsed.get("indexers", {}).get("indexer", [])
            if isinstance(idx, dict):
                idx = [idx]
            ids = [i.get("@id") for i in idx if i.get("@id")]
            return ids or ["all"]
        except Exception:
            # Couldn't enumerate — fall back to the aggregate endpoint.
            return ["all"]

    async def _search_indexer(
        self,
        client: httpx.AsyncClient,
        query: str,
        indexer: str,
        quality_exclude: list[str] | None,
    ) -> list[TorrentResult]:
        """Search a single indexer. Records failures in self.last_errors."""
        try:
            resp = await asyncio.wait_for(
                client.get(self._build_url(query, indexer)),
                timeout=self.indexer_timeout,
            )
            resp.raise_for_status()

            parsed = xmltodict.parse(resp.text)

            # Jackett reports indexer-level failures (Cloudflare challenges,
            # auth errors) as a torznab <error> element inside a 200 response.
            err = parsed.get("error")
            if isinstance(err, dict):
                desc = err.get("@description", "indexer error")
                self.last_errors.append((indexer, desc.splitlines()[0][:200]))
                return []

            channel = parsed.get("rss", {}).get("channel", {})
            items = channel.get("item", [])
            if isinstance(items, dict):
                items = [items]

            found: list[TorrentResult] = []
            for item in items:
                result = self._parse_item(item, indexer)
                if result and self._passes_filter(result, quality_exclude):
                    found.append(result)
            return found

        except asyncio.TimeoutError:
            self.last_errors.append((indexer, f"timed out after {self.indexer_timeout}s"))
            return []
        except httpx.HTTPStatusError as e:
            self.last_errors.append((indexer, f"HTTP {e.response.status_code}"))
            return []
        except Exception as e:
            self.last_errors.append((indexer, type(e).__name__))
            return []

    async def search(
        self,
        query: str,
        quality_exclude: list[str] | None = None,
        max_results: int = 50,
    ) -> list[TorrentResult]:
        """Search across all configured indexers concurrently."""
        self.last_errors = []

        async with httpx.AsyncClient(timeout=self.indexer_timeout) as client:
            indexers = self.indexers if self.indexers else await self._list_indexers(client)

            # Query every indexer in parallel so a slow one only delays itself,
            # capped per-indexer. Healthy indexers still return their results.
            tasks = [
                self._search_indexer(client, query, indexer, quality_exclude)
                for indexer in indexers
            ]
            per_indexer = await asyncio.gather(*tasks)

        results = [r for sublist in per_indexer for r in sublist]

        # Sort by seeders descending
        results.sort(key=lambda r: r.seeders, reverse=True)
        return results[:max_results]

    def _parse_item(self, item: dict, indexer: str) -> TorrentResult | None:
        """Parse a Torznab XML item into a TorrentResult."""
        try:
            attrs = _extract_torznab_attrs(item)

            # Extract magnet from enclosure or link
            magnet = ""
            link = item.get("link", "")
            enclosure = item.get("enclosure", {})
            torrent_url = ""

            if isinstance(link, str) and link.startswith("magnet:"):
                magnet = link
            elif isinstance(enclosure, dict):
                enc_url = enclosure.get("@url", "")
                if enc_url.startswith("magnet:"):
                    magnet = enc_url
                else:
                    torrent_url = enc_url

            if not magnet and not torrent_url:
                torrent_url = link if isinstance(link, str) else ""

            # Get size from attrs or enclosure
            size = 0
            if "size" in attrs:
                try:
                    size = int(attrs["size"])
                except (ValueError, TypeError):
                    pass
            elif isinstance(enclosure, dict) and "@length" in enclosure:
                try:
                    size = int(enclosure["@length"])
                except (ValueError, TypeError):
                    pass

            # Get category list
            categories = item.get("category", [])
            if isinstance(categories, str):
                categories = [categories]

            return TorrentResult(
                title=item.get("title", "Unknown"),
                magnet=magnet,
                torrent_url=torrent_url,
                size_bytes=size,
                seeders=int(attrs.get("seeders", 0)),
                leechers=int(attrs.get("peers", 0)),
                indexer=attrs.get("tracker")
                    or (item["jackettindexer"]["#text"] if isinstance(item.get("jackettindexer"), dict) else str(item.get("jackettindexer", indexer))),
                category=_detect_category(
                    item.get("title", ""),
                    [attrs.get("category", "")]
                ),
                pub_date=item.get("pubDate", ""),
                info_url=item.get("comments", "")
                    or (item["guid"]["#text"] if isinstance(item.get("guid"), dict) else str(item.get("guid", ""))),
            )
        except Exception:
            return None

    def _passes_filter(
        self, result: TorrentResult, quality_exclude: list[str] | None
    ) -> bool:
        """Check if result passes quality filters."""
        if not quality_exclude:
            return True
        # Match quality tags as whole tokens only — release titles separate
        # tags with ., -, _, or space (e.g. "Movie.2025.TS.x264"). Bare
        # substring matching incorrectly flags titles like "Thunderbolts".
        title_upper = result.title.upper()
        for ex in quality_exclude:
            pattern = rf"(?:^|[\s._\-]){re.escape(ex.upper())}(?:[\s._\-]|$)"
            if re.search(pattern, title_upper):
                return False
        return True

    async def check_connection(self) -> bool:
        """Verify Jackett is reachable."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v2.0/indexers/all/results/torznab/api",
                    params={"apikey": self.api_key, "t": "caps"},
                )
                return resp.status_code == 200
        except Exception:
            return False
