"""Foxarr mock *arr server.

Emulates a minimal Radarr/Sonarr v3 API surface to observe what Seerr
actually calls and expects. Logs every request as JSONL to stdout.

Run:
    ROLE=radarr uvicorn server:app --port 7878
    ROLE=sonarr uvicorn server:app --port 8989

ROLE is read from the environment (radarr | sonarr).
State is kept in memory; an optional JSON state file can be used with
FOXARR_MOCK_STATE=/path/to/state.json to survive restarts.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROLE = os.environ.get("ROLE", "radarr").lower()
STATE_FILE = os.environ.get("FOXARR_MOCK_STATE")
LOG_HEADERS = os.environ.get("FOXARR_MOCK_LOG_HEADERS", "0") == "1"

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

state: dict[str, Any] = {
    "movies": {},  # id -> movie object (radarr)
    "series": {},  # id -> series object (sonarr)
    "episodes": {},  # (series_id, season, episode) -> episode object
    "commands": {},  # command_id -> command object
    "seq": 1000,
}

if STATE_FILE and os.path.exists(STATE_FILE):
    with open(STATE_FILE) as fh:
        saved = json.load(fh)
    state.update(saved)
    # Episode keys are tuples in memory, but JSON can only represent string
    # keys. Persist episodes as a list and rebuild the in-memory index.
    saved_episodes = saved.get("episodes", [])
    if isinstance(saved_episodes, list):
        state["episodes"] = {
            (int(e["seriesId"]), int(e["seasonNumber"]), int(e["episodeNumber"])): e
            for e in saved_episodes
        }


def _save_state() -> None:
    if STATE_FILE:
        tmp = STATE_FILE + ".tmp"
        serializable = dict(state)
        serializable["episodes"] = list(state["episodes"].values())
        with open(tmp, "w") as fh:
            json.dump(serializable, fh, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)


def _next_id() -> int:
    state["seq"] += 1
    return state["seq"]


# ---------------------------------------------------------------------------
# App + request logging
# ---------------------------------------------------------------------------

app = FastAPI(title=f"foxarr-mock-{ROLE}", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = b""
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    started = time.monotonic()
    response = await call_next(request)
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)

    # Collect response body for logging (Starlette hands us a streaming
    # response, so drain the iterator and rebuild a plain Response).
    resp_body = b""
    if hasattr(response, "body_iterator"):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        resp_body = b"".join(chunks)
        from starlette.responses import Response

        response = Response(
            content=resp_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
    elif hasattr(response, "body"):
        resp_body = response.body or b""

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "role": ROLE,
        "method": request.method,
        "path": request.url.path,
        "query": str(request.query_params) or None,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "request_body": body.decode("utf-8", "replace") if body else None,
        "response_body": resp_body.decode("utf-8", "replace") if resp_body else None,
    }
    if LOG_HEADERS:
        entry["headers"] = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("authorization", "x-api-key", "cookie")
        }
    print(json.dumps(entry, ensure_ascii=False), flush=True)

    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_ver(request: Request) -> str:
    """Return the API version segment, e.g. 'v3' from /api/v3/..."""
    parts = request.url.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "api":
        return parts[1]
    return "v3"


def _lookup_from_term(term: str) -> dict[str, Any]:
    """Parse term like tmdb:12345 / tvdb:12345 into a fake media object."""
    _, _, raw_id = term.partition(":")
    raw_id = raw_id.strip() or "1"
    try:
        numeric_id = int(raw_id)
    except ValueError:
        numeric_id = 1

    if ROLE == "radarr":
        return {
            "id": 0,
            "tmdbId": numeric_id,
            "imdbId": f"tt{numeric_id:07d}",
            "title": f"Mock Movie {numeric_id}",
            "originalTitle": f"Mock Movie {numeric_id}",
            "sortTitle": f"mock movie {numeric_id}",
            "year": 2026,
            "overview": "Mock movie entry generated by foxarr-mock.",
            "status": "released",
            "monitored": True,
            "qualityProfileId": 1,
            "rootFolderPath": "/movies",
            "minimumAvailability": "released",
            "hasFile": False,
            "isAvailable": True,
            "images": [],
            "genres": ["Mock"],
            "tags": [],
            "added": "2026-01-01T00:00:00Z",
            "statistics": {"movieFileCount": 0, "sizeOnDisk": 0},
        }
    return {
        "id": 0,
        "tvdbId": numeric_id,
        "tvMazeId": numeric_id,
        "imdbId": f"tt{numeric_id:07d}",
        "title": f"Mock Series {numeric_id}",
        "sortTitle": f"mock series {numeric_id}",
        "year": 2026,
        "overview": "Mock series entry generated by foxarr-mock.",
        "status": "continuing",
        "monitored": True,
        "seriesType": "standard",
        "seasonFolder": True,
        "qualityProfileId": 1,
        "rootFolderPath": "/tv",
        "seasons": [
            {"seasonNumber": 1, "monitored": True},
            {"seasonNumber": 2, "monitored": True},
        ],
        "images": [],
        "genres": ["Mock"],
        "tags": [],
        "added": "2026-01-01T00:00:00Z",
        "statistics": {"episodeFileCount": 0, "sizeOnDisk": 0},
    }


def _episodes_for(series_id: int, seasons: list[dict]) -> list[dict]:
    out = []
    for s in seasons:
        season_number = s.get("seasonNumber", 1)
        if not s.get("monitored", True):
            continue
        for ep in range(1, 4):
            key = (series_id, season_number, ep)
            if key not in state["episodes"]:
                state["episodes"][key] = {
                    "id": _next_id(),
                    "seriesId": series_id,
                    "seasonNumber": season_number,
                    "episodeNumber": ep,
                    "title": f"S{season_number:02d}E{ep:02d}",
                    "overview": None,
                    "airDate": "2026-01-01",
                    "monitored": True,
                    "hasFile": False,
                    "episodeFile": None,
                }
            out.append(state["episodes"][key])
    return out


def _apply_series_body(series: dict[str, Any], body: dict[str, Any]) -> None:
    """Apply Sonarr's create payload to an existing or new series."""
    series["monitored"] = body.get("monitored", series.get("monitored", True))
    series["qualityProfileId"] = body.get("qualityProfileId", series.get("qualityProfileId", 1))
    series["rootFolderPath"] = body.get("rootFolderPath", series.get("rootFolderPath", "/tv"))
    series["seriesType"] = body.get("seriesType", series.get("seriesType", "standard"))
    series["seasonFolder"] = body.get("seasonFolder", series.get("seasonFolder", True))
    series["tags"] = body.get("tags", series.get("tags", []))

    requested = body.get("seasons")
    if requested is not None:
        requested_by_number = {
            int(s["seasonNumber"]): bool(s.get("monitored", False))
            for s in requested
        }
        known_numbers = {int(s["seasonNumber"]) for s in series.get("seasons", [])}
        for season in series["seasons"]:
            number = int(season["seasonNumber"])
            if number in requested_by_number:
                season["monitored"] = requested_by_number[number]
        for number, monitored in requested_by_number.items():
            if number not in known_numbers:
                series["seasons"].append({"seasonNumber": number, "monitored": monitored})

        # Keep episode state aligned with the season monitoring selection.
        selected = {
            int(s["seasonNumber"]): bool(s.get("monitored", False))
            for s in series["seasons"]
        }
        state["episodes"] = {
            key: episode
            for key, episode in state["episodes"].items()
            if key[0] != int(series["id"]) or selected.get(key[1], False)
        }
        _episodes_for(int(series["id"]), series["seasons"])

    series["addOptions"] = body.get("addOptions", series.get("addOptions", {}))


