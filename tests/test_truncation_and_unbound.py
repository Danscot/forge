"""Regression tests: truncated patch responses and the PatchParseError scoping bug."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge_harness.agents.base import PatchParseError
from forge_harness.agents.coder_agent import CoderAgent
from forge_harness.core.state import Task
from forge_harness.tools.file_ops import FileOps


class FakeRuntime:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.events = []

    def complete(self, agent, messages, image_paths=None, profile_override=None):
        self.calls.append({"messages": messages, "cap": (profile_override or {}).get("max_output_tokens")})
        content, finish = self.replies.pop(0)
        return SimpleNamespace(content=content, finish_reason=finish, reasoning="", usage={})

    def _emit(self, name, **kw):
        self.events.append((name, kw))


def _agent(tmp_path, runtime):
    cfg = {"llm": {"structured_repair_attempts": 1, "patch_generation": {}}, "project": {"harness_dir": ".harness"}}
    return CoderAgent(runtime, tmp_path, cfg), FileOps(tmp_path, cfg)


def _task():
    return Task(id="home", type="screen", title="Home", description="Home screen")


PLAN = {"operations": [{"type": "create_file", "path": "src/screens/Home.tsx"}]}
GOOD = json.dumps({
    "summary": "ok", "registry_changes": [],
    "operations": [{"type": "create_file", "path": "src/screens/Home.tsx", "content": "export default function Home(){return null}"}],
})


def test_truncation_grows_cap_and_recovers(tmp_path):
    truncated = '{"summary":"x","operations":[{"type":"create_file","path":"src/screens/Home.tsx","content":"import React'
    rt = FakeRuntime([(truncated, "length"), (GOOD, "stop")])
    agent, fops = _agent(tmp_path, rt)
    patch = agent.generate_patch(_task(), PLAN, None, {}, None, fops)
    assert patch["operations"][0]["path"] == "src/screens/Home.tsx"
    assert rt.calls[1]["cap"] == rt.calls[0]["cap"] * 2
    # the cut-off reply must not be fed back into the retry
    assert all(m["content"] != truncated for m in rt.calls[1]["messages"])
    assert any(name == "output_truncated" for name, _ in rt.events)


def test_garbage_twice_raises_patch_parse_error_with_raw_attempts(tmp_path):
    rt = FakeRuntime([("no json here", "stop"), ("still none", "stop")])
    agent, fops = _agent(tmp_path, rt)
    with pytest.raises(PatchParseError) as info:
        agent.generate_patch(_task(), PLAN, None, {}, None, fops)
    assert info.value.raw_attempts == ["no json here", "still none"]


def test_persistent_truncation_raises_patch_parse_error(tmp_path):
    rt = FakeRuntime([("{", "length"), ("{", "length")])
    agent, fops = _agent(tmp_path, rt)
    with pytest.raises(PatchParseError):
        agent.generate_patch(_task(), PLAN, None, {}, None, fops)
