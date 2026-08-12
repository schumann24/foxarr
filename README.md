# Foxarr

> A lightweight Radarr-compatible media request adapter for Seerr.

Foxarr is an open-source bridge for self-hosted media servers. It is designed to let [Seerr](https://seerr.dev/) submit movie requests through a small Radarr-compatible API, then search configured indexers through [Prowlarr](https://prowlarr.com/) and send selected torrents to a download client such as [Transmission](https://transmissionbt.com/).

```text
Seerr → Foxarr → Prowlarr → download client → media library
```

## Status

🚧 **Early development.** The repository currently contains the project scaffold and design notes. The first implementation target is movie requests with Prowlarr and Transmission, including a safe dry-run mode.

Foxarr is **not** a Radarr fork. It will implement only the Radarr API surface required by Seerr and delegate search and download work to configurable providers.

## Planned MVP

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
```

## License

MIT. See [LICENSE](LICENSE).
