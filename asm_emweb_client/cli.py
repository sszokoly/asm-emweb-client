from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import click
from dotenv import dotenv_values

from .client import MAX_LIMIT, DEFAULT_NOTIFY, VALID_NOTIFIES, AsmEmWebClient
from .config import ConfigError, resolve_config
from .diagnostics import logger
from .models import ApiError
from .transport import Httpx2Transport, TransportError

_HELP_SETTINGS = {"help_option_names": ["-h", "--help"]}
VALID_FILTERS = (
    "deviceType",
    "lastName",
    "controller",
    "secName",
    "ast",
    "remoteOffice",
    "deviceVendor",
    "ipAddress",
    "primName",
    "handle",
    "deviceVersion",
    "login",
    "sharedControl",
    "firstName",
    "primReg",
    "simultaneousDevices",
    "secReg",
    "deviceMac",
    "location",
    "deviceModel",
    "deviceSerial",
    "survName",
    "survReg",
)
FILTER_NAMES_HELP = "Valid filter names: " + ", ".join(VALID_FILTERS) + "."
_CANONICAL_FILTERS = {name.lower(): name for name in VALID_FILTERS}
FILTER_MATCH_HELP = (
    "Values use starts-with prefix matching (the API documents '%' as a wildcard "
    "character, but some releases match it literally; use the bare prefix). "
)


def _parse_filter_callback(ctx: click.Context, param: click.Parameter, value: Any) -> Dict[str, str]:
    filters: Dict[str, str] = {}
    for item in value or ():
        name, sep, val = item.partition("=")
        if not sep or not name.strip():
            raise click.BadParameter(
                f"expected NAME=VALUE, got {item!r}", param=param, ctx=ctx
            )
        canonical = _CANONICAL_FILTERS.get(name.strip().lower())
        if canonical is None:
            # Unknown names may be ignored by the server, which would widen the
            # selection (e.g. reboot every endpoint), so reject them up front.
            raise click.BadParameter(
                f"unknown filter name {name.strip()!r}. {FILTER_NAMES_HELP}",
                param=param,
                ctx=ctx,
            )
        filters[canonical] = val
    return filters


def _resolve_verify(insecure: bool, ca: Optional[str]) -> Any:
    if insecure:
        return False
    if ca:
        return ca
    return True


