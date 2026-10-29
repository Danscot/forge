from pathlib import Path

import pytest

from forge_harness.agents.base import AgentOutputError
from forge_harness.agents.coder_agent import CoderAgent
from forge_harness.tools.file_ops import FileOps


def _validator() -> CoderAgent:
    # This unit exercises the deterministic validator without invoking an LLM.
    return CoderAgent.__new__(CoderAgent)


def test_patch_must_include_each_approved_operation(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\nconst sub = 'Welcome';\n", encoding="utf-8")
    plan = {
        "operations": [
            {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"},
            {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Welcome'"},
        ]
    }
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'", "new_text": "'Dashboard'"}]
    with pytest.raises(AgentOutputError, match="omitted approved operation"):
        _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_patch_cannot_add_unplanned_operation(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\n", encoding="utf-8")
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"}]}
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Other'", "new_text": "'Dashboard'"}]
    with pytest.raises(AgentOutputError, match="omitted approved"):
        _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_anchor_matches_with_trailing_whitespace_drift(tmp_path: Path):
    """LLM plan and patch calls may emit different trailing whitespace in old_text."""
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\nconst sub = 'Welcome';\n", encoding="utf-8")
    # Plan anchor exists exactly once in-file; patch anchor differs by trailing whitespace.
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home';"}]}
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home';  ", "new_text": "'Dashboard'"}]
    # The normalized anchor is accepted, then canonicalized to the exact approved
    # source anchor so FileOps can apply it without a second whitespace mismatch.
    file_ops = FileOps(tmp_path)
    _validator()._validate_patch_against_plan(patch, plan, file_ops)
    assert patch[0]["old_text"] == "'Home';"
    file_ops.apply_operations(patch)
    assert target.read_text(encoding="utf-8") == "const title = 'Dashboard'\nconst sub = 'Welcome';\n"


def test_anchor_does_not_match_different_content(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\n", encoding="utf-8")
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"}]}
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Goodbye'", "new_text": "'Dashboard'"}]
    with pytest.raises(AgentOutputError, match="omitted approved"):
        _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_anchors_match_normalizes_whitespace():
    validator = CoderAgent.__new__(CoderAgent)
    assert validator._anchors_match("const x = 1;\n", "const x = 1;  \n") is True
    assert validator._anchors_match("const x = 1;\r\n", "const x = 1;\n") is True
    assert validator._anchors_match("const x = 1;\n", "const y = 1;\n") is False
    assert validator._anchors_match(None, None) is True
    assert validator._anchors_match("abc", None) is False


def test_generate_patch_repairs_malformed_json_without_replanning(tmp_path: Path):
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                "The patch is below: {not valid JSON}",
                '{"summary":"Create theme","operations":[{"type":"create_file","path":"src/theme.ts","content":"export const colors = {};\\n"}],"registry_changes":[]}',
            ]
            self.calls = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": self.responses.pop(0)})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    config = {"project": {"harness_dir": ".harness"}, "llm": {"structured_repair_attempts": 1}}
    agent = CoderAgent(runtime, tmp_path, config)
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert len(runtime.calls) == 2, "The malformed response should be repaired locally, not replanned"
    assert runtime.calls[0]["kwargs"]["profile_override"]["thinking_enabled"] is False
    assert "Validation error" in runtime.calls[1]["messages"][-1]["content"]


def test_generate_patch_repairs_operation_that_is_not_in_approved_plan(tmp_path: Path):
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                '{"summary":"wrong path","operations":[{"type":"create_file","path":"src/not-approved.ts","content":"export {};"}]}',
                '{"summary":"approved path","operations":[{"type":"create_file","path":"src/theme.ts","content":"export {};"}]}',
            ]
            self.repair_prompt = ""

        def complete(self, agent, messages, **kwargs):
            if len(self.responses) == 1:
                self.repair_prompt = messages[-1]["content"]
            return type("Result", (), {"content": self.responses.pop(0)})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {"project": {"harness_dir": ".harness"}, "llm": {"structured_repair_attempts": 1}})
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert "omitted approved" in runtime.repair_prompt


def test_patch_data_testid_is_normalized_to_react_native_testid(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"expo":"1.0.0","react-native":"0.88.0"}}', encoding="utf-8"
    )
    target = tmp_path / "src" / "screens" / "HomeScreen.tsx"
    target.parent.mkdir(parents=True)
    target.write_text(
        "import { Text } from 'react-native';\nexport function HomeScreen() { return <Text data-testid=\"home-heading\">Home</Text>; }\n",
        encoding="utf-8",
    )
    patch = [{
        "type": "overwrite_file",
        "path": "src/screens/HomeScreen.tsx",
        "content": "import { Text } from 'react-native';\nexport function HomeScreen() { return <Text data-testid=\"home-heading\">Home</Text>; }\n",
    }]
    plan = {"operations": [{"type": "overwrite_file", "path": "src/screens/HomeScreen.tsx"}]}

    _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))

    assert 'testID="home-heading"' in patch[0]["content"]
    assert 'data-testid="home-heading"' not in patch[0]["content"]


