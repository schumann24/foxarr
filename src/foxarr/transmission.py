"""Safe Transmission RPC adapter and lifecycle worker.

The adapter performs no calls until a method is explicitly invoked. The worker
submits new torrents paused and refuses start/stop operations without an
explicit confirmation flag. Download URLs are accepted only in memory and are
never included in returned snapshots or error messages.
"""

from __future__ import annotations

from typing import Any

import httpx


class TransmissionError(RuntimeError):
    """Raised when Transmission rejects or cannot process an RPC request."""


class TransmissionConfirmationRequired(TransmissionError):
    """Raised when a potentially state-changing worker action lacks confirmation."""


TORRENT_FIELDS = [
    "id",
    "name",
    "status",
    "percentDone",
    "downloadDir",
    "totalSize",
    "labels",
    "error",
    "errorString",
    "rateDownload",
]

# Transmission's numeric status values.  The lifecycle names are deliberately
# Foxarr-specific and do not imply that media-mirror/Jellyfin has imported a file.
_TRANSMISSION_STATUS_NAMES = {
    0: "paused",
    1: "queued",
    2: "checking",
    3: "queued",
    4: "downloading",
    5: "queued",
    6: "seeding",
}


def build_torrent_add_arguments(
    download_url: str,
    download_dir: str,
    labels: list[str] | None = None,
    paused: bool = True,
) -> dict[str, Any]:
    """Build a torrent-add payload without logging or persisting the URL."""
    if not download_url.startswith(("magnet:", "http://", "https://")):
        raise ValueError("download_url must be a magnet or HTTP(S) URL")
    if not download_dir.startswith("/"):
        raise ValueError("download_dir must be an absolute path")
    if labels is not None and not all(isinstance(label, str) and label for label in labels):
        raise ValueError("labels must contain non-empty strings")
    return {
        "filename": download_url,
        "download-dir": download_dir,
        "labels": labels or [],
        "paused": paused,
    }


def _torrent_status_name(torrent: dict[str, Any]) -> str:
    raw_status = torrent.get("status")
    if isinstance(raw_status, int) and raw_status in _TRANSMISSION_STATUS_NAMES:
        return _TRANSMISSION_STATUS_NAMES[raw_status]
    if isinstance(raw_status, str):
        return raw_status.lower()
    return "unknown"


def _lifecycle_status(torrent: dict[str, Any]) -> str:
    if torrent.get("error") not in (None, 0, "0", ""):
        return "error"
    try:
        percent_done = float(torrent.get("percentDone", 0))
    except (TypeError, ValueError):
        percent_done = 0
    if percent_done >= 1:
        return "transmission_completed"
    status = _torrent_status_name(torrent)
    if status == "paused":
        return "paused"
    if status in {"downloading", "checking"}:
        return "downloading"
    if status in {"queued", "seeding"}:
        return "queued" if status == "queued" else "transmission_completed"
    return "error" if status == "unknown" else status


def safe_torrent_snapshot(torrent: dict[str, Any]) -> dict[str, Any]:
    """Return metadata safe for Foxarr responses/storage; never include URLs."""
    labels = torrent.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    return {
        "torrentId": torrent.get("id"),
        "name": torrent.get("name"),
        "status": _torrent_status_name(torrent),
        "lifecycleStatus": _lifecycle_status(torrent),
        "percentDone": torrent.get("percentDone", 0),
        "downloadDir": torrent.get("downloadDir"),
        "totalSize": torrent.get("totalSize"),
        "labels": [label for label in labels if isinstance(label, str)],
        "error": torrent.get("errorString") or None,
        "rateDownload": torrent.get("rateDownload", 0),
    }


