# foxarr-mock (*arr API mock)

Minimal Radarr/Sonarr v3-compatible mock used to observe what Seerr actually
calls. Logs every request as JSONL to stdout (method, path, query, body,
status, response body, elapsed ms). State is kept in memory and optionally
persisted to a JSON file.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# terminal 1
ROLE=radarr uvicorn server:app --port 7878
# terminal 2
ROLE=sonarr uvicorn server:app --port 8989
```

## Run with Docker

```bash
docker compose up --build
# mock-radarr on 127.0.0.1:7878, mock-sonarr on 127.0.0.1:8989
# state files land in ./data/
```

## Smoke test

```bash
curl -s http://127.0.0.1:7878/api/v3/system/status
curl -s http://127.0.0.1:7878/api/v3/rootfolder
curl -s "http://127.0.0.1:7878/api/v3/movie/lookup?term=tmdb:12345"
curl -s -X POST http://127.0.0.1:7878/api/v3/movie \
  -H 'Content-Type: application/json' \
  -d '{"tmdbId":12345,"qualityProfileId":1,"rootFolderPath":"/movies","monitored":true}'
```

## What it emulates

- `GET /api/v3/system/status`, `/health`, `/diskspace`, `/rootfolder`, `/qualityprofile`, `/languageprofile`
- Radarr: `GET /movie/lookup`, `GET /movie`, `GET /movie/{id}`, `POST /movie`, `DELETE /movie/{id}`
- Sonarr: `GET /series/lookup`, `GET /series`, `GET /series/{id}`, `POST /series`, `DELETE /series/{id}`, `GET /episode`, `GET /episode/{id}`
- Shared: `POST /command`, `GET /command/{id}`, `GET /queue`

## Env knobs

| Variable | Meaning |
| --- | --- |
| `ROLE` | `radarr` or `sonarr` — switches the fake payloads |
| `FOXARR_MOCK_STATE` | optional JSON file for state persistence |
| `FOXARR_MOCK_LOG_HEADERS` | set `1` to log non-secret headers too |