def test_patch_output_budget_scales_with_approved_operation_count(tmp_path: Path):
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": self.response})()

        def _emit(self, *args, **kwargs):
            return None

    plan_ops = [
        {"type": "create_file", "path": "src/theme.ts"},
        {"type": "create_file", "path": "src/components/CourseCard.tsx"},
        {"type": "create_file", "path": "src/components/CategoryChip.tsx"},
    ]
    patch_ops = [
        {"type": "create_file", "path": op["path"], "content": "export {};\n"}
        for op in plan_ops
    ]
    runtime = FakeRuntime(__import__("json").dumps({"operations": patch_ops}))
    config = {
        "project": {"harness_dir": ".harness"},
        "llm": {
            "structured_repair_attempts": 0,
            "agents": {"coder": {"max_output_tokens": 12000}},
        },
    }
    agent = CoderAgent(runtime, tmp_path, config)
    task = Task(id="foundation", type="foundation", title="Create theme and components", description="Add three files")

    agent.generate_patch(task, {"operations": plan_ops}, None, {}, {}, FileOps(tmp_path))

    profile = runtime.calls[0]["kwargs"]["profile_override"]
    assert profile["thinking_enabled"] is False
    assert profile["max_output_tokens"] == 12000  # configured hard cap limits the 13,500 estimated budget


def test_plan_schema_error_is_repaired_without_restarting_task(tmp_path: Path):
    import json
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                {"summary": "empty plan", "risk": "low", "operations": [], "files_to_read": [], "validation_plan": [], "registry_changes": []},
                {"summary": "create theme", "risk": "low", "operations": [{"type": "create_file", "path": "src/theme.ts", "target": "theme", "reason": "Add theme", "content_outline": "Export typed colors"}], "files_to_read": [], "validation_plan": [], "registry_changes": []},
            ]
            self.calls = []
            self.events = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": json.dumps(self.responses.pop(0))})()

        def _emit(self, kind, **payload):
            self.events.append((kind, payload))

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1, "effort_policy": {"coder_plan": {"thinking_enabled": False, "max_output_tokens": 1200}}},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add design tokens")

    plan = agent.plan_changes(task, "Build a small app", None, {}, {}, FileOps(tmp_path))

    assert plan["operations"][0]["path"] == "src/theme.ts"
    assert len(runtime.calls) == 2, "A schema-invalid plan should be repaired locally before retrying the whole task"
    assert any(kind == "structured_output_repair" and payload.get("stage") == "planning_changes" for kind, payload in runtime.events)
    assert "non-empty operations array" in runtime.calls[1]["messages"][-1]["content"]


def test_generate_patch_skips_earlier_empty_operations_object(tmp_path: Path):
    """A provider response may contain an earlier decodable stub before its actual patch."""
    import json
    from forge_harness.core.state import Task

    valid_patch = {
        "summary": "Create theme",
        "operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export const colors = {};\n"}],
        "registry_changes": [],
    }

    class FakeRuntime:
        def __init__(self):
            self.calls = 0

        def complete(self, agent, messages, **kwargs):
            self.calls += 1
            # The first candidate is valid JSON but not a patch. The second is.
            content = '{"summary":"intermediate stub","operations":[]}\nFinal patch:\n' + json.dumps(valid_patch)
            return type("Result", (), {"content": content})()

        def _emit(self, *args, **kwargs):
            return None

    agent = CoderAgent(FakeRuntime(), tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert agent.runtime.calls == 1, "A later valid object should be selected without another LLM call"


def test_generate_patch_unwraps_provider_patch_object(tmp_path: Path):
    """Accept a common wrapper without bypassing validation against the approved plan."""
    import json
    from forge_harness.core.state import Task

    class FakeRuntime:
        def complete(self, agent, messages, **kwargs):
            return type("Result", (), {"content": json.dumps({
                "summary": "wrapped patch",
                "patch": {"operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export {};\n"}]},
            })})()

        def _emit(self, *args, **kwargs):
            return None

    agent = CoderAgent(FakeRuntime(), tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 0},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"


