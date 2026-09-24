from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .diagnostics import _fmt_seconds, logger, request_label
from .models import ApiError, NotifyResult, RegistrationsPage
from .transport import Transport, TransportError, TransportResponse, default_transport

MAX_LIMIT = 1000
DEFAULT_NOTIFY = "reboot"
VALID_NOTIFIES = ("reboot", "reloadconfig", "reloadcontacts", "failback", "forceunregister")


def validate_notify(notify: str) -> str:
    if notify not in VALID_NOTIFIES:
        raise ValueError(
            f"Unknown notify type {notify!r}; valid values are {', '.join(VALID_NOTIFIES)}"
        )
    return notify


def parse_filters(filters: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Return filters ready to pass as query parameters (values URL-encoded by the transport)."""
    if not filters:
        return {}
    return {str(k): str(v) for k, v in filters.items()}


class _BaseClient:
    """Shared request/parsing logic for the sync and async clients."""

    def __init__(
        self,
        transport: Transport,
        max_retries: int = 3,
        retry_max_total: Optional[float] = 240.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.transport = transport
        self.max_retries = max_retries
        self.retry_max_total = retry_max_total

    def _is_retryable_503(self, response: TransportResponse) -> bool:
        return response.status == 503 and (response.retry_after is not None or self._cache_unloaded(response))

    def _cache_unloaded(self, response: TransportResponse) -> bool:
        try:
            parsed = json.loads(response.body.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(parsed, dict) and (parsed.get("additionalStatus") or "").upper() == "CACHEUNLOADED"

    def _retry_wait(self, response: TransportResponse) -> float:
        """Seconds to wait before retrying a cache-unloaded 503 (Retry-After, min 10s)."""
        retry_after = response.retry_after
        return float(max(retry_after if retry_after is not None else 10, 10))

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    async def _asleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _log_give_up(self, label: str, attempt: int, reason: str) -> None:
        suffix = "attempt" if attempt == 1 else "attempts"
        logger.warning("giving up on %s after %d %s (%s)", label, attempt, suffix, reason)

    def _log_retry(
        self,
        label: str,
        response: TransportResponse,
        wait: float,
        attempt: int,
        deadline: Optional[float],
    ) -> None:
        details = f"attempt {attempt + 1} of {self.max_retries + 1}"
        if deadline is not None:
            remaining = max(deadline - time.monotonic() - wait, 0.0)
            details += f", {_fmt_seconds(remaining)} of retry budget left"
        status_text = "503 CACHEUNLOADED" if self._cache_unloaded(response) else "503"
        logger.info("%s on %s; retrying in %s (%s)", status_text, label, _fmt_seconds(wait), details)

    def _do_request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        """Issue a request, retrying cache-unloaded 503s (bounded retries + wall-clock cap).

        Raises ApiError for non-retryable HTTP errors or once the retry budget
        (``max_retries`` / ``retry_max_total``) is exhausted.
        """
        started = time.monotonic()
        deadline = None
        if self.retry_max_total is not None:
            deadline = started + self.retry_max_total

        attempt = 0
        while True:
            attempt += 1
            response = self.transport.request_sync(method, path, params, accept)
            if self._is_retryable_503(response):
                wait = self._retry_wait(response)
                label = request_label(method, path, params)
                if deadline is not None and (time.monotonic() + wait) > deadline:
                    self._log_give_up(
                        label, attempt, f"retry budget of {_fmt_seconds(deadline - started)} exhausted"
                    )
                    self.transport.raise_for_error(response)
                    raise ApiError.from_body(response.status, response.body)

                if attempt > self.max_retries:
                    self._log_give_up(label, attempt, f"max retries ({self.max_retries}) reached")
                    self.transport.raise_for_error(response)
                    raise ApiError.from_body(response.status, response.body)

                self._log_retry(label, response, wait, attempt, deadline)
                self._sleep(wait)
                continue
            self.transport.raise_for_error(response)
            return response

    async def _ado_request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> TransportResponse:
        started = time.monotonic()
        deadline = None
        if self.retry_max_total is not None:
            deadline = started + self.retry_max_total

        attempt = 0
        while True:
            attempt += 1
            response = await self.transport.request_async(method, path, params)
            if self._is_retryable_503(response):
                wait = self._retry_wait(response)
                label = request_label(method, path, params)
                if deadline is not None and (time.monotonic() + wait) > deadline:
                    self._log_give_up(
                        label, attempt, f"retry budget of {_fmt_seconds(deadline - started)} exhausted"
                    )
                    self.transport.raise_for_error(response)
                    raise ApiError.from_body(response.status, response.body)

                if attempt > self.max_retries:
                    self._log_give_up(label, attempt, f"max retries ({self.max_retries}) reached")
                    self.transport.raise_for_error(response)
                    raise ApiError.from_body(response.status, response.body)

                self._log_retry(label, response, wait, attempt, deadline)
                await self._asleep(wait)
                continue
            self.transport.raise_for_error(response)
            return response

    def fetch_page(
        self,
        filters: Optional[Mapping[str, str]] = None,
        limit: int = MAX_LIMIT,
        offset: int = 0,
    ) -> RegistrationsPage:
        params: Dict[str, Any] = parse_filters(filters)
        params["limit"] = limit
        params["offset"] = offset
        params["format"] = "normal"
        response = self._do_request("GET", "registrations", params)
        return RegistrationsPage.from_dict(json.loads(response.body.decode("utf-8") or "{}"))

    def fetch_raw_page(
        self,
        filters: Optional[Mapping[str, str]] = None,
        limit: int = MAX_LIMIT,
        offset: int = 0,
        accept: str = "application/json",
    ) -> TransportResponse:
        """Fetch a registration page without deserializing its successful body."""
        params: Dict[str, Any] = parse_filters(filters)
        params["limit"] = limit
        params["offset"] = offset
        params["format"] = "normal"
        return self._do_request("GET", "registrations", params, accept)

    def count_registrations(self, filters: Optional[Mapping[str, str]] = None) -> int:
        """Return the total number of registrations matching filters (limit=0 count)."""
        page = self.fetch_page(filters, limit=0, offset=0)
        return page.totalcount

    async def acount_registrations(self, filters: Optional[Mapping[str, str]] = None) -> int:
        page = await self.afetch_page(filters, limit=0, offset=0)
        return page.totalcount

    async def afetch_page(
        self,
        filters: Optional[Mapping[str, str]] = None,
        limit: int = MAX_LIMIT,
        offset: int = 0,
    ) -> RegistrationsPage:
        params: Dict[str, Any] = parse_filters(filters)
        params["limit"] = limit
        params["offset"] = offset
        params["format"] = "normal"
        response = await self._ado_request("GET", "registrations", params)
        return RegistrationsPage.from_dict(json.loads(response.body.decode("utf-8") or "{}"))

    def list_registrations(
        self,
        filters: Optional[Mapping[str, str]] = None,
        limit: int = MAX_LIMIT,
    ) -> List[RegistrationsPage]:
        if not (1 <= limit <= MAX_LIMIT):
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        pages: List[RegistrationsPage] = []
        offset = 0
        while True:
            page = self.fetch_page(filters, limit, offset)
            pages.append(page)
            offset += page.count
            if page.count == 0 or offset >= page.totalcount:
                break
        return pages

    async def alist_registrations(
        self,
        filters: Optional[Mapping[str, str]] = None,
        limit: int = MAX_LIMIT,
    ) -> List[RegistrationsPage]:
        if not (1 <= limit <= MAX_LIMIT):
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        pages: List[RegistrationsPage] = []
        offset = 0
        while True:
            page = await self.afetch_page(filters, limit, offset)
            pages.append(page)
            offset += page.count
            if page.count == 0 or offset >= page.totalcount:
                break
        return pages

    def iter_registrations(
        self,
        filters: Optional[Mapping[str, str]] = None,
        limit: int = MAX_LIMIT,
        offset: int = 0,
    ) -> Iterator[Any]:
        """Stream registrations page by page (generator)."""
        if not (1 <= limit <= MAX_LIMIT):
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        current = offset
        while True:
            page = self.fetch_page(filters, limit, current)
            for registration in page.registrations:
                yield registration
            current += page.count
            if page.count == 0 or current >= page.totalcount:
                break

    def send_notify(
        self,
        notify: str = DEFAULT_NOTIFY,
        filters: Optional[Mapping[str, str]] = None,
    ) -> NotifyResult:
        notify = validate_notify(notify)
        params: Dict[str, Any] = parse_filters(filters)
        params["notify"] = notify
        response = self._do_request("POST", "registrations", params)
        return NotifyResult.from_dict(json.loads(response.body.decode("utf-8") or "{}"))

    def send_raw_notify(
        self,
        notify: str = DEFAULT_NOTIFY,
        filters: Optional[Mapping[str, str]] = None,
        accept: str = "application/json",
    ) -> TransportResponse:
        """Send a notification without deserializing its successful response body."""
        notify = validate_notify(notify)
        params: Dict[str, Any] = parse_filters(filters)
        params["notify"] = notify
        return self._do_request("POST", "registrations", params, accept)

    async def asend_notify(
        self,
        notify: str = DEFAULT_NOTIFY,
        filters: Optional[Mapping[str, str]] = None,
    ) -> NotifyResult:
        notify = validate_notify(notify)
        params: Dict[str, Any] = parse_filters(filters)
        params["notify"] = notify
        response = await self._ado_request("POST", "registrations", params)
        return NotifyResult.from_dict(json.loads(response.body.decode("utf-8") or "{}"))


class AsmEmWebClient(_BaseClient):
    """Synchronous client for the SM EM Web Service."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        verify: Any = True,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_max_total: Optional[float] = 240.0,
        heartbeat_interval: float = 10.0,
        transport: Optional[Transport] = None,
    ) -> None:
        if transport is None:
            transport = default_transport(
                host, user, password, verify=verify, timeout=timeout, heartbeat_interval=heartbeat_interval
            )
        super().__init__(transport, max_retries=max_retries, retry_max_total=retry_max_total)


class AsyncAsmEmWebClient(_BaseClient):
    """Asynchronous client for the SM EM Web Service."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        verify: Any = True,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_max_total: Optional[float] = 240.0,
        heartbeat_interval: float = 10.0,
        transport: Optional[Transport] = None,
    ) -> None:
        if transport is None:
            transport = default_transport(
                host, user, password, verify=verify, timeout=timeout, heartbeat_interval=heartbeat_interval
            )
        super().__init__(transport, max_retries=max_retries, retry_max_total=retry_max_total)
