from __future__ import annotations

from pathlib import Path

import pytest

from forge_harness.agents.base import AgentOutputError
from forge_harness.agents.coder_agent import CoderAgent
from forge_harness.core.runner import HarnessRunner
from forge_harness.tools.file_ops import FileOps


def _runner() -> HarnessRunner:
    runner = HarnessRunner.__new__(HarnessRunner)
    runner.config = {
        "validation": {
            "functional_weight": 0.6,
            "visual_weight": 0.4,
            "require_typecheck": True,
            "require_runtime_render": True,
            "require_functional_checks": True,
            "require_visual_critic_when_mockup_exists": True,
            "fail_on_page_errors": True,
            "fail_on_console_errors": False,
            "minimum_quality_score": 75,
        }
    }
    return runner


def _passing_technical_evidence() -> tuple[dict, dict]:
    return (
        {"passed": True},
        {
            "page_errors": [],
            "functional_pass": True,
            "test_results": [{"passed": True}],
            "console_errors": [],
        },
    )


def test_foundation_task_does_not_fail_because_future_screen_is_not_composed():
    typecheck, render = _passing_technical_evidence()
    report = _runner()._evaluate_gates(
        typecheck, render, None, None, {}, has_mockup=False, task_type="foundation"
    )
    assert report["accepted"] is True
    assert report["visual_gate_required"] is False
    assert report["critic_pass"] is True
    assert report["quality_score"] == 100
    assert report["rejection_reasons"] == []


def test_screen_task_still_requires_visual_critic():
    typecheck, render = _passing_technical_evidence()
    report = _runner()._evaluate_gates(
        typecheck, render, None, None, {}, has_mockup=False, task_type="screen"
    )
    assert report["accepted"] is False
    assert report["visual_gate_required"] is True
    assert any("Visual review is required" in reason for reason in report["rejection_reasons"])


def test_gate_report_explains_conflicting_critic_quality_scores():
    typecheck, render = _passing_technical_evidence()
    critic = {
        "status": "PASS",
        "functional_score": 100,
        "visual_score": 0,
        "quality_score": 92,  # Deliberately inconsistent with the returned sub-scores.
        "critical_failure": False,
        "issues": [],
    }
    report = _runner()._evaluate_gates(
        typecheck, render, None, critic, {}, has_mockup=False, task_type="screen"
    )
    assert report["quality_score"] == 60
    assert report["accepted"] is False
    assert any("Effective quality score 60.0" in reason for reason in report["rejection_reasons"])


