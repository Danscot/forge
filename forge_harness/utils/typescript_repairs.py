"""Conservative, evidence-driven TypeScript repairs for generated React Native code.

Only applies a small set of unambiguous fixes to files already covered by an
approved create_file/overwrite_file operation. Anything outside that boundary
is left for the normal model repair loop.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

_SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}
_DIAGNOSTIC_RE = re.compile(
    r"(?m)^(?P<path>.+?\.(?:tsx?|jsx?))\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+TS(?P<code>\d+):\s*(?P<message>[^\r\n]+)"
)


def _normalise_relative(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def _diagnostics(typecheck: dict[str, Any]) -> list[dict[str, Any]]:
    output = "\n".join(
        str(typecheck.get(key, "") or "")
        for key in ("stderr", "stdout")
    )
    return [
        {
            "path": match.group("path").strip(),
            "line": int(match.group("line")),
            "col": int(match.group("col")),
            "code": match.group("code"),
            "message": match.group("message").strip(),
        }
        for match in _DIAGNOSTIC_RE.finditer(output)
    ]


def _relative_source_path(raw_path: str, root: Path) -> str | None:
    path = Path(raw_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    normal = raw_path.replace("\\", "/")
    if normal.startswith(("./", "../")):
        try:
            return (root / normal).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    return normal.lstrip("/")


def _approved_full_write_paths(plan: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for operation in plan.get("operations", []) or []:
        if not isinstance(operation, dict):
            continue
        if operation.get("type") not in {"create_file", "overwrite_file"}:
            continue
        path = str(operation.get("path") or "").replace("\\", "/").lstrip("/")
        if path:
            paths.add(path)
    return paths


def _find_unique_module_candidate(root: Path, importer: str, module_specifier: str) -> str | None:
    """Find a unique in-project source file that matches a missing relative import suffix."""
    if not module_specifier.startswith("."):
        return None
    expected = (Path(importer).parent / module_specifier).as_posix()
    expected_path = root / expected
    if expected_path.exists():
        return None

    module_parts = [part for part in module_specifier.replace("\\", "/").split("/") if part not in {".", "..", ""}]
    if not module_parts:
        return None
    candidates: list[Path] = []
    ignored = {"node_modules", ".git", ".harness", ".expo"}
    try:
        source_files = root.rglob("*")
        for candidate in source_files:
            if not candidate.is_file() or candidate.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(part in ignored for part in candidate.parts):
                continue
            stem_parts = list(Path(relative).with_suffix("").parts)
            if len(stem_parts) >= len(module_parts) and stem_parts[-len(module_parts):] == module_parts:
                candidates.append(candidate)
            elif candidate.stem == "index" and list(candidate.parent.parts)[-len(module_parts):] == module_parts:
                candidates.append(candidate)
    except OSError:
        return None

    unique = sorted({candidate.resolve() for candidate in candidates})
    if len(unique) != 1:
        return None
    candidate = unique[0]
    importer_parent = (root / Path(importer).parent).resolve()
    target = candidate.parent if candidate.stem == "index" else candidate.with_suffix("")
    relative_target = posixpath.relpath(target.as_posix(), importer_parent.as_posix())
    if not relative_target.startswith("."):
        relative_target = "./" + relative_target
    return relative_target


def _fix_missing_relative_import(source: str, root: Path, relative: str, message: str) -> tuple[str, str | None]:
    match = re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", message)
    if not match:
        return source, None
    old_specifier = match.group(1)
    new_specifier = _find_unique_module_candidate(root, relative, old_specifier)
    if not new_specifier or new_specifier == old_specifier:
        return source, None
    # Change only module-specifier literals in import/export declarations; don't rewrite
    # similarly-spelled runtime strings elsewhere in the file.
    pattern = re.compile(
        r"((?:from\s*|import\s*)['\"])(" + re.escape(old_specifier) + r")(['\"])",
    )
    updated, count = pattern.subn(lambda m: m.group(1) + new_specifier + m.group(3), source)
    return (updated, f"corrected unique relative import {old_specifier!r} -> {new_specifier!r}") if count else (source, None)


def _fix_undefined_safe_area_view(source: str, message: str) -> tuple[str, str | None]:
    if not re.search(r"Cannot find name ['\"]SafeAreaView['\"]", message):
        return source, None
    if not re.search(r"<\s*SafeAreaView\b|</\s*SafeAreaView\s*>", source):
        return source, None

    # This diagnostic means the JSX identifier is unbound, not that React Native
    # lacks the component. Preserve safe-area behavior by importing the known
    # React Native export instead of downgrading the screen to a plain View.
    rn_import = re.search(
        r"import\s*\{(?P<names>[^}]+)\}\s*from\s*(['\"])react-native\2\s*;?",
        source,
    )
    if rn_import:
        names = rn_import.group("names")
        if re.search(r"(?:^|,)\s*SafeAreaView(?:\s+as\s+\w+)?\s*(?:,|$)", names):
            return source, None
        updated_names = names.rstrip()
        if updated_names.endswith(","):
            updated_names = updated_names[:-1]
        updated_names += ", SafeAreaView"
        updated = source[:rn_import.start("names")] + updated_names + source[rn_import.end("names"):]
        return updated, "added the missing SafeAreaView named import from react-native"

    updated = "import { SafeAreaView } from 'react-native';\n" + source
    return updated, "added the missing SafeAreaView named import from react-native"


def _balanced_brace_end(text: str, opening_brace: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening_brace, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _style_definition_has_color(source: str, style_name: str) -> bool:
    # This repair is permitted only where the referenced StyleSheet entry clearly
    # represents a style object with a color member, not a color token/string.
    pattern = re.compile(
        r"\b" + re.escape(style_name) + r"\s*:\s*\{(?:(?!\n\s*[A-Za-z_$][\w$]*\s*:).)*?\bcolor\s*:",
        re.DOTALL,
    )
    return bool(pattern.search(source))


def _fix_style_object_passed_as_color(source: str, line_number: int, column: int) -> tuple[str, str | None]:
    lines = source.splitlines(keepends=True)
    if line_number < 1 or line_number > len(lines):
        return source, None
    offset = sum(len(line) for line in lines[: line_number - 1]) + max(0, column - 1)
    open_tag = source.rfind("<", 0, min(len(source), offset + 1))
    close_tag = source.find(">", offset)
    if open_tag < 0 or close_tag < 0 or "</" in source[open_tag:offset]:
        return source, None
    tag = source[open_tag:close_tag + 1]
    color_match = re.search(r"\bcolor\s*=\s*\{([^{}]*)\}", tag, flags=re.DOTALL)
    if not color_match:
        return source, None
    expression = color_match.group(1).strip()
    style_match = re.search(r"\bstyles\.([A-Za-z_$][\w$]*)\b", expression)
    if not style_match or "&&" not in expression or not _style_definition_has_color(source, style_match.group(1)):
        return source, None

    style_prop = re.search(r"\bstyle\s*=\s*\{", tag)
    if style_prop:
        brace_open = tag.find("{", style_prop.start())
        brace_end = _balanced_brace_end(tag, brace_open)
        if brace_end is None:
            return source, None
        current_expression = tag[brace_open + 1:brace_end - 1].strip()
        new_style = "style={[" + current_expression + ", " + expression + "]}"
        tag = tag[:style_prop.start()] + new_style + tag[brace_end:]
        # Locate/remove the original color attribute in the updated tag.
        color_again = re.search(r"\s+color\s*=\s*\{([^{}]*)\}", tag, flags=re.DOTALL)
        if not color_again:
            return source, None
        tag = tag[:color_again.start()] + tag[color_again.end():]
    else:
        tag = tag[:color_match.start()] + "style={" + expression + "}" + tag[color_match.end():]

    updated = source[:open_tag] + tag + source[close_tag + 1:]
    return updated, "moved a conditional StyleSheet object from the color prop into the style prop"


def apply_safe_typescript_repairs(
    project_root: Path,
    typecheck: dict[str, Any],
    approved_plan: dict[str, Any],
) -> list[dict[str, str]]:
    """Apply conservative repairs for specific, compiler-confirmed generated-code errors.

    The helper never expands task scope: only source files already planned as create_file or
    overwrite_file operations may be changed. It returns an empty list if no safe fix matches.
    """
    root = project_root.resolve()
    allowed = _approved_full_write_paths(approved_plan)
    if not allowed or typecheck.get("passed") is True:
        return []
    diagnostics = _diagnostics(typecheck)
    if not diagnostics:
        return []

    source_cache: dict[str, str] = {}
    fix_messages: dict[str, list[str]] = {}
    # Process lower-file edits first so an added import cannot shift the line numbers
    # of a later diagnostic in the same source file.
    ordered_diagnostics = sorted(
        diagnostics, key=lambda item: (str(item["path"]), -int(item["line"])),
    )
    for diagnostic in ordered_diagnostics:
        relative = _relative_source_path(str(diagnostic["path"]), root)
        if not relative or relative not in allowed or Path(relative).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
            if not target.is_file() or target.stat().st_size > 200_000:
                continue
            source = source_cache.get(relative)
            if source is None:
                source = target.read_text(encoding="utf-8", errors="replace")

            code = str(diagnostic["code"])
            message = str(diagnostic["message"])
            fix: str | None = None
            if code == "2307":
                source, fix = _fix_missing_relative_import(source, root, relative, message)
            if code == "2304":
                source, fix = _fix_undefined_safe_area_view(source, message)
            if code == "2322":
                source, fix = _fix_style_object_passed_as_color(
                    source, int(diagnostic["line"]), int(diagnostic["col"]),
                )
            if fix:
                source_cache[relative] = source
                fix_messages.setdefault(relative, []).append(fix)
        except (OSError, UnicodeError, ValueError):
            continue

    results: list[dict[str, str]] = []
    for relative, updated in source_cache.items():
        if not fix_messages.get(relative):
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
            previous = target.read_text(encoding="utf-8", errors="replace")
            if previous == updated:
                continue
            target.write_text(updated, encoding="utf-8")
            results.append({"path": relative, "fixes": "; ".join(dict.fromkeys(fix_messages[relative]))})
        except (OSError, UnicodeError, ValueError):
            continue
    return results
