"""Read-only Prowlarr client used by Foxarr dry-run jobs."""

from __future__ import annotations

from typing import Any

import httpx


class ProwlarrError(RuntimeError):
    """Raised when a Prowlarr dry-run request cannot be completed."""


class ProwlarrClient:
    """Small client for Prowlarr's read-only search API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def search(
        self,
        query: str,
        indexer_ids: list[int] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")

        params: dict[str, Any] = {"query": query.strip(), "limit": limit}
        if indexer_ids:
            params["indexerIds"] = indexer_ids
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/search",
                params=params,
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProwlarrError(f"Prowlarr search failed: {error}") from error

        if not isinstance(payload, list):
            raise ProwlarrError("Prowlarr search returned a non-array response")
        results = [self._safe_release(item) for item in payload if isinstance(item, dict)]
        return results[:limit]

    def resolve_download_url(
        self,
        query: str,
        guid: str,
        indexer_ids: list[int] | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], str]:
        """Re-search and resolve one guid; the executable URL stays in memory only."""
        if not guid.strip():
            raise ValueError("guid must not be empty")
        params: dict[str, Any] = {"query": query.strip(), "limit": limit}
        if indexer_ids:
            params["indexerIds"] = indexer_ids
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/search",
                params=params,
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProwlarrError(f"Prowlarr resolve failed: {error}") from error

        if not isinstance(payload, list):
            raise ProwlarrError("Prowlarr resolve returned a non-array response")
        for item in payload:
            if not isinstance(item, dict) or item.get("guid") != guid:
                continue
            download_url = item.get("downloadUrl") or item.get("magnetUrl")
            if isinstance(download_url, str) and download_url.startswith(
                ("magnet:", "http://", "https://")
            ):
                return self._safe_release(item), download_url
            raise ProwlarrError("matching release has no supported download URL")
        raise ProwlarrError("matching release guid was not found")

    @staticmethod
    def _safe_release(item: dict[str, Any]) -> dict[str, Any]:
        """Keep descriptive release fields, never persist executable URLs."""
        allowed = (
            "title",
            "indexer",
            "indexerId",
            "protocol",
            "size",
            "seeders",
            "leechers",
            "publishDate",
            "guid",
            "language",
            "languages",
            "quality",
            "resolution",
            "resolutions",
            "videoCodec",
            "videoCodecs",
            "codec",
            "codecs",
            "source",
            "sources",
            "releaseType",
            "hdr",
            "dynamicRange",
            "dynamicRanges",
            "audioCodec",
            "audioCodecs",
            "audio",
        )
        return {key: item[key] for key in allowed if key in item}
