from pathlib import Path

from forge_harness.utils.typescript_repairs import apply_safe_typescript_repairs


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _plan(*operations: tuple[str, str]) -> dict:
    return {"operations": [{"type": kind, "path": path} for kind, path in operations]}


def _typecheck(message: str, *, line: int = 1, col: int = 1, code: int = 2307, path: str = "src/components/Card.tsx") -> dict:
    return {
        "passed": False,
        "exit_code": 2,
        "stdout": f"{path}({line},{col}): error TS{code}: {message}\n",
        "stderr": "",
    }


def test_repairs_unique_relative_import_only_in_approved_full_write(tmp_path: Path) -> None:
    _write(tmp_path, "src/theme/tokens.ts", "export const theme = {};\n")
    card = "import { theme } from './theme/tokens';\nexport const Card = () => null;\n"
    _write(tmp_path, "src/components/Card.tsx", card)

    fixes = apply_safe_typescript_repairs(
        tmp_path,
        _typecheck("Cannot find module './theme/tokens' or its corresponding type declarations."),
        _plan(("create_file", "src/components/Card.tsx")),
    )

    assert len(fixes) == 1
    assert "from '../theme/tokens'" in (tmp_path / "src/components/Card.tsx").read_text()


def test_does_not_modify_file_outside_approved_full_write(tmp_path: Path) -> None:
    _write(tmp_path, "src/theme/tokens.ts", "export const theme = {};\n")
    card = "import { theme } from './theme/tokens';\nexport const Card = () => null;\n"
    _write(tmp_path, "src/components/Card.tsx", card)

    fixes = apply_safe_typescript_repairs(
        tmp_path,
        _typecheck("Cannot find module './theme/tokens' or its corresponding type declarations."),
        _plan(("replace_text", "src/components/Card.tsx")),
    )

    assert fixes == []
    assert (tmp_path / "src/components/Card.tsx").read_text() == card


def test_replaces_unbound_safe_area_view_with_existing_view_import(tmp_path: Path) -> None:
    screen = "import { View, Text } from 'react-native';\nexport default function Screen() { return <SafeAreaView><Text>Hi</Text></SafeAreaView>; }\n"
    _write(tmp_path, "src/screens/HomeScreen.tsx", screen)

    fixes = apply_safe_typescript_repairs(
        tmp_path,
        _typecheck("Cannot find name 'SafeAreaView'.", code=2304, path="src/screens/HomeScreen.tsx"),
        _plan(("create_file", "src/screens/HomeScreen.tsx")),
    )

    updated = (tmp_path / "src/screens/HomeScreen.tsx").read_text()
    assert len(fixes) == 1
    assert "<SafeAreaView>" in updated
    assert "</SafeAreaView>" in updated
    assert "SafeAreaView" in next(line for line in updated.splitlines() if line.lstrip().startswith("import "))


def test_moves_style_object_from_color_prop_to_style_and_merges_existing_style(tmp_path: Path) -> None:
    screen = """import React from 'react';
import { Text, StyleSheet } from 'react-native';
const active = true;
const styles = StyleSheet.create({
  tabItem: { padding: 8 },
  tabActive: { color: '#007AFF' },
});
export default function Screen() {
  return <Text style={styles.tabItem} color={active && styles.tabActive}>Discover</Text>;
}
"""
    _write(tmp_path, "src/screens/HomeScreen.tsx", screen)
    lines = screen.splitlines()
    line_number = next(i for i, line in enumerate(lines, start=1) if "color={active" in line)
    column = lines[line_number - 1].index("color=") + 1

    fixes = apply_safe_typescript_repairs(
        tmp_path,
        _typecheck("Type 'false | { color: string; }' is not assignable to type 'false | void'.", code=2322,
                   path="src/screens/HomeScreen.tsx", line=line_number, col=column),
        _plan(("overwrite_file", "src/screens/HomeScreen.tsx")),
    )

    updated = (tmp_path / "src/screens/HomeScreen.tsx").read_text()
    assert len(fixes) == 1
    assert "color={active && styles.tabActive}" not in updated
    assert "style={[styles.tabItem, active && styles.tabActive]}" in updated


def test_does_not_replace_safe_area_when_not_reported_by_compiler(tmp_path: Path) -> None:
    screen = "import { View } from 'react-native';\nexport default () => <SafeAreaView />;\n"
    _write(tmp_path, "src/screens/HomeScreen.tsx", screen)
    no_match = _typecheck("Some other type error.", code=2322, path="src/screens/HomeScreen.tsx")

    fixes = apply_safe_typescript_repairs(
        tmp_path, no_match, _plan(("overwrite_file", "src/screens/HomeScreen.tsx"))
    )

    assert fixes == []
    assert (tmp_path / "src/screens/HomeScreen.tsx").read_text() == screen


def test_applies_multiple_compiler_fixes_to_one_approved_screen(tmp_path: Path) -> None:
    screen = """import { View, Text, StyleSheet } from 'react-native';
const active = true;
const styles = StyleSheet.create({
  tabItem: { padding: 8 },
  tabActive: { color: '#007AFF' },
});
export default function Screen() {
  return <SafeAreaView><Text style={styles.tabItem} color={active && styles.tabActive}>Discover</Text></SafeAreaView>;
}
"""
    _write(tmp_path, "src/screens/HomeScreen.tsx", screen)
    error_line = next(i for i, line in enumerate(screen.splitlines(), start=1) if "color={active" in line)
    error_column = screen.splitlines()[error_line - 1].index("color=") + 1
    typecheck = {
        "passed": False,
        "exit_code": 2,
        "stdout": (
            "src/screens/HomeScreen.tsx(8,10): error TS2304: Cannot find name 'SafeAreaView'.\n"
            f"src/screens/HomeScreen.tsx({error_line},{error_column}): error TS2322: Type 'false | {{ color: string; }}' is not assignable to type 'false | void'.\n"
        ),
        "stderr": "",
    }

    fixes = apply_safe_typescript_repairs(
        tmp_path, typecheck, _plan(("create_file", "src/screens/HomeScreen.tsx"))
    )

    updated = (tmp_path / "src/screens/HomeScreen.tsx").read_text()
    assert len(fixes) == 1
    assert "<SafeAreaView>" in updated
    assert "SafeAreaView" in updated.splitlines()[0]
    assert "color={active && styles.tabActive}" not in updated
    assert "style={[styles.tabItem, active && styles.tabActive]}" in updated
