# Foxarr

> A lightweight Radarr-compatible media request adapter for Seerr.

Foxarr is an open-source bridge for self-hosted media servers. It is designed to let [Seerr](https://seerr.dev/) submit movie requests through a small Radarr-compatible API, then search configured indexers through [Prowlarr](https://prowlarr.com/) and send selected torrents to a download client such as [Transmission](https://transmissionbt.com/).

```text
Seerr → Foxarr → Prowlarr → download client → media library
```

## Status

🚧 **Early development.** The repository now contains a movie-only Radarr-compatible
MVP. It accepts Seerr movie requests and persists them in SQLite, but it does
not search indexers or download files yet.

Foxarr is **not** a Radarr fork. It will implement only the Radarr API surface required by Seerr and delegate search and download work to configurable providers.

## Current movie MVP

The service implements the minimum movie surface captured from Seerr 3.4.1:

```text
GET  /api/v3/system/status
GET  /api/v3/qualityProfile
GET  /api/v3/rootfolder
GET  /api/v3/tag
GET  /api/v3/movie/lookup
POST /api/v3/movie
GET  /api/v3/movie
GET  /api/v3/movie/{id}
```

Movie creation is idempotent by `tmdbId`. The MVP always reports
`hasFile: false`; `addOptions.searchForMovie` is recorded but does not trigger
external work.

## Planned integrations

- Radarr-compatible API for Seerr movie requests
- SQLite-backed request and job state
- Prowlarr JSON API integration
- Release selection by quality, size, seeders, and language
- Transmission RPC integration
- Dry-run mode that searches and logs without downloading
- Idempotent request handling and structured status logging
- Docker Compose deployment and environment-based configuration

## Design goals

- Keep the service small and easy to self-host
- Never log API keys, cookies, or signed download URLs
- Make provider integrations replaceable
- Prefer explicit configuration over hidden magic
- Be useful with a single indexer as well as a larger *arr-style setup

## Development

The implementation is intentionally being built in small, testable steps. Contributions, issue reports, and design feedback are welcome.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

Run locally:

```bash
uvicorn foxarr.app:app --reload --port 7878
```

Or with Docker:

```bash
docker compose up --build
```

The SQLite database is stored in `/data/foxarr.db` in the container. Published
ports default to loopback only.

## License

MIT. See [LICENSE](LICENSE).