def test_plan_changes_selects_later_nonempty_plan_object(tmp_path: Path):
    """Do not spend a structured-repair call when a valid plan follows an empty stub."""
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.calls = 0

        def complete(self, agent, messages, **kwargs):
            self.calls += 1
            return type("Result", (), {"content": (
                '{"summary":"intermediate stub","risk":"low","operations":[]}\n'
                '{"summary":"actual plan","risk":"low","operations":[{"type":"create_file",'
                '"path":"src/theme.ts","target":"theme","reason":"Add tokens",'
                '"content_outline":"Export colors"}],"files_to_read":[],"validation_plan":[],"registry_changes":[]}'
            )})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")

    plan = agent.plan_changes(task, "Build a small app", None, {}, {}, FileOps(tmp_path))

    assert plan["summary"] == "actual plan"
    assert plan["operations"][0]["path"] == "src/theme.ts"
    assert runtime.calls == 1


def test_generate_patch_tries_later_candidate_that_matches_approved_plan(tmp_path: Path):
    """A non-empty but unapproved JSON object must not hide a later valid patch."""
    import json
    from forge_harness.core.state import Task

    valid_patch = {
        "summary": "approved patch",
        "operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export {};\n"}],
    }
    response = (
        json.dumps({"summary": "intermediate wrong path", "operations": [
            {"type": "create_file", "path": "src/not-approved.ts", "content": "export {};"}
        ]})
        + "\nFinal patch:\n"
        + json.dumps(valid_patch)
    )

    class FakeRuntime:
        calls = 0

        def complete(self, agent, messages, **kwargs):
            self.calls += 1
            return type("Result", (), {"content": response})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["summary"] == "approved patch"
    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert runtime.calls == 1


def test_patch_generation_uses_provider_native_json_schema(tmp_path: Path):
    """Constrain NVIDIA patch responses to require a non-empty operations array."""
    import json
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.calls = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": json.dumps({
                "summary": "Create theme",
                "operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export {};\n"}],
                "registry_changes": [],
            })})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    config = {
        "project": {"harness_dir": ".harness"},
        "llm": {
            "default_provider": "nvidia",
            "providers": {"nvidia": {"base_url": "https://integrate.api.nvidia.com/v1"}},
            "agents": {"coder": {"model": "nvidia:nvidia/nemotron-3.5-super-vl-120b-a12b"}},
            "structured_repair_attempts": 0,
        },
    }
    agent = CoderAgent(runtime, tmp_path, config)
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"]
    profile = runtime.calls[0]["kwargs"]["profile_override"]
    schema = profile["structured_output_schema"]["schema"]
    assert schema["properties"]["operations"]["minItems"] == 1
    assert "operations" in schema["required"]


def test_plan_repairs_keep_constrained_json_schema_enabled(tmp_path: Path):
    import json
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.calls = []
            self.responses = [
                {"summary": "empty", "risk": "low", "files_to_read": [], "operations": [], "validation_plan": [], "registry_changes": []},
                {"summary": "create theme", "risk": "low", "files_to_read": [], "operations": [{"type": "create_file", "path": "src/theme.ts", "target": "theme", "reason": "Add tokens", "content_outline": "Export colors"}], "validation_plan": [], "registry_changes": []},
            ]

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": json.dumps(self.responses.pop(0))})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    config = {
        "project": {"harness_dir": ".harness"},
        "llm": {
            "default_provider": "nvidia",
            "providers": {"nvidia": {"base_url": "https://integrate.api.nvidia.com/v1"}},
            "agents": {"coder": {"model": "nvidia:nvidia/nemotron-3.5-super-vl-120b-a12b"}},
            "structured_repair_attempts": 1,
        },
    }
    agent = CoderAgent(runtime, tmp_path, config)
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = agent.plan_changes(task, "Build a small app", None, {}, {}, FileOps(tmp_path))

    assert plan["operations"]
    assert len(runtime.calls) == 2
    for call in runtime.calls:
        profile = call["kwargs"]["profile_override"]
        assert profile["structured_output_schema"]["schema"]["properties"]["operations"]["minItems"] == 1


