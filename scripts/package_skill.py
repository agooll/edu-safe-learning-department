#!/usr/bin/env python3
"""Validate SkillHub constraints and build a submission ZIP."""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any


def configure_utf8_stdio() -> None:
    """Make JSON CLI output deterministic on Windows and POSIX."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


MAX_TOTAL = 100 * 1024 * 1024
MAX_FILE = 10 * 1024 * 1024
MAX_FILES = 500
RESERVED = {"admin", "api", "dashboard", "search", "auth", "me", "global", "system", "static", "assets", "health"}
ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".html", ".css", ".csv", ".pdf",
    ".toml", ".xml", ".xsd", ".xsl", ".dtd", ".ini", ".cfg", ".env",
    ".js", ".cjs", ".mjs", ".ts", ".py", ".sh", ".rb", ".go", ".rs", ".java", ".kt",
    ".lua", ".sql", ".r", ".bat", ".ps1", ".zsh", ".bash",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico",
    ".doc", ".xls", ".ppt", ".docx", ".xlsx", ".pptx",
}
TEXT_EXTENSIONS = ALLOWED_EXTENSIONS - {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".doc", ".xls", ".ppt", ".docx", ".xlsx", ".pptx",
}
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\b(api_key|access_key|secret|password|token)\s*[:=]\s*[\"']?([^\s\"']{12,})"),
)
PLACEHOLDERS = ("your", "example", "sample", "placeholder", "changeme", "replace", "dummy", "mock", "test", "fake", "todo", "xxx", "redacted")
REJECTED_WRAPPER_BASENAMES = {"open" + "ai.yaml", "open" + "ai.yml"}
REJECTED_WRAPPER_REFERENCES = (
    re.compile(r"(?i)\bagents[\\/]+" + re.escape("open" + "ai") + r"\.ya?ml\b"),
)


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: missing YAML frontmatter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"{path}: unclosed YAML frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values


def validate_name(name: str) -> str | None:
    if not 2 <= len(name) <= 64:
        return "name length must be 2-64 characters"
    if name in RESERVED:
        return "name is reserved"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return "name has invalid hyphen placement"
    if any("A" <= char <= "Z" for char in name):
        return "name must be lowercase"
    if not all(char == "-" or char.isalnum() for char in name):
        return "name contains unsupported characters"
    return None


def ignored(path: Path) -> bool:
    return any(part in {"__pycache__", ".git", "__MACOSX"} for part in path.parts) or path.name == ".DS_Store" or path.name.startswith("._") or path.suffix == ".pyc"


def secret_warning(text: str) -> bool:
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0).lower()
            if not any(marker in value for marker in PLACEHOLDERS) and not set(value) <= {"x", "*", "-"}:
                return True
    return False


def check_magic(path: Path, data: bytes) -> bool:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return data.startswith(b"GIF8")
    if suffix == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if suffix == ".ico":
        return data.startswith(b"\x00\x00\x01\x00")
    if suffix == ".pdf":
        return data.startswith(b"%PDF")
    return True


def inspect_skill(root: Path) -> tuple[list[Path], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    root_skill = root / "SKILL.md"
    if not root_skill.is_file():
        errors.append("root SKILL.md is missing")
        return [], errors, warnings

    try:
        frontmatter = parse_frontmatter(root_skill)
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        name_error = validate_name(name)
        if name_error:
            errors.append(f"SKILL.md name: {name_error}")
        if name != root.name:
            errors.append(f"folder name {root.name!r} does not match frontmatter name {name!r}")
        if not description:
            errors.append("SKILL.md description is missing")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(str(exc))

    files = [path for path in root.rglob("*") if path.is_file() and not ignored(path.relative_to(root))]
    if len(files) > MAX_FILES:
        errors.append(f"file count {len(files)} exceeds {MAX_FILES}")

    total = 0
    for path in files:
        relative = path.relative_to(root)
        relative_posix = relative.as_posix()
        size = path.stat().st_size
        total += size
        suffix = path.suffix.lower()
        lowered_parts = tuple(part.lower() for part in relative.parts)
        if len(lowered_parts) >= 2 and lowered_parts[-2] == "agents" and lowered_parts[-1] in REJECTED_WRAPPER_BASENAMES:
            errors.append(f"platform-rejected wrapper metadata: {relative_posix}")
        if suffix not in ALLOWED_EXTENSIONS:
            errors.append(f"unsupported extension: {relative_posix}")
        if size > MAX_FILE:
            errors.append(f"file exceeds 10 MB: {relative_posix}")
        data = path.read_bytes()
        if b"\x00" in data and suffix in TEXT_EXTENSIONS:
            errors.append(f"NUL byte in text file: {relative_posix}")
        if suffix in TEXT_EXTENSIONS:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"text file is not UTF-8: {relative_posix}")
                continue
            if secret_warning(text):
                warnings.append(f"possible credential: {relative_posix}")
            if any(pattern.search(text) for pattern in REJECTED_WRAPPER_REFERENCES):
                errors.append(f"platform-rejected wrapper reference: {relative_posix}")
        elif not check_magic(path, data):
            errors.append(f"file signature mismatch: {relative_posix}")

        if path.name == "SKILL.md":
            try:
                metadata = parse_frontmatter(path)
                nested_name = metadata.get("name", "")
                nested_error = validate_name(nested_name)
                if nested_error:
                    errors.append(f"{relative_posix} name: {nested_error}")
                if not metadata.get("description"):
                    errors.append(f"{relative_posix} description is missing")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                errors.append(str(exc))

    if total > MAX_TOTAL:
        errors.append(f"total size {total} exceeds 100 MB")
    return sorted(files), errors, warnings


def build_zip(root: Path, destination: Path, files: list[Path]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(root)
            archive.write(path, (Path(root.name) / relative).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("output_zip", type=Path)
    parser.add_argument("--allow-warnings", action="store_true", help="Build even when credential warnings exist")
    args = parser.parse_args()
    root = args.skill_dir.resolve()
    files, errors, warnings = inspect_skill(root)
    if warnings and not args.allow_warnings:
        errors.append("credential warnings must be resolved before packaging")
    if not errors:
        build_zip(root, args.output_zip.resolve(), files)
    report: dict[str, Any] = {
        "ok": not errors,
        "skill": root.name,
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "output_zip": str(args.output_zip.resolve()) if not errors else None,
        "warnings": warnings,
        "errors": errors,
    }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if not errors else 1


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