class TransmissionClient:
    """Minimal JSON-RPC client with the standard session-id handshake."""

    def __init__(
        self,
        url: str,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.url = url
        self.timeout = timeout
        self._transport = transport
        self._session_id = ""
        self._auth = (
            httpx.BasicAuth(username, password)
            if username is not None and password is not None
            else None
        )

    def _rpc(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        request = {
            "method": method,
            "arguments": arguments or {},
            "tag": "foxarr",
        }
        headers = {"X-Transmission-Session-Id": self._session_id}
        with httpx.Client(
            timeout=self.timeout,
            transport=self._transport,
            auth=self._auth,
        ) as client:
            response = client.post(self.url, json=request, headers=headers)
            if response.status_code == 409:
                session_id = response.headers.get("X-Transmission-Session-Id")
                if not session_id:
                    raise TransmissionError("Transmission returned 409 without a session id")
                self._session_id = session_id
                response = client.post(
                    self.url,
                    json=request,
                    headers={"X-Transmission-Session-Id": self._session_id},
                )
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise TransmissionError(f"Transmission {method} RPC failed: {error}") from error
        if not isinstance(payload, dict):
            raise TransmissionError(f"Transmission {method} returned an invalid response")
        result = payload.get("result")
        if result != "success":
            raise TransmissionError(f"Transmission {method} returned result: {result}")
        return payload

    def add_torrent(
        self,
        download_url: str,
        download_dir: str,
        labels: list[str] | None = None,
        paused: bool = True,
    ) -> dict[str, Any]:
        """Submit one torrent-add RPC and return Transmission's result."""
        arguments = build_torrent_add_arguments(download_url, download_dir, labels, paused)
        return self._rpc("torrent-add", arguments)

    def get_torrents(
        self,
        torrent_ids: list[int] | None = None,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read torrent metadata without exposing executable URLs."""
        if torrent_ids is not None and not all(
            isinstance(torrent_id, int) and not isinstance(torrent_id, bool) and torrent_id > 0
            for torrent_id in torrent_ids
        ):
            raise ValueError("torrent_ids must contain positive integers")
        arguments: dict[str, Any] = {"fields": fields or TORRENT_FIELDS}
        if torrent_ids:
            arguments["ids"] = torrent_ids
        payload = self._rpc("torrent-get", arguments)
        response_arguments = payload.get("arguments", {})
        torrents = response_arguments.get("torrents", []) if isinstance(response_arguments, dict) else []
        if not isinstance(torrents, list):
            raise TransmissionError("Transmission torrent-get returned invalid torrents")
        return [torrent for torrent in torrents if isinstance(torrent, dict)]

    def get_torrent(self, torrent_id: int) -> dict[str, Any] | None:
        torrents = self.get_torrents([torrent_id])
        return torrents[0] if torrents else None

    def find_by_label(self, label: str) -> list[dict[str, Any]]:
        if not isinstance(label, str) or not label:
            raise ValueError("label must be a non-empty string")
        return [torrent for torrent in self.get_torrents() if label in torrent.get("labels", [])]

    def start_torrent(self, torrent_id: int) -> dict[str, Any]:
        self._validate_torrent_id(torrent_id)
        return self._rpc("torrent-start", {"ids": [torrent_id]})

    def stop_torrent(self, torrent_id: int) -> dict[str, Any]:
        self._validate_torrent_id(torrent_id)
        return self._rpc("torrent-stop", {"ids": [torrent_id]})

    @staticmethod
    def _validate_torrent_id(torrent_id: int) -> None:
        if not isinstance(torrent_id, int) or isinstance(torrent_id, bool) or torrent_id < 1:
            raise ValueError("torrent_id must be a positive integer")


class TransmissionWorker:
    """Idempotent Foxarr lifecycle operations over a Transmission client.

    New submissions are always paused. A job label is the idempotency key. The
    worker never starts a torrent implicitly; ``start`` and ``stop`` require
    ``confirm=True`` and return only a safe torrent snapshot.
    """

    def __init__(self, client: TransmissionClient):
        self.client = client

    @staticmethod
    def job_label(job_id: int) -> str:
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1:
            raise ValueError("job_id must be a positive integer")
        return f"foxarr-job-{job_id}"

    def submit_paused(
        self,
        job_id: int,
        download_url: str,
        download_dir: str,
        media_type: str = "movie",
    ) -> dict[str, Any]:
        """Submit a new torrent paused, or return the existing job torrent."""
        if media_type not in {"movie", "series"}:
            raise ValueError("media_type must be movie or series")
        label = self.job_label(job_id)
        labels = ["foxarr", label]
        existing = self.client.find_by_label(label)
        if existing:
            return {
                "created": False,
                "mediaType": media_type,
                "labels": labels,
                "torrent": safe_torrent_snapshot(existing[0]),
            }

        try:
            payload = self.client.add_torrent(download_url, download_dir, labels, paused=True)
        except TransmissionError:
            # A concurrent worker may have added the same job between the
            # lookup and add. Re-read by label before surfacing the error.
            existing = self.client.find_by_label(label)
            if existing:
                return {
                    "created": False,
                    "mediaType": media_type,
                    "labels": labels,
                    "torrent": safe_torrent_snapshot(existing[0]),
                }
            raise

        torrent = self._torrent_from_add_response(payload)
        if torrent is None:
            existing = self.client.find_by_label(label)
            if not existing:
                raise TransmissionError("Transmission added torrent but returned no torrent id")
            torrent = existing[0]
        else:
            # Transmission's torrent-added/torrent-duplicate object is often
            # only id/name/hashString. Fetch the authoritative safe snapshot
            # before persisting a lifecycle state.
            refreshed = self.client.get_torrent(int(torrent["id"]))
            if refreshed is not None:
                torrent = refreshed
        return {
            "created": True,
            "mediaType": media_type,
            "labels": labels,
            "torrent": safe_torrent_snapshot(torrent),
        }

    def snapshot(self, torrent_id: int) -> dict[str, Any]:
        torrent = self.client.get_torrent(torrent_id)
        if torrent is None:
            raise TransmissionError("Transmission torrent was not found")
        return safe_torrent_snapshot(torrent)

    def start(self, torrent_id: int, confirm: bool = False) -> dict[str, Any]:
        self._require_confirmation(confirm, "start")
        self.client.start_torrent(torrent_id)
        return self.snapshot(torrent_id)

    def stop(self, torrent_id: int, confirm: bool = False) -> dict[str, Any]:
        self._require_confirmation(confirm, "stop")
        self.client.stop_torrent(torrent_id)
        return self.snapshot(torrent_id)

    @staticmethod
    def _require_confirmation(confirm: bool, action: str) -> None:
        if confirm is not True:
            raise TransmissionConfirmationRequired(
                f"Transmission {action} requires explicit confirmation"
            )

    @staticmethod
    def _torrent_from_add_response(payload: dict[str, Any]) -> dict[str, Any] | None:
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            return None
        for key in ("torrent-added", "torrent-duplicate"):
            torrent = arguments.get(key)
            if isinstance(torrent, dict) and isinstance(torrent.get("id"), int):
                return torrent
        return None


def build_download_plan(
    job_id: int,
    selected_index: int,
    selected_result: dict[str, Any],
    download_dir: str,
    download_client: str = "transmission",
) -> dict[str, Any]:
    """Create a side-effect-free preview for a selected release."""
    if download_client != "transmission":
        raise ValueError("only transmission is supported")
    if not download_dir.startswith("/"):
        raise ValueError("downloadDir must be an absolute path")
    labels = ["foxarr", f"foxarr-job-{job_id}"]
    return {
        "downloadClient": download_client,
        "downloadDir": download_dir,
        "labels": labels,
        "paused": True,
        "source": {
            "jobId": job_id,
            "selectedIndex": selected_index,
            "guid": selected_result.get("guid"),
        },
        "rpcPreview": {
            "method": "torrent-add",
            "arguments": {
                "filename": "<resolved-from-prowlarr-at-submit-time>",
                "download-dir": download_dir,
                "labels": labels,
                "paused": True,
            },
        },
        "execution": "not_submitted",
    }


def build_resolved_submit_preview(
    plan: dict[str, Any],
    download_url: str,
) -> dict[str, Any]:
    """Build a submit preview while deliberately redacting the resolved URL."""
    arguments = dict(plan["rpcPreview"]["arguments"])
    arguments["filename"] = "<resolved-ephemeral-download-url>"
    return {
        **plan,
        "resolved": True,
        "urlKind": "magnet" if download_url.startswith("magnet:") else "http",
        "rpcPreview": {"method": "torrent-add", "arguments": arguments},
        "execution": "not_submitted",
    }
