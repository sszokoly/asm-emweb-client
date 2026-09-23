from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


def _s(data: Mapping[str, Any], key: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    return str(value)


def _b(data: Mapping[str, Any], key: str) -> Optional[bool]:
    value = data.get(key)
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    return None


def _i(data: Mapping[str, Any], key: str) -> Optional[int]:
    value = data.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Registration:
    """A single SIP user registration record from the /registrations resource.

    All fields are optional: the API omits null values, and placeholder records
    (no active registration) carry no ipAddress or registration key.
    """

    id: Optional[int] = None
    login: Optional[str] = None
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    ipAddress: Optional[str] = None
    deviceType: Optional[str] = None
    deviceVendor: Optional[str] = None
    deviceModel: Optional[str] = None
    deviceVersion: Optional[str] = None
    deviceMac: Optional[str] = None
    deviceSerial: Optional[str] = None
    ast: Optional[bool] = None
    controller: Optional[str] = None
    primName: Optional[str] = None
    primReg: Optional[bool] = None
    secReg: Optional[bool] = None
    survReg: Optional[bool] = None
    remoteOffice: Optional[bool] = None
    location: Optional[str] = None
    actualLocation: Optional[str] = None
    sharedControl: Optional[bool] = None
    simultaneousDevices: Optional[str] = None
    handle: Optional[str] = None
    link_href: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Registration":
        link = data.get("link") or {}
        return cls(
            id=_i(data, "id"),
            login=_s(data, "login"),
            firstName=_s(data, "firstName"),
            lastName=_s(data, "lastName"),
            ipAddress=_s(data, "ipAddress"),
            deviceType=_s(data, "deviceType"),
            deviceVendor=_s(data, "deviceVendor"),
            deviceModel=_s(data, "deviceModel"),
            deviceVersion=_s(data, "deviceVersion"),
            deviceMac=_s(data, "deviceMac"),
            deviceSerial=_s(data, "deviceSerial"),
            ast=_b(data, "ast"),
            controller=_s(data, "controller"),
            primName=_s(data, "primName"),
            primReg=_b(data, "primReg"),
            secReg=_b(data, "secReg"),
            survReg=_b(data, "survReg"),
            remoteOffice=_b(data, "remoteOffice"),
            location=_s(data, "location"),
            actualLocation=_s(data, "actualLocation"),
            sharedControl=_b(data, "sharedControl"),
            simultaneousDevices=_s(data, "simultaneousDevices"),
            handle=_s(data, "handle"),
            link_href=_s(link, "href") if isinstance(link, Mapping) else None,
        )


@dataclass
class RegistrationsPage:
    """A page of registrations with the server's paging metadata."""

    count: int = 0
    totalcount: int = 0
    limit: int = 0
    offset: int = 0
    query: Optional[str] = None
    registrations: List[Registration] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RegistrationsPage":
        raw_list = data.get("registration")
        if raw_list is None:
            raw_list = data.get("registrations") or []
        if isinstance(raw_list, Mapping):
            raw_list = [raw_list]
        regs = [Registration.from_dict(item) for item in raw_list]
        return cls(
            count=_i(data, "count") or len(regs),
            totalcount=_i(data, "totalcount") or len(regs),
            limit=_i(data, "limit") or 0,
            offset=_i(data, "offset") or 0,
            query=_s(data, "query"),
            registrations=regs,
        )


@dataclass
class NotifyStatus:
    """Per-Session-Manager breakdown of a notification request."""

    hrefname: Optional[str] = None
    href: Optional[str] = None
    requested: int = 0
    sent: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NotifyStatus":
        asmstatus = data.get("asmstatus") or {}
        link = asmstatus.get("link") if isinstance(asmstatus, Mapping) else None
        return cls(
            hrefname=_s(link, "hrefname") if isinstance(link, Mapping) else None,
            href=_s(link, "href") if isinstance(link, Mapping) else None,
            requested=_i(data, "requested") or 0,
            sent=_i(data, "sent") or 0,
        )


@dataclass
class NotifyResult:
    """Result of an AST device notification request."""

    notify: str = "reboot"
    requested: int = 0
    sent: int = 0
    statuses: List[NotifyStatus] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NotifyResult":
        raw_list = data.get("notifystatus")
        if raw_list is None:
            raw_list = []
        if isinstance(raw_list, Mapping):
            raw_list = [raw_list]
        statuses = [NotifyStatus.from_dict(item) for item in raw_list]
        return cls(
            notify=_s(data, "notify") or "reboot",
            requested=_i(data, "requested") or 0,
            sent=_i(data, "sent") or 0,
            statuses=statuses,
        )

    @property
    def unsent(self) -> int:
        return max(self.requested - self.sent, 0)


class ApiError(Exception):
    """An HTTP error returned by the SM EM Web Service.

    Carries the standard error-body fields (status, statusMessage, message,
    additionalStatus, additionalMessage) when the body could be parsed.
    """

    def __init__(
        self,
        status: int,
        message: Optional[str] = None,
        status_message: Optional[str] = None,
        additional_status: Optional[str] = None,
        additional_message: Optional[str] = None,
        request_method: Optional[str] = None,
        request_uri: Optional[str] = None,
    ) -> None:
        self.status = status
        self.status_message = status_message
        self.message = message
        self.additional_status = additional_status
        self.additional_message = additional_message
        self.request_method = request_method
        self.request_uri = request_uri
        super().__init__(self.__str__())

    @classmethod
    def from_body(cls, status: int, body: bytes) -> "ApiError":
        parsed: Dict[str, Any] = {}
        if body:
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return cls(
            status=status,
            message=_s(parsed, "message"),
            status_message=_s(parsed, "statusMessage"),
            additional_status=_s(parsed, "additionalStatus"),
            additional_message=_s(parsed, "additionalMessage"),
            request_method=_s(parsed, "requestMethod"),
            request_uri=_s(parsed, "requestURI"),
        )

    def __str__(self) -> str:
        text = f"HTTP {self.status}"
        if self.status_message:
            # Servers often repeat the code ("401 Unauthorized"); avoid "HTTP 401 401 ...".
            status_message = self.status_message
            code = str(self.status)
            if status_message.startswith(code):
                status_message = status_message[len(code):].strip()
            if status_message:
                text += f" {status_message}"
        if self.message:
            text += f": {self.message}"
        if self.additional_status:
            text += f" [{self.additional_status}]"
        if self.additional_message:
            text += f" {self.additional_message}"
        return text

    @property
    def is_cache_unloaded(self) -> bool:
        return self.additional_status == "CACHEUNLOADED"