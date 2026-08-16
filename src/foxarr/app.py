"""FastAPI application exposing Foxarr's movie-only Radarr surface."""

from __future__ import annotations

import json
import os
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .prowlarr import ProwlarrClient, ProwlarrError
from .selection import ReleaseSelectionError, parse_criteria, select_release
from .storage import MovieNotFoundError, MovieStore
from .transmission import (
    TransmissionClient,
    TransmissionConfirmationRequired,
    TransmissionError,
    TransmissionWorker,
    build_download_plan,
    build_resolved_submit_preview,
)


class FoxarrSettings:
    """Environment-backed settings for the small MVP service."""

    def __init__(
        self,
        database: str | Path | None = None,
        api_key: str | None = None,
        root_folder: str | None = None,
        quality_profile_id: int | None = None,
        quality_profile_name: str | None = None,
        prowlarr_url: str | None = None,
        prowlarr_api_key: str | None = None,
        dry_run: bool | None = None,
        download_dir: str | None = None,
        movie_download_dir: str | None = None,
        series_download_dir: str | None = None,
        transmission_url: str | None = None,
        selection_profiles: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.database = database or os.environ.get("FOXARR_DATABASE", "/data/foxarr.db")
        self.api_key = api_key if api_key is not None else os.environ.get("FOXARR_API_KEY", "")
        self.root_folder = root_folder or os.environ.get("FOXARR_ROOT_FOLDER", "/movies")
        self.quality_profile_id = quality_profile_id or int(
            os.environ.get("FOXARR_QUALITY_PROFILE_ID", "1")
        )
        self.quality_profile_name = quality_profile_name or os.environ.get(
            "FOXARR_QUALITY_PROFILE_NAME", "Any"
        )
        self.prowlarr_url = prowlarr_url or os.environ.get("FOXARR_PROWLARR_URL", "")
        self.prowlarr_api_key = (
            prowlarr_api_key
            if prowlarr_api_key is not None
            else os.environ.get("FOXARR_PROWLARR_API_KEY", "")
        )
        self.dry_run = (
            dry_run
            if dry_run is not None
            else os.environ.get("FOXARR_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}
        )
        self.download_dir = download_dir or os.environ.get("FOXARR_DOWNLOAD_DIR", "")
        self.movie_download_dir = movie_download_dir or os.environ.get(
            "FOXARR_TRANSMISSION_MOVIE_DIR",
            self.download_dir or "/home/blackfox/data/film",
        )
        self.series_download_dir = series_download_dir or os.environ.get(
            "FOXARR_TRANSMISSION_SERIES_DIR", "/home/blackfox/data/serial"
        )
        self.transmission_url = transmission_url or os.environ.get(
            "FOXARR_TRANSMISSION_RPC_URL", ""
        )
        if selection_profiles is not None:
            self.selection_profiles = selection_profiles
        else:
            raw_profiles = os.environ.get("FOXARR_SELECTION_PROFILES_JSON", "")
            if raw_profiles:
                try:
                    parsed_profiles = json.loads(raw_profiles)
                except JSONDecodeError as error:
                    raise ValueError("FOXARR_SELECTION_PROFILES_JSON must be valid JSON") from error
                if not isinstance(parsed_profiles, dict):
                    raise ValueError("FOXARR_SELECTION_PROFILES_JSON must be a JSON object")
                self.selection_profiles = {
                    str(profile_id): profile
                    for profile_id, profile in parsed_profiles.items()
                    if isinstance(profile, dict)
                }
            else:
                self.selection_profiles = {
                    str(self.quality_profile_id): {
                        "name": self.quality_profile_name,
                        "criteria": {},
                    }
                }


def create_app(
    store: MovieStore | None = None,
    settings: FoxarrSettings | None = None,
) -> FastAPI:
    """Create an isolated Foxarr application instance."""
    settings = settings or FoxarrSettings()
    store = store or MovieStore(settings.database)
    app = FastAPI(
        title="Foxarr",
        version="0.1.0.dev0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store
    app.state.settings = settings

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if settings.api_key:
            supplied = request.query_params.get("apikey") or request.headers.get("X-Api-Key", "")
            if supplied != settings.api_key:
                return JSONResponse(status_code=401, content={"message": "Invalid API key"})
        return await call_next(request)

    @app.get("/api/{ver}/system/status")
    async def system_status(ver: str) -> dict[str, Any]:
        return {
            "appName": "Radarr",
            "version": "5.8.0.0",
            "instanceName": "foxarr",
        }

    @app.get("/api/{ver}/health")
    async def health(ver: str) -> list[Any]:
        return []

    @app.get("/api/{ver}/qualityProfile")
    async def quality_profile(ver: str) -> list[dict[str, Any]]:
        profiles = []
        for profile_id, profile in settings.selection_profiles.items():
            try:
                numeric_id = int(profile_id)
            except ValueError:
                continue
            profiles.append(
                {
                    "id": numeric_id,
                    "name": str(profile.get("name", f"Profile {numeric_id}")),
                    "upgradeAllowed": False,
                    "cutoff": 0,
                    "items": [],
                    "language": {"id": 1, "name": "Any"},
                }
            )
        return profiles or [
            {
                "id": settings.quality_profile_id,
                "name": settings.quality_profile_name,
                "upgradeAllowed": False,
                "cutoff": 0,
                "items": [],
                "language": {"id": 1, "name": "Any"},
            }
        ]

    # Keep the lowercase spelling too; Seerr uses the camel-case endpoint,
    # while some Radarr-compatible clients still probe the lowercase variant.
    @app.get("/api/{ver}/qualityprofile")
    async def quality_profile_lowercase(ver: str) -> list[dict[str, Any]]:
        return await quality_profile(ver)

    @app.get("/api/{ver}/rootfolder")
    async def root_folder(ver: str) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "path": settings.root_folder,
                "accessible": True,
                "freeSpace": 100_000_000_000,
                "totalSpace": 200_000_000_000,
            }
        ]

    @app.get("/api/{ver}/tag")
    async def tags(ver: str) -> list[Any]:
        return []

    @app.get("/api/{ver}/diskspace")
    async def disk_space(ver: str) -> list[dict[str, Any]]:
        return [
            {
                "path": settings.root_folder,
                "label": "foxarr",
                "freeSpace": 100_000_000_000,
                "totalSpace": 200_000_000_000,
            }
        ]

    @app.get("/api/{ver}/movie/lookup")
    async def movie_lookup(ver: str, term: str = "") -> list[dict[str, Any]]:
        return store.lookup(term)

    @app.get("/api/{ver}/movie")
    async def movie_list(
        ver: str,
        tmdb_id: int | None = Query(default=None, alias="tmdbId"),
    ) -> list[dict[str, Any]]:
        return store.list_movies(tmdb_id)

    @app.get("/api/{ver}/movie/{movie_id}")
    async def movie_get(ver: str, movie_id: int) -> dict[str, Any]:
        try:
            return store.get_movie(movie_id)
        except MovieNotFoundError as error:
            raise HTTPException(status_code=404, detail="Movie not found") from error

    @app.post("/api/{ver}/movie")
    async def movie_create(ver: str, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            return store.create_or_update(payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/internal/dry-run/search")
    async def dry_run_search(request: Request) -> dict[str, Any]:
        if not settings.dry_run:
            raise HTTPException(status_code=409, detail="dry-run mode is disabled")
        if not settings.prowlarr_url or not settings.prowlarr_api_key:
            raise HTTPException(status_code=503, detail="Prowlarr is not configured")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be a non-empty string")
            indexer_ids = payload.get("indexerIds", [])
            if not isinstance(indexer_ids, list) or not all(
                isinstance(value, int) and not isinstance(value, bool) for value in indexer_ids
            ):
                raise TypeError("indexerIds must be an array of integers")
            limit = payload.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise TypeError("limit must be an integer")
            job_id = store.create_search_job(query.strip(), indexer_ids, limit)
            try:
                results = ProwlarrClient(
                    settings.prowlarr_url,
                    settings.prowlarr_api_key,
                ).search(query, indexer_ids=indexer_ids, limit=limit)
                return store.finish_search_job(job_id, results)
            except (ProwlarrError, TypeError, ValueError) as error:
                return store.finish_search_job(job_id, [], error=str(error))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/internal/dry-run/search/{job_id}")
    async def dry_run_search_status(job_id: int) -> dict[str, Any]:
        try:
            return store.get_search_job(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Search job not found") from error

    @app.post("/api/internal/dry-run/search/{job_id}/select")
    async def dry_run_select_release(job_id: int, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            criteria_payload = dict(payload)
            profile_id = criteria_payload.pop("qualityProfileId", None)
            if profile_id is None:
                profile_id = settings.quality_profile_id
            if profile_id is not None:
                if not isinstance(profile_id, int) or isinstance(profile_id, bool):
                    raise TypeError("qualityProfileId must be an integer")
                profile = settings.selection_profiles.get(str(profile_id))
                if profile is None and payload.get("qualityProfileId") is not None:
                    raise ValueError(f"unknown quality profile: {profile_id}")
                profile_criteria = profile.get("criteria", {}) if profile else {}
                if not isinstance(profile_criteria, dict):
                    raise TypeError("quality profile criteria must be an object")
                merged_criteria = dict(profile_criteria)
                merged_criteria.update(criteria_payload)
                criteria_payload = merged_criteria
            criteria = parse_criteria(criteria_payload)
            job = store.get_search_job(job_id)
            selected_index, selected_result = select_release(job["results"], criteria)
            criteria_payload = {
                "qualityProfileId": profile_id,
                "preferredProtocols": list(criteria.preferred_protocols),
                "minSeeders": criteria.min_seeders,
                "minSize": criteria.min_size,
                "maxSize": criteria.max_size,
                "preferredLanguages": list(criteria.preferred_languages),
                "preferredQuality": list(criteria.preferred_quality),
                "allowedResolutions": list(criteria.allowed_resolutions),
                "preferredResolutions": list(criteria.preferred_resolutions),
                "minResolution": criteria.min_resolution,
                "maxResolution": criteria.max_resolution,
                "allowedVideoCodecs": list(criteria.allowed_video_codecs),
                "preferredVideoCodecs": list(criteria.preferred_video_codecs),
                "allowedSources": list(criteria.allowed_sources),
                "preferredSources": list(criteria.preferred_sources),
                "allowedHdr": list(criteria.allowed_hdr),
                "preferredHdr": list(criteria.preferred_hdr),
                "allowedAudioCodecs": list(criteria.allowed_audio_codecs),
                "preferredAudioCodecs": list(criteria.preferred_audio_codecs),
            }
            return store.select_search_job(
                job_id, selected_index, selected_result, criteria_payload
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Search job not found") from error
        except ReleaseSelectionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/internal/dry-run/search/{job_id}/plan")
    async def dry_run_plan_download(job_id: int, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            download_client = payload.get("downloadClient", "transmission")
            if not isinstance(download_client, str):
                raise TypeError("downloadClient must be a string")
            media_type = payload.get("mediaType", "movie")
            if media_type not in {"movie", "series"}:
                raise ValueError("mediaType must be movie or series")
            default_dir = (
                settings.movie_download_dir
                if media_type == "movie"
                else settings.series_download_dir
            )
            download_dir = payload.get("downloadDir", default_dir)
            if not isinstance(download_dir, str):
                raise TypeError("downloadDir must be a string")
            job = store.get_search_job(job_id)
            if job["status"] != "selected" or job["selectedResult"] is None:
                raise ValueError("only selected search jobs can be planned")
            plan = build_download_plan(
                job_id,
                job["selectedIndex"],
                job["selectedResult"],
                download_dir,
                download_client,
            )
            plan["mediaType"] = media_type
            return store.plan_search_job(job_id, plan)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Search job not found") from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/internal/dry-run/search/{job_id}/submit-preview")
    async def dry_run_submit_preview(job_id: int, request: Request) -> dict[str, Any]:
        """Resolve a selected release and preview RPC without submitting it."""
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            media_type = payload.get("mediaType", "movie")
            if media_type not in {"movie", "series"}:
                raise ValueError("mediaType must be movie or series")
            default_dir = (
                settings.movie_download_dir
                if media_type == "movie"
                else settings.series_download_dir
            )
            download_dir = payload.get("downloadDir", default_dir)
            if not isinstance(download_dir, str):
                raise TypeError("downloadDir must be a string")
            job = store.get_search_job(job_id)
            if job["status"] not in {"selected", "download_planned"}:
                raise ValueError("only selected search jobs can be resolved")
            selected = job["selectedResult"]
            if not isinstance(selected, dict) or not selected.get("guid"):
                raise ValueError("selected result has no guid")
            if not settings.prowlarr_url or not settings.prowlarr_api_key:
                raise ValueError("Prowlarr is not configured")
            plan = build_download_plan(
                job_id,
                job["selectedIndex"],
                selected,
                download_dir,
            )
            plan["mediaType"] = media_type
            _, download_url = ProwlarrClient(
                settings.prowlarr_url,
                settings.prowlarr_api_key,
            ).resolve_download_url(
                job["query"],
                selected["guid"],
                indexer_ids=job["indexerIds"],
            )
            # The URL is intentionally never passed to storage and never returned.
            return build_resolved_submit_preview(plan, download_url)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Search job not found") from error
        except (ProwlarrError, TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/internal/dry-run/search/{job_id}/transmission")
    async def update_transmission_status(job_id: int, request: Request) -> dict[str, Any]:
        """Persist a worker's safe Transmission status snapshot."""
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            torrent_id = payload.get("torrentId")
            status = payload.get("status")
            percent_done = payload.get("percentDone", 0)
            if not isinstance(torrent_id, int) or isinstance(torrent_id, bool) or torrent_id < 1:
                raise TypeError("torrentId must be a positive integer")
            if not isinstance(status, str) or not status.strip():
                raise TypeError("status must be a non-empty string")
            if not isinstance(percent_done, (int, float)) or isinstance(percent_done, bool):
                raise TypeError("percentDone must be a number")
            return store.update_transmission_status(
                job_id,
                torrent_id,
                status.strip(),
                float(percent_done),
                payload.get("error"),
                payload.get("downloadDir"),
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Search job not found") from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    def transmission_worker() -> TransmissionWorker:
        if not settings.transmission_url:
            raise HTTPException(status_code=503, detail="Transmission is not configured")
        return TransmissionWorker(TransmissionClient(settings.transmission_url))

    def selected_job(job_id: int) -> dict[str, Any]:
        try:
            job = store.get_search_job(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Search job not found") from error
        if job["status"] not in {"selected", "download_planned", "paused", "downloading"}:
            raise HTTPException(
                status_code=409,
                detail="only selected or submitted search jobs can use Transmission",
            )
        if not isinstance(job.get("selectedResult"), dict):
            raise HTTPException(status_code=409, detail="search job has no selected result")
        return job

    @app.post("/api/internal/transmission/search/{job_id}/submit")
    async def transmission_submit(job_id: int, request: Request) -> dict[str, Any]:
        """Resolve and submit one selected release, always paused.

        This is an external side effect. ``confirm`` must be exactly true;
        otherwise no Prowlarr resolve or Transmission call is made.
        """
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            if payload.get("confirm") is not True:
                raise TransmissionConfirmationRequired(
                    "Transmission submit requires explicit confirmation"
                )
            media_type = payload.get("mediaType", "movie")
            if media_type not in {"movie", "series"}:
                raise ValueError("mediaType must be movie or series")
            job = selected_job(job_id)
            selected = job["selectedResult"]
            if not selected.get("guid"):
                raise ValueError("selected result has no guid")
            if not settings.prowlarr_url or not settings.prowlarr_api_key:
                raise ValueError("Prowlarr is not configured")
            default_dir = (
                settings.movie_download_dir
                if media_type == "movie"
                else settings.series_download_dir
            )
            download_dir = payload.get("downloadDir", default_dir)
            if not isinstance(download_dir, str):
                raise TypeError("downloadDir must be a string")
            _, download_url = ProwlarrClient(
                settings.prowlarr_url,
                settings.prowlarr_api_key,
            ).resolve_download_url(
                job["query"],
                selected["guid"],
                indexer_ids=job["indexerIds"],
            )
            result = transmission_worker().submit_paused(
                job_id,
                download_url,
                download_dir,
                media_type,
            )
            torrent = result["torrent"]
            updated = store.update_transmission_status(
                job_id,
                int(torrent["torrentId"]),
                str(torrent["status"]),
                float(torrent.get("percentDone", 0)),
                torrent.get("error"),
                torrent.get("downloadDir") or download_dir,
            )
            return {
                "job": updated,
                "created": result["created"],
                "mediaType": result["mediaType"],
                "labels": result["labels"],
                "torrent": torrent,
                "paused": True,
            }
        except HTTPException:
            raise
        except TransmissionConfirmationRequired as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProwlarrError, TransmissionError, TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/internal/transmission/search/{job_id}/snapshot")
    async def transmission_snapshot(job_id: int) -> dict[str, Any]:
        job = selected_job(job_id)
        torrent_id = job["transmission"]["torrentId"]
        if not isinstance(torrent_id, int):
            raise HTTPException(status_code=409, detail="search job has no Transmission torrent")
        try:
            snapshot = transmission_worker().snapshot(torrent_id)
            updated = store.update_transmission_status(
                job_id,
                torrent_id,
                str(snapshot["status"]),
                float(snapshot.get("percentDone", 0)),
                snapshot.get("error"),
                snapshot.get("downloadDir"),
            )
            return {"job": updated, "torrent": snapshot}
        except TransmissionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    async def transmission_control(job_id: int, request: Request, action: str) -> dict[str, Any]:
        job = selected_job(job_id)
        torrent_id = job["transmission"]["torrentId"]
        if not isinstance(torrent_id, int):
            raise HTTPException(status_code=409, detail="search job has no Transmission torrent")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            if payload.get("confirm") is not True:
                raise TransmissionConfirmationRequired(
                    f"Transmission {action} requires explicit confirmation"
                )
            worker = transmission_worker()
            snapshot = (
                worker.start(torrent_id, confirm=True)
                if action == "start"
                else worker.stop(torrent_id, confirm=True)
            )
            updated = store.update_transmission_status(
                job_id,
                torrent_id,
                str(snapshot["status"]),
                float(snapshot.get("percentDone", 0)),
                snapshot.get("error"),
                snapshot.get("downloadDir"),
            )
            return {"job": updated, "torrent": snapshot}
        except HTTPException:
            raise
        except TransmissionConfirmationRequired as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (TransmissionError, TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/internal/transmission/search/{job_id}/start")
    async def transmission_start(job_id: int, request: Request) -> dict[str, Any]:
        return await transmission_control(job_id, request, "start")

    @app.post("/api/internal/transmission/search/{job_id}/stop")
    async def transmission_stop(job_id: int, request: Request) -> dict[str, Any]:
        return await transmission_control(job_id, request, "stop")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("foxarr.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7878")))
