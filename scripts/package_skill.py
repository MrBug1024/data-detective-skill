#!/usr/bin/env python3
"""Build a portable runtime-only Data Detective skill archive (standard library)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile


SKILL_NAME = "data-detective"
SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRECTORIES = {
    "scripts": {".py"},
    "references": {".md"},
    "schemas": {".json"},
    "agents": {".yaml", ".yml"},
}
ROOT_FILES = ("SKILL.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md")
EXCLUDED_PARTS = {"__pycache__", ".git", ".dd_cache", ".venv", "venv", "node_modules"}


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _excluded(path: Path) -> bool:
    return any(part in EXCLUDED_PARTS or part.startswith(".") for part in path.parts)


def runtime_files(root: Path = SKILL_ROOT) -> list[Path]:
    """Return validated relative resource paths; never follow linked resources."""
    root = Path(root).resolve()
    skill_file = root / "SKILL.md"
    if _is_link(skill_file) or not skill_file.is_file():
        raise ValueError("SKILL.md must be a regular file, not a symbolic link")
    text = skill_file.read_text(encoding="utf-8-sig")
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.DOTALL)
    if not frontmatter or not re.search(
        r"^name:\s*['\"]?" + re.escape(SKILL_NAME) + r"['\"]?\s*$",
        frontmatter.group(1), re.MULTILINE,
    ):
        raise ValueError(f"SKILL.md must declare name: {SKILL_NAME}")
    for required in ("scripts", "references", "schemas"):
        if not (root / required).is_dir():
            raise ValueError(f"Missing runtime directory: {required}")

    files: list[Path] = []
    for name in ROOT_FILES:
        path = root / name
        if _is_link(path):
            raise ValueError(f"Linked resource is not supported: {name}")
        if path.is_file():
            files.append(Path(name))
    for name, suffixes in RUNTIME_DIRECTORIES.items():
        directory = root / name
        if _is_link(directory):
            raise ValueError(f"Linked resource is not supported: {name}")
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError(f"Runtime resource must be a directory: {name}")
        for current, directories, filenames in os.walk(directory, followlinks=False):
            current_path = Path(current)
            for child in list(directories):
                relative = (current_path / child).relative_to(root)
                if _excluded(relative):
                    directories.remove(child)
                elif _is_link(current_path / child):
                    raise ValueError(f"Linked resource is not supported: {relative}")
            for filename in filenames:
                path = current_path / filename
                relative = path.relative_to(root)
                if _excluded(relative):
                    continue
                allowed = path.suffix.lower() in suffixes or relative == Path("scripts/requirements.txt")
                if not allowed:
                    continue
                if _is_link(path) or not path.is_file():
                    raise ValueError(f"Resource must be a regular file: {relative}")
                files.append(relative)
    return sorted(files, key=lambda path: path.as_posix())


def validate_skill(root: Path = SKILL_ROOT) -> int:
    """Check the files that are actually shipped; no network or extra dependencies."""
    root = Path(root).resolve()
    files = runtime_files(root)
    shipped = {path.as_posix() for path in files}
    if "LICENSE" not in shipped or not (root / "LICENSE").read_text(encoding="utf-8-sig").strip():
        raise ValueError("A nonempty LICENSE is required for this project's distribution")
    text = (root / "SKILL.md").read_text(encoding="utf-8-sig")
    header = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.DOTALL).group(1)
    description = re.search(r"^description:\s*(.+)$", header, re.MULTILINE)
    if not description or description.group(1).strip() in {"''", '""', "|", ">", "null", "~"}:
        raise ValueError("SKILL.md requires a nonempty single-line description")

    for relative in files:
        path = root / relative
        if relative.parts[0] == "schemas" and path.suffix == ".json":
            try:
                document = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON schema {relative}: {exc}") from exc
            if not isinstance(document, dict):
                raise ValueError(f"JSON schema must be an object: {relative}")
        if path.suffix.lower() != ".md":
            continue
        content = path.read_text(encoding="utf-8-sig")
        for match in re.finditer(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", content):
            target = match.group(1).strip()
            if target.startswith("<"):
                target = target[1:target.find(">")]
            else:
                target = re.split(r"\s+[\"']", target, maxsplit=1)[0]
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(root) or resolved.relative_to(root).as_posix() not in shipped:
                raise ValueError(f"Link is outside the runtime package: {relative} -> {target}")

    metadata = root / "agents/openai.yaml"
    if metadata.exists():
        content = metadata.read_text(encoding="utf-8-sig")
        interface = re.search(r"^interface:\s*\n((?:[ \t]+[^\n]*\n?|[ \t]*\n)+)", content, re.MULTILINE)
        if not interface:
            raise ValueError("agents/openai.yaml requires an interface mapping")
        for field in ("display_name", "short_description", "default_prompt"):
            match = re.search(r"^  " + field + r":\s*(\"[^\n]*\")\s*$",
                              interface.group(1), re.MULTILINE)
            try:
                value = json.loads(match.group(1)) if match else None
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid quoted interface.{field} in agents/openai.yaml") from exc
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"agents/openai.yaml requires a quoted interface.{field}")
            if field == "short_description" and not 25 <= len(value) <= 64:
                raise ValueError("interface.short_description must contain 25 to 64 characters")
            if field == "default_prompt" and f"${SKILL_NAME}" not in value:
                raise ValueError(f"interface.default_prompt must mention ${SKILL_NAME}")
    return len(files)


def package_skill(output: Path, root: Path = SKILL_ROOT) -> tuple[int, str]:
    """Write a deterministic ZIP with one data-detective/ top-level directory."""
    root = Path(root).resolve()
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("Archive output must end in .zip")
    files = runtime_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix=".data-detective-", suffix=".zip", dir=output.parent, delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative in files:
                entry = zipfile.ZipInfo(f"{SKILL_NAME}/{relative.as_posix()}")
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, (root / relative).read_bytes())
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)
    return len(files), hashlib.sha256(output.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("dist/data-detective.zip"),
                        help="Archive path (default: dist/data-detective.zip)")
    parser.add_argument("--check", action="store_true",
                        help="Validate runtime metadata, local links, and schemas without writing an archive")
    args = parser.parse_args(argv)
    try:
        if args.check:
            count = validate_skill()
            print(f"Validated {SKILL_NAME}: {count} runtime files, metadata, local links, and schemas")
            return 0
        validate_skill()
        output = args.out.expanduser().resolve()
        count, digest = package_skill(output)
        checksum = output.with_name(output.name + ".sha256")
        checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8", newline="\n")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Packaged {count} runtime files: {output}")
    print(f"SHA256: {digest}")
    print(f"Checksum file: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
