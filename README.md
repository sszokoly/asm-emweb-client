# asm-emweb-client

Python client library and CLI for the Avaya Aura Session Manager EM Web Service
(`https://<system-manager>/ASM/ws`): list SIP registrations, count them, and send
AST device notifications such as reboot.

## Install

```bash
uv sync            # or: pip install .
```

## Configuration

Base URL, user, and password are resolved in this order: CLI flag
(`--base-url`, `--user`, `--password`), then environment variable
(`SMGR_BASE_URL`, `SMGR_USER`, `SMGR_PASSWORD`), then a `.env` file in the
current directory.

```bash
# .env
SMGR_BASE_URL=https://smgr.example.com
SMGR_USER=admin
SMGR_PASSWORD=secret
```

## CLI example

```bash
# Total number of registrations (one fast limit=0 request, prints a bare integer)
uv run -m asm_emweb_client count --insecure

# Registrations whose IP starts with 10.10.48. (starts-with prefix matching)
uv run -m asm_emweb_client list --insecure --filter ipAddress=10.10.48. --limit 20

# Reboot one user's AST devices (shows the match count and asks for confirmation)
uv run -m asm_emweb_client reboot --insecure --filter login=alice

# Scripted: skip the confirmation prompt / omit list pagination progress
uv run -m asm_emweb_client reboot --insecure --force --filter login=alice
uv run -m asm_emweb_client list --insecure --no-progress > registrations.txt

# Investigate a slow request with detailed diagnostics on stderr
uv run -m asm_emweb_client count --insecure --debug
```

## Request diagnostics

By default `list` reports only `Fetched N / M registrations` per page on stderr.
Use `--no-progress` to suppress those lines. Pass `--debug` on any branch for
detailed request diagnostics on stderr: start/end lines with phase timing
(connecting, TLS handshake, sending request, waiting for server response,
downloading response), a `still waiting` heartbeat about every 10 seconds, and
503 cache-unloaded retry or give-up lines. `--debug` still emits diagnostics
when combined with `--no-progress`; stdout is never affected (`count` still
prints a bare integer). The diagnostics come from the `asm_emweb_client` logger,
which is silent unless the CLI or your own logging configuration enables it.

Filter names are validated (case-insensitive); unknown names are rejected
before anything is sent. Run `<command> --help` for the list of valid names
and the other options (`--ca`, `--timeout`, `--max-retries`, ...).

## Single-File Build

Build a compressed, base64-encoded launcher for a deployment target that has
Python 3.9+ and this project's runtime dependencies (`click`, `httpx2`,
`requests`, and `python-dotenv`) installed:

```bash
uv run python scripts/build_single_file.py
```

The default output is `dist/asm-emweb-client.py`. The builder verifies that the
bundle is deterministic. Use `--output-dir DIR` to choose another location,
`--shebang PATH` to add an interpreter line, `--keep-flat` to retain readable
uncompressed source, and `--with-readme` to write target deployment notes:

```bash
uv run python scripts/build_single_file.py --output-dir bundle --shebang /usr/bin/python3 --keep-flat --with-readme
```

## API example

```python
from asm_emweb_client import AsmEmWebClient, ApiError

client = AsmEmWebClient(
    host="https://smgr.example.com",
    user="admin",
    password="secret",
    verify=False,          # or a CA bundle path
)

try:
    print("Registrations:", client.count_registrations())

    for reg in client.iter_registrations({"login": "alice"}):
        print(reg.login, reg.ipAddress, reg.deviceModel, reg.ast)

    result = client.send_notify("reboot", {"login": "alice"})
    print(f"requested={result.requested} sent={result.sent}")
except ApiError as exc:
    print("Request failed:", exc)
```

An `AsyncAsmEmWebClient` with the same methods prefixed by `a`
(`acount_registrations`, `afetch_page`, `asend_notify`, ...) is also available.
Library calls pass filters through unvalidated; the CLI validates filter names.
