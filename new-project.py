#!/usr/bin/env python3
"""Create a HAL tool-extension project from example/simple."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
TEXT_FILES = {
    ".env.example",
    ".gitignore",
    "README.md",
    "hal.yaml.example",
    "pyproject.toml",
    "src/hal_simple/__init__.py",
    "src/hal_simple/tools.py",
    "tests/test_tools.py",
}


def normalized_names(name: str) -> tuple[str, str]:
    """Return the entry-point slug and import-package name for *name*."""
    if not PROJECT_NAME.fullmatch(name):
        raise ValueError(
            "project name must start with a letter and contain only letters, "
            "numbers, hyphens, or underscores"
        )
    slug = re.sub(r"[-_]+", "-", name).lower()
    return slug, slug.replace("-", "_")


def create_project(name: str, parent: Path | None = None) -> Path:
    """Create *name* beside this HAL checkout and return its path."""
    slug, module = normalized_names(name)
    hal_root = Path(__file__).resolve().parent
    source = hal_root / "example" / "simple"
    parent = (parent or hal_root.parent).resolve()
    destination = parent / name

    if destination.exists():
        raise FileExistsError(f"project already exists: {destination}")
    if not source.is_dir():
        raise FileNotFoundError(f"project template is missing: {source}")

    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=parent))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        replacements = {
            "hal-simple": f"hal-{slug}",
            "hal_simple": f"hal_{module}",
            "simple_": f"{module}_",
            "simple": slug,
            "Simple": " ".join(part.capitalize() for part in slug.split("-")),
        }
        for relative in TEXT_FILES:
            path = temporary / relative
            text = path.read_text(encoding="utf-8")
            for old, new in replacements.items():
                text = text.replace(old, new)
            path.write_text(text, encoding="utf-8", newline="\n")

        old_package = temporary / "src" / "hal_simple"
        old_package.rename(temporary / "src" / f"hal_{module}")
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a sibling HAL tool-extension project from example/simple."
    )
    parser.add_argument("name", help="new project directory name")
    args = parser.parse_args(argv)
    try:
        destination = create_project(args.name)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created HAL extension project: {destination}")
    print("Next steps:")
    print("      deactivate  # if a virtual environment is currently active")
    print(f'      cd "{destination}"')
    print("      python -m venv .venv")
    print("      source .venv/Scripts/activate  # Git Bash")
    print("      # .\\.venv\\Scripts\\Activate.ps1  # PowerShell")
    print('      python -m pip install -e "../hal[dev]"')
    print('      python -m pip install -e ".[dev]"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