def test_noop_replace_text_is_rejected_as_a_plan(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\n", encoding="utf-8")
    agent = CoderAgent.__new__(CoderAgent)
    plan = {
        "risk": "low",
        "operations": [{
            "type": "replace_text",
            "path": "src/App.tsx",
            "old_text": "'Home'",
            "new_text": "'Home'",
        }],
    }
    with pytest.raises(AgentOutputError, match="No-op replace_text"):
        agent._validate_plan(plan, FileOps(tmp_path))


def test_noop_replace_text_is_rejected_in_patch(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\n", encoding="utf-8")
    agent = CoderAgent.__new__(CoderAgent)
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"}]}
    patch_ops = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'", "new_text": "'Home'"}]
    with pytest.raises(AgentOutputError, match="No-op replace_text"):
        agent._validate_patch_against_plan(patch_ops, plan, FileOps(tmp_path))


def test_critic_effective_score_is_recomputed_from_component_scores(tmp_path: Path):
    from forge_harness.agents.critic_agent import CriticAgent

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                '{"status":"PASS","functional_score":100,"visual_score":0,"quality_score":92,"critical_failure":false,"issues":[]}'
            ]

        def complete(self, agent, messages, **kwargs):
            return type("Result", (), {"content": self.responses.pop(0)})()

        def _emit(self, *args, **kwargs):
            return None

    project = tmp_path
    screenshot = project / "shot.png"
    screenshot.write_bytes(b"fake")
    agent = CriticAgent(FakeRuntime(), project, {
        "project": {"harness_dir": ".harness"},
        "validation": {"functional_weight": 0.6, "visual_weight": 0.4},
    })
    from forge_harness.core.state import Task
    report = agent.inspect(
        Task(id="screen", type="screen", title="Home", description="Render home"),
        "test spec", None, {}, [], screenshot,
    )
    assert report["reported_quality_score"] == 92
    assert report["quality_score"] == 60


def test_visual_review_scope_is_limited_to_screen_integration_or_mockup():
    from forge_harness.core.state import Task

    runner = _runner()
    foundation = Task(id="foundation", type="foundation", title="Tokens", description="Create tokens")
    shared = Task(id="shared", type="shared_component", title="Card", description="Create card")
    screen = Task(id="home", type="screen", title="Home", description="Build screen")
    mockup_component = Task(
        id="card_visual", type="shared_component", title="Card from mockup",
        description="Implement card", mockup_paths=["input/mockups/card.png"],
    )
    assert runner._requires_visual_review(foundation) is False
    assert runner._requires_visual_review(shared) is False
    assert runner._requires_visual_review(screen) is True
    assert runner._requires_visual_review(mockup_component) is True


def test_foundation_gate_ignores_out_of_scope_screen_browser_failures():
    """A design-system task must not be rejected by selectors for a future screen."""
    typecheck = {"passed": True}
    render = {
        "page_errors": [],
        "functional_pass": False,
        "test_results": [
            {"action": "assert_visible", "selector": ".course-card", "passed": False, "error": "Timeout"},
            {"action": "assert_visible", "selector": ".search-input", "passed": False, "error": "Timeout"},
        ],
        "console_errors": [],
    }
    report = _runner()._evaluate_gates(
        typecheck, render, None, None, {}, has_mockup=False, task_type="design_tokens"
    )
    assert report["accepted"] is True
    assert report["functional_gate_required"] is False
    assert report["functional_pass"] is True
    assert report["quality_score"] == 100


def test_screen_task_still_fails_its_own_functional_browser_checks():
    typecheck = {"passed": True}
    render = {
        "page_errors": [],
        "functional_pass": False,
        "test_results": [{"action": "assert_visible", "selector": "h1", "passed": False, "error": "Timeout"}],
        "console_errors": [],
    }
    report = _runner()._evaluate_gates(
        typecheck, render, None, None, {}, has_mockup=False, task_type="screen"
    )
    assert report["accepted"] is False
    assert report["functional_gate_required"] is True
    assert report["functional_pass"] is False


def test_effective_test_plan_only_runs_for_screen_scoped_tasks():
    from forge_harness.core.state import Task

    step = {"action": "assert_visible", "selector": ".course-card"}
    foundation = Task(id="tokens", type="design_tokens", title="Tokens", description="Create tokens", test_plan=[step])
    shared = Task(id="card", type="shared_component", title="Card", description="Create card", test_plan=[step])
    screen = Task(id="home", type="screen", title="Home", description="Build Home", test_plan=[step])
    component_mockup = Task(
        id="card_mockup", type="shared_component", title="Card", description="Build card",
        mockup_paths=["input/card.png"], test_plan=[step],
    )
    assert HarnessRunner._effective_test_plan(foundation) == []
    assert HarnessRunner._effective_test_plan(shared) == []
    assert HarnessRunner._effective_test_plan(screen) == [step]
    assert HarnessRunner._effective_test_plan(component_mockup) == [step]


def test_planner_discards_screen_test_plan_from_non_screen_task(tmp_path: Path):
    import json
    from forge_harness.agents.planner import PlannerAgent

    class FakeRuntime:
        def __init__(self):
            self.profile_overrides = []

        def complete(self, agent, messages, **kwargs):
            self.profile_overrides.append(kwargs.get("profile_override"))
            content = {
                "app_summary": "Example",
                "tasks": [
                    {
                        "id": "tokens", "type": "design_tokens", "title": "Tokens",
                        "description": "Create tokens", "test_plan": [
                            {"action": "assert_visible", "selector": ".course-card"}
                        ],
                    },
                    {
                        "id": "home", "type": "screen", "title": "Home",
                        "description": "Build home", "test_plan": [
                            {"action": "assert_visible", "selector": "h1"}
                        ],
                    },
                ],
            }
            return type("Result", (), {"content": json.dumps(content), "usage": {}, "finish_reason": "stop"})()

        def _emit(self, *args, **kwargs):
            return None

    root = tmp_path / "project"
    root.mkdir()
    runtime = FakeRuntime()
    agent = PlannerAgent(runtime, root, {"llm": {"structured_repair_attempts": 0}})
    _, tasks = agent.plan("Build an app", {"mockups": []}, FileOps(root))
    assert tasks[0].type == "design_tokens"
    assert tasks[0].test_plan == []
    assert tasks[1].type == "screen"
    assert len(tasks[1].test_plan) == 1
    assert runtime.profile_overrides[0]["max_output_tokens"] >= 6000