def test_json_schema_is_disabled_for_unconfigured_compatible_provider(tmp_path: Path):
    config = {
        "project": {"harness_dir": ".harness"},
        "llm": {
            "default_provider": "custom",
            "providers": {"custom": {"base_url": "http://localhost/v1"}},
            "agents": {"coder": {"model": "custom:sample-model"}},
        },
    }
    agent = CoderAgent.__new__(CoderAgent)
    agent.config = config
    agent.name = "coder"
    profile = {"max_output_tokens": 2000}
    result = agent._structured_profile_override(profile, schema_name="forge_test", schema={"type": "object"})
    assert result["structured_output_schema"] == {"name": "forge_test", "schema": {"type": "object"}}
    from forge_harness.llm.runtime import LLMRuntime
    runtime = LLMRuntime(config)
    payload = runtime._build_payload(
        "custom", "sample-model", {**profile, **result}, [{"role": "user", "content": "Return JSON"}], True,
    )
    assert "response_format" not in payload


def test_legacy_nvidia_structured_output_format_includes_required_type():
    """Old project configs using format=nvidia must use the complete API envelope."""
    from forge_harness.llm.runtime import LLMRuntime

    schema = {
        "type": "object",
        "properties": {"operations": {"type": "array", "items": {"type": "object"}}},
        "required": ["operations"],
    }
    config = {
        "llm": {
            "default_provider": "nvidia",
            "providers": {
                "nvidia": {
                    "base_url": "https://integrate.api.nvidia.com/v1",
                    "structured_output": True,
                    "structured_output_format": "nvidia",
                }
            },
            "agents": {"planner": {"model": "nvidia:nvidia/nemotron-3.5-super-vl-120b-a12b"}},
        }
    }
    runtime = LLMRuntime(config)
    profile = runtime._profile("planner")
    profile["structured_output_schema"] = {"name": "forge_test", "schema": schema}
    payload = runtime._build_payload(
        "nvidia", "nvidia/nemotron-3.5-super-vl-120b-a12b", profile,
        [{"role": "user", "content": "Return JSON"}], True, agent="planner",
    )
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "forge_test"
    assert payload["response_format"]["json_schema"]["schema"] == schema


def test_planner_inventory_uses_raw_source_without_line_number_prefixes(tmp_path: Path):
    from forge_harness.core.state import Task

    target = tmp_path / "App.tsx"
    target.write_text("import React from 'react';\nexport default function App() { return <Text>Home</Text>; }\n", encoding="utf-8")
    task = Task(id="screen", type="screen", title="Mount screen", description="Render Home")
    agent = CoderAgent.__new__(CoderAgent)
    inventory = agent._relevant_inventory(task, FileOps(tmp_path))

    assert "export default function App()" in inventory
    assert "1 | import React" not in inventory
    assert "raw source; no line numbers" in inventory


def test_repair_plan_must_cover_diagnostic_testids():
    diagnostics = {
        "root_causes": [
            {"cause": 'Missing data-testid="home-heading"'},
            {"cause": 'Missing data-testid="search-input"'},
            {"cause": 'Missing data-testid="course-title"'},
        ],
        "next_steps": ["Add home heading, search input, and course titles"],
    }
    unrelated_plan = {
        "summary": "Clean up old navigation options; the screen already works",
        "operations": [{
            "type": "replace_text", "path": "src/screens/HomeScreen.tsx",
            "target": "navigation options", "reason": "Remove deprecated property",
            "old_text": "old", "new_text": "new",
        }],
    }
    with pytest.raises(AgentOutputError, match="home-heading.*search-input.*course-title"):
        _validator()._validate_diagnostic_coverage(unrelated_plan, diagnostics)


