"""Deterministic, read-only release selection for dry-run jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ReleaseSelectionError(ValueError):
    """Raised when no stored release satisfies the selection criteria."""


@dataclass(frozen=True)
class ReleaseSelectionCriteria:
    """Safe selection preferences; these never trigger provider side effects."""

    preferred_protocols: tuple[str, ...] = ("torrent",)
    min_seeders: int = 0
    max_size: int | None = None
    preferred_languages: tuple[str, ...] = ()
    preferred_quality: tuple[str, ...] = ()


def parse_criteria(payload: dict[str, Any]) -> ReleaseSelectionCriteria:
    """Validate the small public criteria object used by the selection endpoint."""

    def strings(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = payload.get(name, list(default))
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{name} must be an array of strings")
        return tuple(item.strip().lower() for item in value if item.strip())

    min_seeders = payload.get("minSeeders", 0)
    if not isinstance(min_seeders, int) or isinstance(min_seeders, bool) or min_seeders < 0:
        raise TypeError("minSeeders must be a non-negative integer")

    max_size = payload.get("maxSize")
    if max_size is not None and (
        not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < 1
    ):
        raise TypeError("maxSize must be a positive integer")

    return ReleaseSelectionCriteria(
        preferred_protocols=strings("preferredProtocols", ("torrent",)),
        min_seeders=min_seeders,
        max_size=max_size,
        preferred_languages=strings("preferredLanguages", ()),
        preferred_quality=strings("preferredQuality", ()),
    )


def select_release(
    results: list[dict[str, Any]], criteria: ReleaseSelectionCriteria
) -> tuple[int, dict[str, Any]]:
    """Return ``(result_index, safe_result_with_score)`` for the best candidate."""
    candidates: list[tuple[tuple[int, int, int, int], int, dict[str, Any]]] = []
    preferred_protocols = set(criteria.preferred_protocols)
    preferred_languages = set(criteria.preferred_languages)

    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        seeders = _non_negative_int(result.get("seeders"))
        size = _non_negative_int(result.get("size"))
        protocol = str(result.get("protocol", "")).strip().lower()
        if seeders < criteria.min_seeders:
            continue
        if criteria.max_size is not None and (size == 0 or size > criteria.max_size):
            continue
        if preferred_protocols and protocol not in preferred_protocols:
            continue

        score = min(seeders, 100)
        if protocol in preferred_protocols:
            score += 100
        score += _language_score(result, preferred_languages)
        score += _quality_score(result, criteria.preferred_quality)
        # Prefer more seeders, then smaller files, then the first Prowlarr result.
        sort_key = (score, seeders, -size if size else 0, -index)
        selected = dict(result)
        selected["selectionScore"] = score
        candidates.append((sort_key, index, selected))

    if not candidates:
        raise ReleaseSelectionError("no release satisfies the selection criteria")
    _, index, selected = max(candidates, key=lambda candidate: candidate[0])
    return index, selected


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _language_score(result: dict[str, Any], preferred: set[str]) -> int:
    if not preferred:
        return 0
    values: list[str] = []
    for key in ("language", "languages"):
        value = result.get(key)
        if isinstance(value, str):
            values.append(value.lower())
        elif isinstance(value, list):
            values.extend(item.lower() for item in value if isinstance(item, str))
    title = result.get("title")
    if isinstance(title, str):
        values.append(title.lower())
    return 50 if any(language in value for language in preferred for value in values) else 0


def _quality_score(result: dict[str, Any], preferred: tuple[str, ...]) -> int:
    values: list[str] = []
    for key in ("quality", "resolution", "title"):
        value = result.get(key)
        if isinstance(value, str):
            values.append(value.lower())
    haystack = " ".join(values)
    if preferred:
        for rank, quality in enumerate(preferred):
            if quality in haystack:
                return max(1, 30 - rank)
        return 0
    for quality, score in (("2160p", 30), ("4k", 30), ("1080p", 20), ("720p", 10)):
        if quality in haystack:
            return score
    return 0
