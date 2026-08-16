"""Deterministic, read-only release selection for dry-run jobs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class ReleaseSelectionError(ValueError):
    """Raised when no stored release satisfies the selection criteria."""


_RESOLUTION_ORDER = {"480p": 480, "576p": 576, "720p": 720, "1080p": 1080, "2160p": 2160}


@dataclass(frozen=True)
class ReleaseSelectionCriteria:
    """Hard constraints and soft preferences for one release selection."""

    preferred_protocols: tuple[str, ...] = ("torrent",)
    min_seeders: int = 0
    min_size: int | None = None
    max_size: int | None = None
    preferred_languages: tuple[str, ...] = ()
    preferred_quality: tuple[str, ...] = ()
    allowed_resolutions: tuple[str, ...] = ()
    preferred_resolutions: tuple[str, ...] = ()
    min_resolution: str | None = None
    max_resolution: str | None = None
    allowed_video_codecs: tuple[str, ...] = ()
    preferred_video_codecs: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    preferred_sources: tuple[str, ...] = ()
    allowed_hdr: tuple[str, ...] = ()
    preferred_hdr: tuple[str, ...] = ()
    allowed_audio_codecs: tuple[str, ...] = ()
    preferred_audio_codecs: tuple[str, ...] = ()


def parse_criteria(payload: dict[str, Any]) -> ReleaseSelectionCriteria:
    """Validate the selection criteria accepted by the internal endpoint.

    ``allowed*`` and min/max values are hard filters. ``preferred*`` values
    only affect ranking after a release passes all hard filters.
    """

    def strings(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        value = payload.get(name, list(default))
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{name} must be an array of strings")
        return tuple(_normalize(item) for item in value if item.strip())

    def positive_int(name: str) -> int | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TypeError(f"{name} must be a positive integer")
        return value

    min_seeders = payload.get("minSeeders", 0)
    if not isinstance(min_seeders, int) or isinstance(min_seeders, bool) or min_seeders < 0:
        raise TypeError("minSeeders must be a non-negative integer")

    min_size = positive_int("minSize")
    max_size = positive_int("maxSize")
    if min_size is not None and max_size is not None and min_size > max_size:
        raise ValueError("minSize cannot be greater than maxSize")

    min_resolution = _parse_resolution_bound(payload.get("minResolution"), "minResolution")
    max_resolution = _parse_resolution_bound(payload.get("maxResolution"), "maxResolution")
    if (
        min_resolution is not None
        and max_resolution is not None
        and _RESOLUTION_ORDER[min_resolution] > _RESOLUTION_ORDER[max_resolution]
    ):
        raise ValueError("minResolution cannot be greater than maxResolution")

    allowed_resolutions = tuple(
        _parse_resolution(value, "allowedResolutions")
        for value in strings("allowedResolutions")
    )
    preferred_resolutions = tuple(
        _parse_resolution(value, "preferredResolutions")
        for value in strings("preferredResolutions")
    )

    return ReleaseSelectionCriteria(
        preferred_protocols=strings("preferredProtocols", ("torrent",)),
        min_seeders=min_seeders,
        min_size=min_size,
        max_size=max_size,
        preferred_languages=strings("preferredLanguages"),
        preferred_quality=strings("preferredQuality"),
        allowed_resolutions=allowed_resolutions,
        preferred_resolutions=preferred_resolutions,
        min_resolution=min_resolution,
        max_resolution=max_resolution,
        allowed_video_codecs=_canonicalize(
            strings("allowedVideoCodecs"), _VIDEO_CODECS
        ),
        preferred_video_codecs=_canonicalize(
            strings("preferredVideoCodecs"), _VIDEO_CODECS
        ),
        allowed_sources=_canonicalize(strings("allowedSources"), _SOURCES),
        preferred_sources=_canonicalize(strings("preferredSources"), _SOURCES),
        allowed_hdr=_canonicalize(strings("allowedHdr"), _HDR),
        preferred_hdr=_canonicalize(strings("preferredHdr"), _HDR),
        allowed_audio_codecs=_canonicalize(
            strings("allowedAudioCodecs"), _AUDIO_CODECS
        ),
        preferred_audio_codecs=_canonicalize(
            strings("preferredAudioCodecs"), _AUDIO_CODECS
        ),
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
        protocol = _normalize(str(result.get("protocol", "")))
        media_info = extract_media_info(result)
        if not _passes_hard_filters(result, media_info, criteria, seeders, size, protocol):
            continue

        score = min(seeders, 100)
        if protocol in preferred_protocols:
            score += 100
        score += _language_score(result, preferred_languages)
        score += _quality_score(result, criteria.preferred_quality)
        score += _preference_score(media_info, criteria)
        # Prefer more seeders, then smaller files, then the first Prowlarr result.
        sort_key = (score, seeders, -size if size else 0, -index)
        selected = dict(result)
        selected["mediaInfo"] = media_info
        selected["selectionScore"] = score
        candidates.append((sort_key, index, selected))

    if not candidates:
        raise ReleaseSelectionError("no release satisfies the selection criteria")
    _, index, selected = max(candidates, key=lambda candidate: candidate[0])
    return index, selected


def extract_media_info(result: dict[str, Any]) -> dict[str, list[str] | None]:
    """Extract normalized technical metadata from explicit fields and release title."""
    title = str(result.get("title", ""))
    return {
        "resolution": _extract_resolution(result, title),
        "videoCodecs": _extract_values(
            result, title, "videoCodec", "videoCodecs", "codec", "codecs", patterns=_VIDEO_CODECS
        ),
        "sources": _extract_values(
            result, title, "source", "sources", "releaseType", patterns=_SOURCES
        ),
        "hdr": _extract_values(
            result, title, "hdr", "dynamicRange", "dynamicRanges", patterns=_HDR
        ),
        "audioCodecs": _extract_values(
            result, title, "audioCodec", "audioCodecs", "audio", patterns=_AUDIO_CODECS
        ),
    }


def _passes_hard_filters(
    result: dict[str, Any],
    media_info: dict[str, list[str] | None],
    criteria: ReleaseSelectionCriteria,
    seeders: int,
    size: int,
    protocol: str,
) -> bool:
    if seeders < criteria.min_seeders:
        return False
    if criteria.min_size is not None and (size == 0 or size < criteria.min_size):
        return False
    if criteria.max_size is not None and (size == 0 or size > criteria.max_size):
        return False
    if criteria.preferred_protocols and protocol not in set(criteria.preferred_protocols):
        return False

    resolution = media_info["resolution"]
    if criteria.allowed_resolutions and resolution not in set(criteria.allowed_resolutions):
        return False
    if criteria.min_resolution and (
        resolution is None or _RESOLUTION_ORDER[resolution] < _RESOLUTION_ORDER[criteria.min_resolution]
    ):
        return False
    if criteria.max_resolution and (
        resolution is None or _RESOLUTION_ORDER[resolution] > _RESOLUTION_ORDER[criteria.max_resolution]
    ):
        return False

    for key, allowed in (
        ("videoCodecs", criteria.allowed_video_codecs),
        ("sources", criteria.allowed_sources),
        ("hdr", criteria.allowed_hdr),
        ("audioCodecs", criteria.allowed_audio_codecs),
    ):
        detected = media_info[key]
        if allowed and (not detected or not set(detected).intersection(allowed)):
            return False
    return True


def _preference_score(media_info: dict[str, list[str] | None], criteria: ReleaseSelectionCriteria) -> int:
    score = 0
    for key, preferred in (
        ("videoCodecs", criteria.preferred_video_codecs),
        ("sources", criteria.preferred_sources),
        ("hdr", criteria.preferred_hdr),
        ("audioCodecs", criteria.preferred_audio_codecs),
    ):
        detected = media_info[key] or []
        for rank, value in enumerate(preferred):
            if value in detected:
                score += max(1, 25 - rank * 3)
                break
    resolution = media_info["resolution"]
    if resolution and criteria.preferred_resolutions:
        for rank, value in enumerate(criteria.preferred_resolutions):
            if resolution == value:
                score += max(1, 35 - rank * 4)
                break
    return score


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


def _normalize(value: str) -> str:
    return re.sub(r"[._\-]+", " ", value.strip().lower()).strip()


def _parse_resolution(value: str, field: str) -> str:
    normalized = _normalize(value)
    aliases = {"4k": "2160p", "uhd": "2160p"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in _RESOLUTION_ORDER:
        raise ValueError(f"{field} contains unsupported resolution: {value}")
    return normalized


def _parse_resolution_bound(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a resolution string")
    return _parse_resolution(value, field)


def _extract_resolution(result: dict[str, Any], title: str) -> str | None:
    for key in ("resolution", "resolutions"):
        value = result.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                match = re.search(r"(2160p|1080p|720p|576p|480p|4k|uhd)", item.lower())
                if match:
                    return _parse_resolution(match.group(1), "resolution")
    match = re.search(r"(?<!\d)(2160p|1080p|720p|576p|480p|4k|uhd)(?!\w)", title.lower())
    return _parse_resolution(match.group(1), "resolution") if match else None


_VIDEO_CODECS = {
    "x265": "hevc", "h265": "hevc", "h 265": "hevc", "hevc": "hevc",
    "x264": "h264", "h264": "h264", "h 264": "h264", "avc": "h264",
    "av1": "av1", "vp9": "vp9",
}
_SOURCES = {
    "web dl": "web-dl", "webdl": "web-dl", "web rip": "webrip", "webrip": "webrip",
    "bluray": "bluray", "blu ray": "bluray", "bdrip": "bdrip", "bdremux": "bdremux",
    "remux": "remux", "dvdrip": "dvdrip",
}
_HDR = {"dolby vision": "dv", "hdr10+": "hdr10+", "hdr10": "hdr10", "hdr": "hdr", "sdr": "sdr"}
_AUDIO_CODECS = {
    "truehd": "truehd", "dts hd": "dts-hd", "dts": "dts", "atmos": "atmos",
    "eac3": "eac3", "ddp": "eac3", "ac3": "ac3", "dd": "ac3", "aac": "aac",
    "flac": "flac", "opus": "opus",
}


def _extract_values(
    result: dict[str, Any],
    title: str,
    *keys: str,
    patterns: dict[str, str],
) -> list[str]:
    values: set[str] = set()
    for key in keys:
        value = result.get(key)
        raw_values = value if isinstance(value, list) else [value]
        for item in raw_values:
            if isinstance(item, str):
                normalized = _normalize(item)
                for pattern, canonical in patterns.items():
                    if pattern in normalized:
                        values.add(canonical)
    normalized_title = _normalize(title)
    for pattern, canonical in patterns.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", normalized_title):
            values.add(canonical)
    # More specific labels must not also be reported as their generic parent:
    # DTS-HD is not a separate plain-DTS track for profile matching.
    if "dts-hd" in values:
        values.discard("dts")
    return sorted(values)


def _canonicalize(values: tuple[str, ...], patterns: dict[str, str]) -> tuple[str, ...]:
    """Map profile aliases such as ``DTS-HD`` and ``web-dl`` to canonical values."""
    canonical: list[str] = []
    for value in values:
        normalized = _normalize(value)
        mapped = next(
            (name for pattern, name in patterns.items() if pattern == normalized),
            normalized,
        )
        if mapped not in canonical:
            canonical.append(mapped)
    return tuple(canonical)
