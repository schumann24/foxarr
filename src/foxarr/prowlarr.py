"""Read-only Prowlarr client used by Foxarr dry-run jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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
        self.last_search_errors: list[str] = []

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

        # An empty list means "all enabled indexers". Querying them separately
        # prevents one slow Cloudflare/indexer path from hiding fast results.
        if not indexer_ids:
            return self.search_all(query, limit=limit)
        return self._search_once(query, indexer_ids, limit)

    def get_enabled_indexer_ids(self) -> list[int]:
        """Return enabled Prowlarr indexer ids, without exposing credentials."""
        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/indexer",
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProwlarrError(f"Prowlarr indexer list failed: {error}") from error
        if not isinstance(payload, list):
            raise ProwlarrError("Prowlarr indexer list returned a non-array response")

        ids: list[int] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("enable") is False:
                continue
            indexer_id = item.get("id")
            if isinstance(indexer_id, int) and not isinstance(indexer_id, bool):
                ids.append(indexer_id)
        return ids

    def search_all(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search enabled indexers independently and merge successful results.

        Each indexer gets its own request. A timeout/error from one indexer is
        recorded in ``last_search_errors`` but does not discard results from
        other indexers. If every indexer fails, a ProwlarrError is raised.
        """
        self.last_search_errors = []
        indexer_ids = self.get_enabled_indexer_ids()
        if not indexer_ids:
            # Keeps compatibility with Prowlarr-compatible test doubles and
            # installations where the indexer endpoint is not populated.
            return self._search_once(query, [], limit)

        executor = ThreadPoolExecutor(max_workers=min(8, len(indexer_ids)))
        futures = {
            executor.submit(self._search_once, query, [indexer_id], limit): indexer_id
            for indexer_id in indexer_ids
        }
        results: list[dict[str, Any]] = []
        completed = 0
        try:
            for future in as_completed(futures, timeout=self.timeout):
                completed += 1
                indexer_id = futures[future]
                try:
                    results.extend(future.result())
                except (ProwlarrError, httpx.HTTPError, ValueError, TimeoutError) as error:
                    # One broken indexer must not abort the complete search.
                    self.last_search_errors.append(f"indexer {indexer_id}: {error}")
        except TimeoutError:
            pending = [indexer_id for future, indexer_id in futures.items() if not future.done()]
            self.last_search_errors.extend(
                f"indexer {indexer_id}: timed out" for indexer_id in pending
            )
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        deduplicated: dict[str, dict[str, Any]] = {}
        for result in results:
            key = str(result.get("guid") or "")
            if not key:
                key = "|".join(
                    str(result.get(field, "")) for field in ("title", "size", "indexer")
                )
            deduplicated.setdefault(key, result)

        merged = list(deduplicated.values())
        if not merged and self.last_search_errors:
            raise ProwlarrError(
                "all Prowlarr indexer searches failed: " + "; ".join(self.last_search_errors)
            )
        return merged

    def _search_once(
        self,
        query: str,
        indexer_ids: list[int],
        limit: int,
    ) -> list[dict[str, Any]]:
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
