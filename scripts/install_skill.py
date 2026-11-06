#!/usr/bin/env python3
"""Install this local checkout into Codex or Claude Code without overwriting skills."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from package_skill import SKILL_NAME, SKILL_ROOT, runtime_files, validate_skill


def default_destination(target: str) -> Path:
    """Return the skills parent directory for an explicitly selected host."""
    if target == "codex":
        return Path.home() / ".agents" / "skills"
    if target == "claude":
        return Path.home() / ".claude" / "skills"
    raise ValueError(f"Unknown installation target: {target}")


def install_skill(destination: Path, root: Path = SKILL_ROOT) -> tuple[Path, int]:
    """Copy the runtime into destination/data-detective; reserve it exclusively."""
    root = Path(root).resolve()
    validate_skill(root)
    files = runtime_files(root)
    destination = Path(destination).expanduser().resolve() / SKILL_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError:
        raise ValueError(
            f"Destination already exists: {destination}. "
            "Back up and remove the existing installation before installing again."
        ) from None
    try:
        for relative in files:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, target)
    except BaseException:
        # This invocation created the directory exclusively; leave no partial skill.
        shutil.rmtree(destination)
        raise
    return destination, len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("codex", "claude"),
                        help="Host whose user skills directory receives the skill")
    parser.add_argument("--dest", type=Path,
                        help="Explicit skills parent directory (overrides --target destination)")
    args = parser.parse_args(argv)
    if not args.target and args.dest is None:
        parser.error("Choose --target codex|claude or an explicit --dest skills directory")
    try:
        parent = args.dest if args.dest is not None else default_destination(args.target)
        destination, count = install_skill(parent)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {SKILL_NAME} ({count} runtime files): {destination}")
    print("Python dependencies are separate: run python -m pip install -r <installed-path>/scripts/requirements.txt")
    print(f"Use ${SKILL_NAME} in your next Codex turn or a new Claude Code session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