# ---------------------------------------------------------------------------
# Radarr endpoints
# ---------------------------------------------------------------------------


@app.get("/api/{ver}/system/status")
async def system_status(ver: str):
    if ROLE == "radarr":
        return {"appName": "Radarr", "version": "5.8.0.0", "instanceName": "foxarr-mock"}
    return {"appName": "Sonarr", "version": "4.0.8.0", "instanceName": "foxarr-mock"}


@app.get("/api/{ver}/health")
async def health(ver: str):
    return []


@app.get("/api/{ver}/diskspace")
async def diskspace(ver: str):
    return [{"path": "/movies" if ROLE == "radarr" else "/tv", "label": "mock", "freeSpace": 100000000000, "totalSpace": 200000000000}]


@app.get("/api/{ver}/rootfolder")
async def rootfolder(ver: str):
    return [
        {
            "id": 1,
            "path": "/movies" if ROLE == "radarr" else "/tv",
            "accessible": True,
            "freeSpace": 100000000000,
            "totalSpace": 200000000000,
        }
    ]


@app.get("/api/{ver}/qualityprofile")
async def qualityprofile(ver: str):
    return [
        {
            "id": 1,
            "name": "Any",
            "upgradeAllowed": False,
            "cutoff": 0,
            "items": [{"quality": {"id": 10, "name": "WEBDL-1080p"}, "items": [], "allowed": True}],
            "language": {"id": 1, "name": "Any"},
        }
    ]


