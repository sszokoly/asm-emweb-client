"""Build a compressed single-file bundle of asm-emweb-client.

The bundle concatenates every runtime module of the package in dependency
order into one flat source file, strips intra-package imports, compresses
the result with zlib, and wraps it in a small base64 launcher script.

The target platform is Python 3.9+ with Click, requests, and python-dotenv
already installed. httpx2 is optional; all dependencies stay imported, not
bundled.
The original package files are never modified.

Usage:

    uv run python scripts/build_single_file.py [options]
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import re
import shutil
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "asm_emweb_client"

MODULE_ORDER = [
    "config",
    "models",
    "diagnostics",
    "transport",
    "client",
    "cli",
]

EXCLUDED_MODULES = {"__init__", "__main__"}
BUNDLE_NAME = "asm-emweb-client.py"
FLAT_NAME = "asm-emweb-client.flat.py"


class BuildError(Exception):
    """A bundle build or verification step failed."""


def module_path(name: str) -> Path:
    return PACKAGE_DIR / f"{name}.py"


def load_module_source(name: str) -> str:
    path = module_path(name)
    if not path.is_file():
        raise BuildError(f"missing module file: {path}")
    return path.read_text(encoding="utf-8")


def parse_module(name: str) -> ast.Module:
    source = load_module_source(name)
    try:
        return ast.parse(source)
    except SyntaxError as error:
        raise BuildError(f"cannot parse {module_path(name)}: {error}") from error


def check_order_covers_package() -> None:
    actual = {path.stem for path in PACKAGE_DIR.glob("*.py") if path.stem not in EXCLUDED_MODULES}
    listed = set(MODULE_ORDER)
    missing = sorted(actual - listed)
    extra = sorted(listed - actual)
    if missing or extra:
        details = []
        if missing:
            details.append(f"not listed: {missing}")
        if extra:
            details.append(f"listed but absent: {extra}")
        raise BuildError(
            "MODULE_ORDER does not match the package modules (" + "; ".join(details) + ")"
        )


def relative_import_targets(tree: ast.Module) -> List[str]:
    targets: List[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            continue
        if node.module is None:
            targets.extend(alias.name.split(".")[0] for alias in node.names)
        else:
            targets.append(node.module.split(".")[0])
    return targets


def check_dependency_order() -> None:
    position = {name: index for index, name in enumerate(MODULE_ORDER)}
    for name in MODULE_ORDER:
        tree = parse_module(name)
        for target in relative_import_targets(tree):
            if target not in position:
                raise BuildError(f"asm_emweb_client.{name} imports unknown module '{target}'")
            if position[target] >= position[name]:
                raise BuildError(
                    f"asm_emweb_client.{name} imports asm_emweb_client.{target}, "
                    "which is not earlier in MODULE_ORDER"
                )


def top_level_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_name_collisions() -> None:
    seen: Dict[str, str] = {}
    for name in MODULE_ORDER:
        for symbol in top_level_names(parse_module(name)):
            if symbol in seen:
                raise BuildError(
                    f"top-level name '{symbol}' defined in both "
                    f"asm_emweb_client.{seen[symbol]} and asm_emweb_client.{name}"
                )
            seen[symbol] = name


def strip_relative_imports(name: str) -> str:
    source = load_module_source(name)
    tree = ast.parse(source)
    spans = [
        (
            node.lineno,
            node.end_lineno if node.end_lineno is not None else node.lineno,
        )
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.level > 0 or node.module == "__future__")
    ]
    drop = {line_number for start, end in spans for line_number in range(start, end + 1)}
    kept = [line for number, line in enumerate(source.splitlines(), start=1) if number not in drop]
    return normalize_blank_lines(kept)


def normalize_blank_lines(lines: List[str]) -> str:
    normalized: List[str] = []
    blanks = 0
    for line in lines:
        blanks = blanks + 1 if not line.strip() else 0
        if blanks <= 2:
            normalized.append(line)
    text = "\n".join(normalized).strip("\n")
    return text + "\n" if text else ""


def read_version() -> str:
    pyproject = REPO_ROOT / "pyproject.toml"
    try:
        source = pyproject.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(f"could not read {pyproject}: {error}") from error
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', source, re.MULTILINE)
    if match is None:
        raise BuildError("could not read project version from pyproject.toml")
    return match.group(1)


def assemble_flat_source() -> str:
    header = [
        '"""asm-emweb-client single-file flat source (generated).',
        "",
        "Concatenation of the asm_emweb_client package in dependency order.",
        "Regenerate with scripts/build_single_file.py --keep-flat.",
        '"""',
        "from __future__ import annotations",
        f'__version__ = "{read_version()}"',
    ]
    chunks: List[str] = ["\n".join(header)]
    for name in MODULE_ORDER:
        banner = "\n".join(
            [
                "# " + "-" * 66,
                f"# asm_emweb_client.{name}",
                "# " + "-" * 66,
            ]
        )
        chunks.append(banner + "\n" + strip_relative_imports(name).rstrip("\n"))
    chunks.append(
        "# Match package initialization so unconfigured library logging stays silent.\n"
        "logger.addHandler(logging.NullHandler())"
    )
    chunks.append('if __name__ == "__main__":\n    cli_main()')
    return "\n\n\n".join(chunks) + "\n"