def test_repair_plan_passes_when_all_diagnostic_testids_are_covered():
    diagnostics = {
        "root_causes": [{"cause": 'Missing data-testid="home-heading" and data-testid="search-input"'}],
        "next_steps": ['Render course item with testID="course-title"'],
    }
    plan = {
        "summary": "Repair missing home screen elements",
        "operations": [{
            "type": "create_file", "path": "src/screens/HomeScreen.tsx",
            "target": "home-heading, search-input, course-title",
            "reason": "Render the required heading, searchable input and course list",
            "content_outline": "Include testID home-heading, search-input, and course-title; filter courses by title",
        }],
    }
    _validator()._validate_diagnostic_coverage(plan, diagnostics)


def test_diagnostic_coverage_ignores_prose_words_after_testid():
    diagnostics = {
        "root_causes": [{"cause": "Property testID: string is missing; testID requirement not met for data-testid=\"course-card\""}],
        "next_steps": ["testID prop must be forwarded"],
    }
    plan = {"summary": "Forward course-card testID", "operations": [{
        "type": "replace_text", "path": "a", "target": "course-card", "reason": "r",
        "old_text": "a", "new_text": "testID={testID} course-card"}]}
    _validator()._validate_diagnostic_coverage(plan, diagnostics)


def test_diagnostic_coverage_ignores_json_structure_words():
    diagnostics = {"root_causes": [
        {"cause": "testID on heading present", "evidence": "TS2613 default import", "confidence": "high"}],
        "next_steps": ["Keep testID home-heading"]}
    plan = {"summary": "fix import", "operations": []}
    _validator()._validate_diagnostic_coverage(plan, diagnostics)  # must not raise ('evidence' etc.)


def test_normalize_plan_drops_noop_and_fixes_create_overwrite(tmp_path):
    from forge_harness.tools.file_ops import FileOps
    (tmp_path / "a.tsx").write_text("x")
    fo = FileOps(tmp_path)
    plan = {"operations": [
        {"type": "replace_text", "path": "a.tsx", "old_text": "x", "new_text": "x"},
        {"type": "create_file", "path": "a.tsx", "content_outline": "redo"},
        {"type": "overwrite_file", "path": "b.tsx", "content_outline": "new"},
    ]}
    _validator()._normalize_plan(plan, fo)
    assert [o["type"] for o in plan["operations"]] == ["overwrite_file", "create_file"]


def test_patch_drops_unplanned_ops_and_accepts_overwrite(tmp_path):
    from forge_harness.tools.file_ops import FileOps
    (tmp_path / "a.tsx").write_text("old")
    (tmp_path / "other.tsx").write_text("keep")
    fo = FileOps(tmp_path)
    plan = {"operations": [{"type": "overwrite_file", "path": "a.tsx"}]}
    ops = [
        {"type": "create_file", "path": "a.tsx", "content": "new"},   # model says create, disk says overwrite
        {"type": "replace_text", "path": "other.tsx", "old_text": "keep", "new_text": "changed"},  # unplanned
    ]
    _validator()._validate_patch_against_plan(ops, plan, fo)
    assert len(ops) == 1 and ops[0]["type"] == "overwrite_file"
    fo.apply_operations(ops)
    assert (tmp_path / "a.tsx").read_text() == "new"
    assert (tmp_path / "other.tsx").read_text() == "keep"


def test_diagnosed_stale_anchor_is_promoted_to_whole_file_repair(tmp_path: Path):
    from forge_harness.core.state import Task

    screen = tmp_path / "src" / "screens" / "HomeScreen.tsx"
    screen.parent.mkdir(parents=True)
    screen.write_text("renderItem={({ item, index }) => <CourseCard key={item.id} />};\n", encoding="utf-8")
    task = Task(
        id="home_screen", type="screen", title="Repair Home screen", description="Repair missing UI",
        acceptance_criteria=["Render heading", "Filter course list"],
    )
    plan = {"operations": [{
        "type": "replace_text", "path": "src/screens/HomeScreen.tsx",
        "target": "FlatList renderItem callback", "reason": "Fix selector index",
        "old_text": "renderItem={({ item }) => <CourseCard key={item.id} />};",
        "new_text": "renderItem={({ item, index }) => <CourseCard key={item.id} />};",
    }]}
    diagnostics = {
        "repair_strategy": 'Render a heading, search input and course cards with data-testid="home-heading", data-testid="search-input", data-testid="course-card-0".',
        "target_files": ["src/screens/HomeScreen.tsx"],
        "root_causes": [{"cause": "Required selectors are missing", "evidence": "home-heading and search-input timed out"}],
        "next_steps": ["Implement filtering"],
    }
    _validator()._normalize_stale_repair_anchors(task, plan, FileOps(tmp_path), diagnostics)
    operation = plan["operations"][0]
    assert operation["type"] == "overwrite_file"
    assert "old_text" not in operation and "new_text" not in operation
    assert "home-heading" in operation["content_outline"]
    assert "search-input" in operation["content_outline"]