class _ClickStderrHandler(logging.Handler):
    """Logging handler that writes through click to the real stderr stream."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            click.echo(self.format(record), err=True)
        except Exception:
            self.handleError(record)


@contextmanager
def _request_diagnostics(debug: bool) -> Iterator[None]:
    """Route request-diagnostic log lines to stderr when debug is enabled."""
    if not debug:
        yield
        return
    handler = _ClickStderrHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _shared_options(fn: Any) -> Any:
    """Connection options shared by every command branch (accepted after the branch name)."""
    fn = click.option(
        "--base-url",
        "host",
        type=str,
        default=None,
        help="System Manager base URL (hostname or https://host; /ASM/ws appended automatically). Defaults to SMGR_BASE_URL.",
    )(fn)
    fn = click.option("--user", type=str, default=None, help="System Manager administrator login. Defaults to SMGR_USER.")(fn)
    fn = click.option("--password", type=str, default=None, help="System Manager administrator password. Defaults to SMGR_PASSWORD.")(fn)
    fn = click.option("--insecure", is_flag=True, help="Skip TLS certificate verification (self-signed).")(fn)
    fn = click.option("--ca", type=click.Path(exists=True, dir_okay=False), default=None, help="CA bundle path for TLS verification.")(fn)
    fn = click.option("--timeout", type=float, default=30.0, show_default=True, help="Per-request timeout in seconds.")(fn)
    fn = click.option("--max-retries", type=int, default=3, show_default=True, help="Maximum retries per request on cache-unloaded 503 responses.")(fn)
    fn = click.option("--retry-max-total", type=float, default=240.0, show_default=True, help="Wall-clock cap (seconds) on 503-cache retries per request.")(fn)
    fn = click.option(
        "--progress/--no-progress",
        default=True,
        show_default=True,
        help="Report 'Fetched N / M registrations' per page on list stderr.",
    )(fn)
    fn = click.option(
        "--debug",
        is_flag=True,
        help="Report request diagnostics (phase timing, retries, heartbeats) on stderr.",
    )(fn)
    fn = click.option("--json", "json_output", is_flag=True, help="Write raw JSON service responses to stdout.")(fn)
    fn = click.option("--xml", "xml_output", is_flag=True, help="Write raw XML service responses to stdout.")(fn)
    return fn


def _output_media_type(json_output: bool, xml_output: bool) -> Optional[str]:
    if json_output and xml_output:
        raise click.UsageError("--json and --xml are mutually exclusive")
    if json_output:
        return "application/json"
    if xml_output:
        return "application/xml"
    return None


def _write_raw_response(body: bytes, separator: bool = False) -> None:
    stream = click.get_binary_stream("stdout")
    if separator:
        stream.write(b"\n")
    stream.write(body)


def _render_table(headers: List[str], rows: List[List[str]]) -> List[str]:
    if not rows:
        return []
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = [
        "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    return lines


def _build_client(
    host: str,
    user: str,
    password: str,
    verify: Any,
    timeout: float,
    max_retries: int,
    retry_max_total: float,
) -> AsmEmWebClient:
    return AsmEmWebClient(
        host=host,
        user=user,
        password=password,
        verify=verify,
        timeout=timeout,
        max_retries=max_retries,
        retry_max_total=retry_max_total,
    )


def _resolve_config(ctx: click.Context) -> Any:
    """Resolve credentials; convert ConfigError into click usage output."""
    try:
        return resolve_config(
            {key: ctx.params.get(key) for key in ("host", "user", "password")},
            dotenv_values=dotenv_values(),
        )
    except ConfigError as exc:
        if all(
            ctx.get_parameter_source(name) is not click.core.ParameterSource.COMMANDLINE
            for name in ctx.params
        ):
            click.echo(ctx.get_help())
            ctx.exit()
        raise click.UsageError(str(exc))


class _OrderedGroup(click.Group):
    """Click group that preserves the operator-facing command order."""

    _command_order = ("count", "list", "failback", "reboot", "reloadconfig")

    def list_commands(self, ctx: click.Context) -> List[str]:
        commands = set(super().list_commands(ctx))
        ordered = [name for name in self._command_order if name in commands]
        return ordered + sorted(commands - set(ordered))


@click.group(no_args_is_help=True, chain=False, cls=_OrderedGroup)
def main() -> None:
    """Avaya Session Manager EM Web Service API client."""


@main.command(name="list", context_settings=_HELP_SETTINGS)
@_shared_options
@click.option(
    "--filter",
    "filters",
    multiple=True,
    callback=_parse_filter_callback,
    default=(),
    help=(
        "Query filter as NAME=VALUE (repeatable). "
        + FILTER_MATCH_HELP
        + "e.g. --filter login=alice --filter ipAddress=10.1.2. "
        + FILTER_NAMES_HELP
    ),
)
@click.option("--limit", type=click.IntRange(min=0), default=None, help="Maximum number of registrations to show (default: all). Fetches pages of up to 1000, stopping once the limit is reached.")
@click.option("--offset", type=click.IntRange(min=0), default=0, show_default=True, help="Starting index of the result window (applied server-side).")
@click.pass_context
def list_command(
    ctx: click.Context,
    filters: Dict[str, str],
    limit: Optional[int],
    offset: int,
    progress: bool,
    debug: bool,
    host: Optional[str],
    user: Optional[str],
    password: Optional[str],
    insecure: bool,
    ca: Optional[str],
    timeout: float,
    max_retries: int,
    retry_max_total: float,
    json_output: bool,
    xml_output: bool,
) -> None:
    """List matching registrations (handle, IP, model, flags, location)."""
    media_type = _output_media_type(json_output, xml_output)
    config = _resolve_config(ctx)
    client = _build_client(
        config.host,
        config.user,
        config.password,
        _resolve_verify(insecure, ca),
        timeout,
        max_retries,
        retry_max_total,
    )
    try:
        with _request_diagnostics(debug):
            if media_type is not None:
                if limit == 0:
                    return
                total = client.count_registrations(filters)
                remaining = max(total - offset, 0)
                if limit is not None:
                    remaining = min(remaining, limit)
                current = offset
                separator = False
                while remaining:
                    page_limit = min(MAX_LIMIT, remaining)
                    response = client.fetch_raw_page(filters, page_limit, current, media_type)
                    _write_raw_response(response.body, separator)
                    separator = True
                    current += page_limit
                    remaining -= page_limit
                return
            rows: List[List[str]] = []
            if limit == 0:
                click.echo("Registrations: 0")
                return
            current = offset
            while True:
                page = client.fetch_page(filters, limit=MAX_LIMIT, offset=current)
                for reg in page.registrations:
                    rows.append(
                        [
                            reg.handle or "",
                            reg.ipAddress or "",
                            reg.deviceModel or "",
                            "yes" if reg.ast else "",
                            "yes" if reg.primReg else "",
                            "yes" if reg.secReg else "",
                            "yes" if reg.survReg else "",
                            reg.location or "",
                        ]
                    )
                    if limit is not None and len(rows) >= limit:
                        break
                current += page.count
                if progress:
                    click.echo(f"Fetched {current} / {page.totalcount} registrations", err=True)
                if limit is not None and len(rows) >= limit:
                    break
                if page.count == 0 or current >= page.totalcount:
                    break
    except (TransportError, ApiError) as exc:
        raise click.ClickException(f"Connection error: {exc}")
    headers = ["HANDLE", "IP", "MODEL", "AST", "1st", "2nd", "3rd", "LOCATION"]
    lines = _render_table(headers, rows)
    click.echo(f"Registrations: {len(rows)}")
    for line in lines:
        click.echo(line)


@main.command(name="count", context_settings=_HELP_SETTINGS)
@_shared_options
@click.option(
    "--filter",
    "filters",
    multiple=True,
    callback=_parse_filter_callback,
    default=(),
    help=(
        "Query filter as NAME=VALUE (repeatable). Counts only registrations matching the filters. "
        + FILTER_MATCH_HELP
        + FILTER_NAMES_HELP
    ),
)
@click.pass_context
def count_command(
    ctx: click.Context,
    filters: Dict[str, str],
    host: Optional[str],
    user: Optional[str],
    password: Optional[str],
    insecure: bool,
    ca: Optional[str],
    timeout: float,
    max_retries: int,
    retry_max_total: float,
    progress: bool,
    debug: bool,
    json_output: bool,
    xml_output: bool,
) -> None:
    """Print the number of matching registrations (single fast request)."""
    media_type = _output_media_type(json_output, xml_output)
    config = _resolve_config(ctx)
    client = _build_client(
        config.host,
        config.user,
        config.password,
        _resolve_verify(insecure, ca),
        timeout,
        max_retries,
        retry_max_total,
    )
    try:
        with _request_diagnostics(debug):
            if media_type is not None:
                _write_raw_response(client.fetch_raw_page(filters, limit=0, accept=media_type).body)
                return
            count = client.count_registrations(filters)
    except (TransportError, ApiError) as exc:
        raise click.ClickException(f"Connection error: {exc}")
    click.echo(f"{count}")


def _run_notification(
    ctx: click.Context,
    filters: Dict[str, str],
    notify: str,
    force: bool,
    host: Optional[str],
    user: Optional[str],
    password: Optional[str],
    insecure: bool,
    ca: Optional[str],
    timeout: float,
    max_retries: int,
    retry_max_total: float,
    debug: bool,
    json_output: bool,
    xml_output: bool,
) -> None:
    media_type = _output_media_type(json_output, xml_output)
    config = _resolve_config(ctx)
    client = _build_client(
        config.host,
        config.user,
        config.password,
        _resolve_verify(insecure, ca),
        timeout,
        max_retries,
        retry_max_total,
    )
    try:
        with _request_diagnostics(debug):
            count = client.count_registrations(filters)
            if count == 0:
                click.echo("No registrations match the given filters; nothing to do.", err=media_type is not None)
                return
            click.echo(f"Matching registrations: {count}", err=media_type is not None)
            if notify == "reboot":
                click.echo("Note: reboot notification only reaches AST devices (ast=true).", err=media_type is not None)
            if not force and not click.confirm(
                f"Send {notify} notification to {count} registered endpoint(s)?", abort=False, err=media_type is not None
            ):
                click.echo("Aborted - nothing sent.", err=media_type is not None)
                return
            if media_type is not None:
                _write_raw_response(client.send_raw_notify(notify, filters, media_type).body)
                return
            result = client.send_notify(notify, filters)
    except TransportError as exc:
        raise click.ClickException(f"Connection error: {exc}")
    except ApiError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Notify '{result.notify}': requested={result.requested} sent={result.sent}")
    if result.unsent:
        click.echo(f"Warning: {result.unsent} request(s) were not confirmed sent (best-effort notification).")
    for status in result.statuses:
        click.echo(f"  SM {status.hrefname or status.href or '(unknown)'}: requested={status.requested} sent={status.sent}")


def _fixed_notification_options(fn: Any) -> Any:
    fn = _shared_options(fn)
    fn = click.option(
        "--filter",
        "filters",
        multiple=True,
        callback=_parse_filter_callback,
        default=(),
        help=(
            "Query filter as NAME=VALUE (repeatable). Selects the registrations to notify. "
            + FILTER_MATCH_HELP
            + FILTER_NAMES_HELP
        ),
    )(fn)
    return click.option(
        "-f",
        "--force",
        is_flag=True,
        help="Send the notification without asking for confirmation (for scripts). The match count is still printed.",
    )(fn)


@main.command(name="reboot", context_settings=_HELP_SETTINGS)
@_shared_options
@click.option(
    "--filter",
    "filters",
    multiple=True,
    callback=_parse_filter_callback,
    default=(),
    help=(
        "Query filter as NAME=VALUE (repeatable). Selects the registrations to notify. "
        + FILTER_MATCH_HELP
        + FILTER_NAMES_HELP
    ),
)
@click.option(
    "--notify",
    type=click.Choice(list(VALID_NOTIFIES)),
    default=DEFAULT_NOTIFY,
    show_default=True,
    help="AST notification/action to request.",
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    help="Send the notification without asking for confirmation (for scripts). The match count is still printed.",
)
@click.pass_context
def reboot_command(
    ctx: click.Context,
    filters: Dict[str, str],
    notify: str,
    force: bool,
    host: Optional[str],
    user: Optional[str],
    password: Optional[str],
    insecure: bool,
    ca: Optional[str],
    timeout: float,
    max_retries: int,
    retry_max_total: float,
    progress: bool,
    debug: bool,
    json_output: bool,
    xml_output: bool,
) -> None:
    """Send an AST notification (reboot) to matching endpoints."""
    _run_notification(
        ctx, filters, notify, force, host, user, password, insecure, ca, timeout,
        max_retries, retry_max_total, debug, json_output, xml_output,
    )


@main.command(name="reloadconfig", context_settings=_HELP_SETTINGS)
@_fixed_notification_options
@click.pass_context
def reloadconfig_command(
    ctx: click.Context,
    filters: Dict[str, str],
    force: bool,
    host: Optional[str],
    user: Optional[str],
    password: Optional[str],
    insecure: bool,
    ca: Optional[str],
    timeout: float,
    max_retries: int,
    retry_max_total: float,
    progress: bool,
    debug: bool,
    json_output: bool,
    xml_output: bool,
) -> None:
    """Send an AST notification (reloadconfig) to matching endpoints."""
    _run_notification(
        ctx, filters, "reloadconfig", force, host, user, password, insecure, ca, timeout,
        max_retries, retry_max_total, debug, json_output, xml_output,
    )


@main.command(name="failback", context_settings=_HELP_SETTINGS)
@_fixed_notification_options
@click.pass_context
def failback_command(
    ctx: click.Context,
    filters: Dict[str, str],
    force: bool,
    host: Optional[str],
    user: Optional[str],
    password: Optional[str],
    insecure: bool,
    ca: Optional[str],
    timeout: float,
    max_retries: int,
    retry_max_total: float,
    progress: bool,
    debug: bool,
    json_output: bool,
    xml_output: bool,
) -> None:
    """Send an AST notification (failback) to matching endpoints."""
    _run_notification(
        ctx, filters, "failback", force, host, user, password, insecure, ca, timeout,
        max_retries, retry_max_total, debug, json_output, xml_output,
    )


def cli_main() -> None:
    main(prog_name="asm-emweb-client.py")


if __name__ == "__main__":
    cli_main()
