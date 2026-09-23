from __future__ import annotations

import asyncio
import base64
import threading
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Iterator, Mapping, Optional

from .diagnostics import (
    RequestMonitor,
    end_line,
    failure_line,
    heartbeat_line,
    logger,
    start_line,
)
from .models import ApiError


class AsyncUnavailableError(RuntimeError):
    """Raised when an async operation is requested from a sync-only backend."""


class TransportError(RuntimeError):
    """Raised for connection-level failures (TLS, DNS, timeout)."""


@dataclass
class TransportResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def retry_after(self) -> Optional[int]:
        raw = self.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None


def build_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8"))
    return f"BASIC {token.decode('ascii')}"


def get_required_headers(
    username: str,
    password: str,
    accept: str = "application/json",
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": accept,
        "Authorization": build_auth_header(username, password),
    }
    if extra:
        headers.update(extra)
    return headers


class Transport:
    """Minimal sync/async HTTP abstraction shared by the client classes.

    A transport owns the base URL, credentials, TLS settings, and timeouts.
    Backends (httpx2 primary, requests fallback) implement ``request_sync`` and,
    when able, ``request_async``, both returning :class:`TransportResponse`.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify: Any = True,
        timeout: float = 30.0,
        heartbeat_interval: float = 10.0,
    ) -> None:
        host = (host or "").strip().rstrip("/")
        if not host:
            raise ValueError("host is required")
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        self.base_url = f"{host}/ASM/ws"
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval

    @property
    def headers(self) -> Dict[str, str]:
        return get_required_headers(self.username, self.password)

    def build_url(self, path: str) -> str:
        if not path:
            return self.base_url
        return f"{self.base_url}/{path.lstrip('/')}"

    def raise_for_error(self, response: TransportResponse) -> None:
        if response.status >= 400:
            raise ApiError.from_body(response.status, response.body)

    def _log_request_start(self, method: str, path: str, params: Optional[Mapping[str, Any]]) -> None:
        logger.info("%s", start_line(method, path, params, self.timeout))

    def _log_request_end(
        self,
        monitor: RequestMonitor,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]],
        response: TransportResponse,
    ) -> None:
        logger.info("%s", end_line(method, path, params, monitor, response.status, len(response.body)))

    def _log_request_failure(
        self,
        monitor: RequestMonitor,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]],
        error: BaseException,
    ) -> None:
        logger.warning("%s", failure_line(method, path, params, monitor, type(error).__name__))

    @contextmanager
    def _heartbeat(self, monitor: RequestMonitor) -> Iterator[None]:
        """Log a periodic 'still waiting' line while a sync request is in flight."""
        if self.heartbeat_interval <= 0:
            yield
            return
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(self.heartbeat_interval):
                logger.info("%s", heartbeat_line(monitor, self.timeout))

        thread = threading.Thread(target=run, name="asm-emweb-diagnostics", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    @asynccontextmanager
    async def _aheartbeat(self, monitor: RequestMonitor) -> AsyncGenerator[None, None]:
        """Log a periodic 'still waiting' line while an async request is in flight."""
        task: Optional[asyncio.Task[None]] = None
        if self.heartbeat_interval > 0:

            async def run() -> None:
                try:
                    while True:
                        await asyncio.sleep(self.heartbeat_interval)
                        logger.info("%s", heartbeat_line(monitor, self.timeout))
                except asyncio.CancelledError:
                    raise

            task = asyncio.create_task(run())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def request_sync(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        raise NotImplementedError

    async def request_async(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        raise AsyncUnavailableError(
            "The async client requires the 'httpx2' backend, which is not installed. "
            "Install httpx2 or use the synchronous AsmEmWebClient."
        )


class Httpx2Transport(Transport):
    """Primary backend using httpx2 (sync + async)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            import httpx2
        except ImportError as exc:  # pragma: no cover - guarded by factory
            raise TransportError("httpx2 is required for the default transport") from exc
        self._httpx2 = httpx2

    def _do(self, response: Any) -> TransportResponse:
        headers = {k.lower(): v for k, v in response.headers.items()}
        return TransportResponse(status=response.status_code, headers=headers, body=response.content)

    def request_sync(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        url = self.build_url(path)
        monitor = RequestMonitor()
        self._log_request_start(method, path, params)
        try:
            with self._heartbeat(monitor):
                with self._httpx2.Client(verify=self.verify, timeout=self.timeout) as client:
                    response = client.request(
                        method,
                        url,
                        params=params,
                        headers=get_required_headers(self.username, self.password, accept),
                        extensions={"trace": monitor.trace},
                    )
            monitor.finish()
            result = self._do(response)
            self._log_request_end(monitor, method, path, params, result)
            return result
        except Exception as exc:
            monitor.finish()
            self._log_request_failure(monitor, method, path, params, exc)
            raise TransportError(f"Request failed: {exc}") from exc

    async def request_async(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        url = self.build_url(path)
        monitor = RequestMonitor()
        self._log_request_start(method, path, params)
        try:
            async with self._aheartbeat(monitor):
                async with self._httpx2.AsyncClient(verify=self.verify, timeout=self.timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        headers=get_required_headers(self.username, self.password, accept),
                        extensions={"trace": monitor.atrace},
                    )
            monitor.finish()
            result = self._do(response)
            self._log_request_end(monitor, method, path, params, result)
            return result
        except Exception as exc:
            monitor.finish()
            self._log_request_failure(monitor, method, path, params, exc)
            raise TransportError(f"Request failed: {exc}") from exc


class RequestsTransport(Transport):
    """Fallback backend (sync only) using requests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise TransportError("requests is required for the fallback transport") from exc
        self._requests = requests

    def request_sync(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        url = self.build_url(path)
        monitor = RequestMonitor()
        self._log_request_start(method, path, params)
        try:
            with self._heartbeat(monitor):
                response = self._requests.request(
                    method,
                    url,
                    params=params,
                    headers=get_required_headers(self.username, self.password, accept),
                    verify=self.verify,
                    timeout=self.timeout,
                )
            monitor.finish()
            result = TransportResponse(
                status=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.content,
            )
            self._log_request_end(monitor, method, path, params, result)
            return result
        except Exception as exc:
            monitor.finish()
            self._log_request_failure(monitor, method, path, params, exc)
            raise TransportError(f"Request failed: {exc}") from exc


def default_transport(*args: Any, **kwargs: Any) -> Transport:
    """Build the default transport: httpx2, falling back to requests if absent."""
    try:
        import httpx2  # noqa: F401
    except ImportError:
        return RequestsTransport(*args, **kwargs)
    return Httpx2Transport(*args, **kwargs)