def test_screen_plan_adds_root_app_mount_when_missing(tmp_path: Path):
    from forge_harness.core.state import Task

    (tmp_path / "package.json").write_text('{"dependencies":{"expo":"1.0.0"}}', encoding="utf-8")
    (tmp_path / "index.ts").write_text("import { registerRootComponent } from 'expo';\nimport App from './App';\nregisterRootComponent(App);\n", encoding="utf-8")
    (tmp_path / "App.tsx").write_text(
        "export default function App() { return <View><Text>Home</Text></View>; }\n", encoding="utf-8"
    )
    task = Task(id="home_screen", type="screen", title="Create Home screen", description="Build Home")
    plan = {"summary": "Create HomeScreen", "operations": [{
        "type": "create_file", "path": "src/screens/HomeScreen.tsx",
        "target": "Build HomeScreen", "reason": "Required screen", "content_outline": "Heading and course list",
    }]}
    _validator()._ensure_screen_mount_operation(task, plan, FileOps(tmp_path), None)
    app_ops = [operation for operation in plan["operations"] if operation["path"] == "App.tsx"]
    assert len(app_ops) == 1
    assert app_ops[0]["type"] == "overwrite_file"
    assert plan["risk"] == "medium"
    assert "./src/screens/HomeScreen" in app_ops[0]["content_outline"]
    assert "render <HomeScreen />" in app_ops[0]["content_outline"]


def test_screen_plan_upgrades_stale_app_anchor_to_mount_rewrite(tmp_path: Path):
    from forge_harness.core.state import Task

    (tmp_path / "package.json").write_text('{"dependencies":{"expo":"1.0.0"}}', encoding="utf-8")
    (tmp_path / "App.tsx").write_text("export default function App() { return <Text>Home</Text>; }\n", encoding="utf-8")
    task = Task(id="home_screen", type="screen", title="Repair Home", description="Repair screen")
    plan = {"operations": [
        {"type": "overwrite_file", "path": "src/screens/HomeScreen.tsx", "content_outline": "Heading"},
        {"type": "replace_text", "path": "App.tsx", "target": "Mount HomeScreen", "reason": "render it", "old_text": "stale anchor", "new_text": "<HomeScreen />"},
    ]}
    _validator()._ensure_screen_mount_operation(task, plan, FileOps(tmp_path), None)
    app_ops = [operation for operation in plan["operations"] if operation["path"] == "App.tsx"]
    assert len(app_ops) == 1
    assert app_ops[0]["type"] == "overwrite_file"
    assert "stale anchor" not in app_ops[0]
    assert "actually render <HomeScreen />" in app_ops[0]["content_outline"]


def test_plan_rejects_text_operations_for_binary_asset_paths(tmp_path: Path):
    from forge_harness.agents.base import AgentOutputError

    validator = _validator()
    plan = {
        "risk": "low",
        "operations": [{"type": "create_file", "path": "assets/images/course-4.png"}],
    }
    with pytest.raises(AgentOutputError, match="binary asset"):
        validator._validate_plan(plan, FileOps(tmp_path))


def test_patch_rejects_binary_asset_path_even_if_plan_contains_it(tmp_path: Path):
    from forge_harness.tools.file_ops import FileOperationError

    validator = _validator()
    plan = {"operations": [{"type": "create_file", "path": "assets/images/logo.png"}]}
    patch = [{"type": "create_file", "path": "assets/images/logo.png", "content": "not a png"}]
    with pytest.raises(FileOperationError, match="binary asset"):
        validator._validate_patch_against_plan(patch, plan, FileOps(tmp_path))
