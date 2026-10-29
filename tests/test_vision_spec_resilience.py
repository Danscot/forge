from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from forge_harness.agents.vision_spec_agent import VisionSpecAgent, VISION_SPEC_SCHEMA
from forge_harness.core.state import Task
from forge_harness.tools.file_ops import FileOps


class FakeVisionRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.events = []

    def complete(self, agent, messages, image_paths=None, profile_override=None):
        self.calls.append({
            "agent": agent,
            "messages": messages,
            "image_paths": image_paths,
            "profile": profile_override or {},
        })
        content, finish_reason = self.responses.pop(0)
        return SimpleNamespace(content=content, finish_reason=finish_reason, usage={})

    def _emit(self, name, **kwargs):
        self.events.append((name, kwargs))


def _vision_json():
    return '''{
      "screen": "LearnHub course discovery screen",
      "layout": "A vertically scrolling mobile screen with a compact top header, large two-line greeting, rounded search field, section heading, four horizontal course cards, and a fixed bottom navigation bar.",
      "hierarchy": ["header", "greeting", "search", "popular courses", "bottom navigation"],
      "design_tokens": {"colors": {"primary": "blue", "background": "near-white"}, "spacing": {"screen": 20}},
      "regions": [{"name": "Header", "position": "top", "layout": "horizontal row with brand left and avatar right", "details": "Keep generous horizontal padding and align items vertically."}],
      "responsive_rules": ["Allow the course list to scroll without covering the bottom navigation."],
      "assets": [{"purpose": "course thumbnail placeholders", "path": null}],
      "accessibility": ["Label the course search input."],
      "functional_notes": ["Filter course titles as text is entered."],
      "ambiguities": []
    }'''


def test_vision_retries_provider_truncation_with_larger_budget_and_schema(tmp_path: Path):
    mockup = tmp_path / "input" / "mockups" / "1.png"
    mockup.parent.mkdir(parents=True)
    mockup.write_bytes(b"mockup-bytes-for-fake-runtime")
    runtime = FakeVisionRuntime([
        ('{"screen":"LearnHub","layout":"partial"}', "length"),
        (_vision_json(), "stop"),
    ])
    config = {
        "project": {"harness_dir": ".harness", "max_image_bytes": 7_000_000},
        "llm": {"structured_repair_attempts": 1, "max_images_per_request": 6, "max_total_image_bytes": 14_000_000},
    }
    agent = VisionSpecAgent(runtime, tmp_path, config)
    task = Task(
        id="discover", type="screen", title="Discover screen", description="Match reference",
        mockup_paths=["input/mockups/1.png"],
    )

    result = agent.specify(task, "Reproduce the attached reference.", FileOps(tmp_path, config), {})

    assert result["layout"].startswith("A vertically scrolling mobile screen")
    assert len(result["regions"]) == 1
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["profile"]["max_output_tokens"] == 5000
    assert runtime.calls[1]["profile"]["max_output_tokens"] == 10000
    schema = runtime.calls[0]["profile"]["structured_output_schema"]
    assert schema["name"] == "forge_vision_response"
    assert schema["schema"]["required"] == VISION_SPEC_SCHEMA["required"]
    assert any(event == "output_truncated" or (event == "structured_output_repair" and "truncated" in payload.get("error", "")) for event, payload in runtime.events)


def test_vision_retries_valid_json_missing_required_layout(tmp_path: Path):
    mockup = tmp_path / "input" / "mockups" / "1.png"
    mockup.parent.mkdir(parents=True)
    mockup.write_bytes(b"fake")
    runtime = FakeVisionRuntime([
        ('{"screen":"LearnHub","regions":[{"name":"Header","position":"top","layout":"row","details":"Brand."}]}', "stop"),
        (_vision_json(), "stop"),
    ])
    config = {"project": {"harness_dir": ".harness"}, "llm": {"structured_repair_attempts": 1}}
    agent = VisionSpecAgent(runtime, tmp_path, config)
    task = Task(id="discover", type="screen", title="Discover", description="Build screen", mockup_paths=["input/mockups/1.png"])

    result = agent.specify(task, "Reference required.", FileOps(tmp_path, config), {})

    assert result["layout"]
    assert len(runtime.calls) == 2
    assert any(event == "structured_output_repair" for event, _ in runtime.events)
