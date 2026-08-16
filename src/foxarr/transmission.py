"""Small Transmission RPC adapter; no calls are made unless explicitly invoked."""

from __future__ import annotations

from typing import Any

import httpx


class TransmissionError(RuntimeError):
    """Raised when Transmission rejects or cannot process an RPC request."""


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


class TransmissionClient:
    """Minimal JSON-RPC client with the standard session-id handshake."""

    def __init__(self, url: str, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self.url = url
        self.timeout = timeout
        self._transport = transport
        self._session_id = ""

    def add_torrent(
        self,
        download_url: str,
        download_dir: str,
        labels: list[str] | None = None,
        paused: bool = True,
    ) -> dict[str, Any]:
        """Submit one torrent-add RPC and return Transmission's result."""
        arguments = build_torrent_add_arguments(download_url, download_dir, labels, paused)
        request = {"method": "torrent-add", "arguments": arguments, "tag": "foxarr"}
        headers = {"X-Transmission-Session-Id": self._session_id}
        with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
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
                raise TransmissionError(f"Transmission RPC failed: {error}") from error
        if payload.get("result") != "success":
            raise TransmissionError(f"Transmission returned result: {payload.get('result')}")
        return payload


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
