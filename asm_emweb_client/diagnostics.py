from __future__ import annotations

import http.client
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("asm_emweb_client")

_REQUEST_LABEL_PARAMS = ("limit", "offset", "notify")


class RequestMonitor:
    """Track request phase timing for one HTTP request.

    ``trace``/``atrace`` are fed by the httpx2 ``trace`` extension; backends
    without tracing leave ``current_phase`` at its default. The heartbeat loop
    reads ``current_phase`` and ``elapsed``, so those remain plain attributes
    (safe to read across threads under the GIL).
    """

    _PHASE_ORDER = (
        "connecting",
        "TLS handshake",
        "sending request",
        "waiting for server response",
        "downloading response",
    )

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._stack: List[Tuple[str, float]] = []
        self._elapsed: Dict[str, float] = {}
        self.current_phase: str = "request in progress"

    def trace(self, event: Optional[str], info: Optional[Dict[str, Any]] = None) -> None:
        if not event:
            return
        label = self._phase_label(event)
        if label is None:
            return
        if event.endswith(".started"):
            self._stack.append((label, time.monotonic()))
            self.current_phase = label
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == label:
                _, started = self._stack.pop(i)
                self._elapsed[label] = self._elapsed.get(label, 0.0) + (time.monotonic() - started)
                break
        self.current_phase = self._stack[-1][0] if self._stack else label

    async def atrace(self, event: Optional[str], info: Optional[Dict[str, Any]] = None) -> None:
        self.trace(event, info)

    def finish(self) -> None:
        """Charge any still-open phases so the final breakdown is complete."""
        now = time.monotonic()
        while self._stack:
            label, started = self._stack.pop()
            self._elapsed[label] = self._elapsed.get(label, 0.0) + (now - started)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def phases(self) -> List[Tuple[str, float]]:
        return [
            (label, round(self._elapsed[label], 3))
            for label in self._PHASE_ORDER
            if label in self._elapsed
        ]

    @staticmethod
    def _phase_label(event: str) -> Optional[str]:
        if "connect_tcp" in event:
            return "connecting"
        if "start_tls" in event:
            return "TLS handshake"
        if "send_request_headers" in event or "send_request_body" in event:
            return "sending request"
        if "receive_response_headers" in event:
            return "waiting for server response"
        if "receive_response_body" in event:
            return "downloading response"
        return None


def _fmt_seconds(seconds: float) -> str:
    seconds = float(seconds)
    if seconds >= 1 and seconds.is_integer():
        return f"{seconds:.0f}s"
    return f"{seconds:.1f}s"


def _fmt_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def request_label(method: str, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
    label = f"{method} {path}"
    parts = [f"{key}={params[key]}" for key in _REQUEST_LABEL_PARAMS if params and key in params]
    if parts:
        label += " " + " ".join(parts)
    return label


def start_line(method: str, path: str, params: Optional[Mapping[str, Any]], timeout: float) -> str:
    return (
        f"{request_label(method, path, params)} - waiting for response "
        f"(timeout {_fmt_seconds(timeout)} per phase)"
    )


def end_line(
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]],
    monitor: RequestMonitor,
    status: int,
    size: Optional[int],
) -> str:
    detail = [f"{phase} {_fmt_seconds(seconds)}" for phase, seconds in monitor.phases()]
    if size is not None:
        detail.append(_fmt_size(size))
    where = f" ({', '.join(detail)})" if detail else ""
    reason = http.client.responses.get(status, "")
    suffix = f" {reason}" if reason else ""
    return (
        f"{request_label(method, path, params)} -> {status}{suffix} "
        f"in {_fmt_seconds(monitor.elapsed)}{where}"
    )


def failure_line(
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]],
    monitor: RequestMonitor,
    error_kind: str,
) -> str:
    return (
        f"{request_label(method, path, params)} failed after {_fmt_seconds(monitor.elapsed)} "
        f"while {monitor.current_phase}: {error_kind}"
    )


def heartbeat_line(monitor: RequestMonitor, timeout: float) -> str:
    phase = monitor.current_phase.removeprefix("waiting for ")
    return (
        f"still waiting for {phase} ({_fmt_seconds(monitor.elapsed)} elapsed, "
        f"timeout {_fmt_seconds(timeout)} per phase)"
    )