def wrap_launcher(flat_source: str, shebang: Optional[str]) -> str:
    blob = base64.encodebytes(zlib.compress(flat_source.encode("utf-8"), 9))
    blob_lines = blob.decode("ascii").splitlines()
    lines: List[str] = []
    if shebang:
        lines.append(f"#!{shebang}")
    lines.extend(
        [
            '"""asm-emweb-client single-file bundle (generated; do not edit).',
            "",
            "The flat asm-emweb-client source is zlib-compressed and base64-encoded",
            "below. Regenerate with scripts/build_single_file.py.",
            '"""',
            "import base64",
            "import traceback",
            "import zlib",
            "",
            "",
            'COMPRESSED_SCRIPT = """\\',
        ]
    )
    lines.extend(blob_lines)
    lines.extend(
        [
            '"""',
            "",
            "",
            "def unwrap_and_decompress(wrapped_text):",
            '    """Unwraps, base64 decodes and decompresses string."""',
            '    base64_str = wrapped_text.replace("\\n", "")',
            "    compressed_bytes = base64.b64decode(base64_str)",
            "    original_string = zlib.decompress(compressed_bytes)",
            '    return original_string.decode("utf-8")',
            "",
            "",
            'if __name__ == "__main__":',
            "    script_content = unwrap_and_decompress(COMPRESSED_SCRIPT)",
            "    try:",
            '        code = compile(script_content, "<asm-emweb-client>", "exec")',
            "        exec(code)",
            "    except SystemExit:",
            "        raise",
            "    except KeyboardInterrupt:",
            "        raise",
            "    except Exception:",
            "        traceback.print_exc()",
            "        raise SystemExit(1)",
        ]
    )
    return "\n".join(lines) + "\n"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit_readme(output_dir: Path, shebang: Optional[str]) -> None:
    python = shebang or "python3"
    readme = (
        "# asm-emweb-client single-file bundle\n"
        "\n"
        "Generated by `scripts/build_single_file.py`. The bundle embeds the full\n"
        "asm_emweb_client package (CLI + API) in one file. It does not bundle\n"
        "third-party dependencies.\n"
        "\n"
        "## Target prerequisites\n"
        "\n"
        "- Python 3.9 or later\n"
        "- Installed `click`, `requests`, and `python-dotenv` packages\n"
        "- Optional: `httpx2` for the preferred HTTP backend and async API\n"
        "\n"
        "## List endpoints that satisfy the filters\n"
        "\n"
        "```console\n"
        f"{python} asm-emweb-client.py list --filter ast=true --filter ipAddress=192.168.11.\n"
        "```\n"
        "\n"
        "## Reboot endpoints that satisfy the filters\n"
        "\n"
        "```console\n"
        f"{python} asm-emweb-client.py reboot --filter ast=true --filter ipAddress=192.168.11.\n"
        "```\n"
        "\n"
        "## Retrieve the total count of endpoints that satisfy the filters\n"
        "\n"
        "```console\n"
        f"{python} asm-emweb-client.py count --filter ast=true --filter ipAddress=192.168.11.\n"
        "```\n"
    )
    readme_path = output_dir / "README.md"
    readme_path.write_text(readme, encoding="utf-8")
    print(f"wrote {readme_path}")


def build(
    output_dir: Path,
    keep_flat: bool,
    shebang: Optional[str],
    with_readme: bool,
) -> Path:
    print("checking module order and collisions")
    check_order_covers_package()
    check_dependency_order()
    check_name_collisions()

    print("assembling flat source")
    flat_source = assemble_flat_source()
    compile(flat_source, "<flat-bundle>", "exec")

    output_dir.mkdir(parents=True, exist_ok=True)
    if keep_flat:
        flat_path = output_dir / FLAT_NAME
        flat_path.write_text(flat_source, encoding="utf-8")
        print(f"wrote {flat_path}")

    launcher = wrap_launcher(flat_source, shebang)
    compile(launcher, "<launcher>", "exec")
    bundle_path = output_dir / BUNDLE_NAME
    bundle_path.write_text(launcher, encoding="utf-8")
    print(f"wrote {bundle_path}")
    if with_readme:
        emit_readme(output_dir, shebang)
    return bundle_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist",
        help="directory for generated artifacts (default: dist)",
    )
    parser.add_argument(
        "--keep-flat",
        action="store_true",
        help="also write the readable uncompressed flat source",
    )
    parser.add_argument(
        "--shebang",
        default=None,
        help="interpreter path to bake into the first line",
    )
    parser.add_argument(
        "--with-readme",
        action="store_true",
        help="also write README.md deployment notes",
    )
    arguments = parser.parse_args(argv)

    try:
        bundle_path = build(
            arguments.output_dir,
            arguments.keep_flat,
            arguments.shebang,
            arguments.with_readme,
        )

        rebuild = build(
            arguments.output_dir / ".determinism",
            False,
            arguments.shebang,
            False,
        )
        if digest(bundle_path) != digest(rebuild):
            raise BuildError("build is not deterministic")
        rebuild.unlink()
        shutil.rmtree(rebuild.parent)

        kib = bundle_path.stat().st_size / 1024.0
        print(f"determinism: identical across rebuilds ({digest(bundle_path)[:12]})")
        print(f"done: {bundle_path} ({kib:.1f} KiB)")
        return 0
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
