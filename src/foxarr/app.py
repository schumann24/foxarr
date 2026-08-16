"""FastAPI application exposing Foxarr's movie-only Radarr surface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .prowlarr import ProwlarrClient, ProwlarrError
from .selection import ReleaseSelectionError, parse_criteria, select_release
from .storage import MovieNotFoundError, MovieStore


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
        return [
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
            criteria = parse_criteria(payload)
            job = store.get_search_job(job_id)
            selected_index, selected_result = select_release(job["results"], criteria)
            criteria_payload = {
                "preferredProtocols": list(criteria.preferred_protocols),
                "minSeeders": criteria.min_seeders,
                "maxSize": criteria.max_size,
                "preferredLanguages": list(criteria.preferred_languages),
                "preferredQuality": list(criteria.preferred_quality),
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

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("foxarr.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7878")))