@app.get("/api/{ver}/qualityProfile")
async def quality_profile_camel(ver: str):
    return await qualityprofile(ver)


@app.get("/api/{ver}/tag")
async def tags(ver: str):
    return []


@app.get("/api/{ver}/languageprofile")
async def languageprofile(ver: str):
    # Only meaningful for Sonarr v3 / Radarr v3; harmless otherwise.
    return [{"id": 1, "name": "Any", "upgradeAllowed": False, "cutoff": {"id": 1, "name": "Any"}, "languages": [{"id": 1, "name": "Any"}]}]


@app.get("/api/{ver}/movie/lookup")
async def movie_lookup(ver: str, term: str = ""):
    obj = _lookup_from_term(term)
    obj["id"] = 0
    return [obj]


@app.get("/api/{ver}/movie")
async def movie_list(ver: str, tmdbId: int | None = None):
    movies = list(state["movies"].values())
    if tmdbId is not None:
        movies = [m for m in movies if m["tmdbId"] == tmdbId]
    return movies


@app.get("/api/{ver}/movie/{movie_id}")
async def movie_get(ver: str, movie_id: int):
    m = state["movies"].get(str(movie_id))
    if not m:
        return JSONResponse(status_code=404, content={"message": "Movie not found"})
    return m


@app.post("/api/{ver}/movie")
async def movie_create(ver: str, request: Request):
    body = await request.json()
    tmdb_id = body.get("tmdbId") or 1
    existing = [m for m in state["movies"].values() if m["tmdbId"] == tmdb_id]
    if existing:
        return existing[0]
    movie = _lookup_from_term(f"tmdb:{tmdb_id}")
    movie["id"] = _next_id()
    movie["monitored"] = body.get("monitored", True)
    movie["qualityProfileId"] = body.get("qualityProfileId", 1)
    movie["rootFolderPath"] = body.get("rootFolderPath", "/movies")
    movie["minimumAvailability"] = body.get("minimumAvailability", "released")
    movie["tags"] = body.get("tags", [])
    movie["added"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["movies"][str(movie["id"])] = movie
    _save_state()
    return movie


@app.delete("/api/{ver}/movie/{movie_id}")
async def movie_delete(ver: str, movie_id: int):
    state["movies"].pop(str(movie_id), None)
    _save_state()


# ---------------------------------------------------------------------------
# Sonarr endpoints
# ---------------------------------------------------------------------------


@app.get("/api/{ver}/series/lookup")
async def series_lookup(ver: str, term: str = ""):
    obj = _lookup_from_term(term)
    existing = [s for s in state["series"].values() if s["tvdbId"] == obj["tvdbId"]]
    if existing:
        # Sonarr returns the existing library item from lookup. Seerr uses a
        # positive id here to choose its update-series path instead of POST.
        return [existing[0]]
    obj["id"] = 0
    return [obj]


@app.get("/api/{ver}/series")
async def series_list(ver: str, tvdbId: int | None = None):
    series = list(state["series"].values())
    if tvdbId is not None:
        series = [s for s in series if s["tvdbId"] == tvdbId]
    return series


@app.get("/api/{ver}/series/{series_id}")
async def series_get(ver: str, series_id: int):
    s = state["series"].get(str(series_id))
    if not s:
        return JSONResponse(status_code=404, content={"message": "Series not found"})
    return s


@app.post("/api/{ver}/series")
async def series_create(ver: str, request: Request):
    body = await request.json()
    tvdb_id = body.get("tvdbId") or body.get("tvMazeId") or 1
    existing = [s for s in state["series"].values() if s["tvdbId"] == tvdb_id]
    if existing:
        _apply_series_body(existing[0], body)
        _save_state()
        return existing[0]
    series = _lookup_from_term(f"tvdb:{tvdb_id}")
    series["id"] = _next_id()
    _apply_series_body(series, body)

    state["series"][str(series["id"])] = series
    _save_state()
    return series


@app.put("/api/{ver}/series")
async def series_update(ver: str, request: Request):
    body = await request.json()
    series_id = body.get("id")
    existing = state["series"].get(str(series_id)) if series_id is not None else None
    if not existing:
        tvdb_id = body.get("tvdbId") or body.get("tvMazeId")
        matches = [s for s in state["series"].values() if s.get("tvdbId") == tvdb_id]
        existing = matches[0] if matches else None
    if not existing:
        return JSONResponse(status_code=404, content={"message": "Series not found"})
    _apply_series_body(existing, body)
    _save_state()
    return existing


@app.delete("/api/{ver}/series/{series_id}")
async def series_delete(ver: str, series_id: int):
    state["series"].pop(str(series_id), None)
    state["episodes"] = {k: v for k, v in state["episodes"].items() if k[0] != series_id}
    _save_state()


@app.get("/api/{ver}/episode")
async def episode_list(ver: str, seriesId: int | None = None):
    eps = list(state["episodes"].values())
    if seriesId is not None:
        eps = [e for e in eps if e["seriesId"] == seriesId]
    return eps


@app.get("/api/{ver}/episode/{episode_id}")
async def episode_get(ver: str, episode_id: int):
    for e in state["episodes"].values():
        if e["id"] == episode_id:
            return e
    return JSONResponse(status_code=404, content={"message": "Episode not found"})


@app.put("/api/{ver}/episode/monitor")
async def episode_monitor(ver: str, request: Request):
    body = await request.json()
    ids = {int(value) for value in body.get("episodeIds", [])}
    monitored = bool(body.get("monitored", True))
    for episode in state["episodes"].values():
        if episode["id"] in ids:
            episode["monitored"] = monitored
    _save_state()


# ---------------------------------------------------------------------------
# Commands (shared)
# ---------------------------------------------------------------------------


@app.post("/api/{ver}/command")
async def command_create(ver: str, request: Request):
    body = await request.json()
    cmd_id = _next_id()
    cmd = {
        "id": cmd_id,
        "name": body.get("name", "Unknown"),
        "commandName": body.get("name", "Unknown"),
        "body": body,
        "status": "started",
        "queued": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ended": None,
        "result": None,
    }
    state["commands"][str(cmd_id)] = cmd
    # Mark the command completed immediately for simplicity.
    cmd["status"] = "completed"
    cmd["ended"] = cmd["started"]
    cmd["result"] = "successful"
    _save_state()
    return cmd


@app.get("/api/{ver}/command/{command_id}")
async def command_get(ver: str, command_id: int):
    cmd = state["commands"].get(str(command_id))
    if not cmd:
        return JSONResponse(status_code=404, content={"message": "Command not found"})
    return cmd


@app.get("/api/{ver}/queue")
async def queue(ver: str):
    return {"page": 1, "pageSize": 10, "sortKey": "timeleft", "sortDirection": "ascending", "totalRecords": 0, "records": []}


if __name__ == "__main__":
    import uvicorn

    port = 7878 if ROLE == "radarr" else 8989
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
