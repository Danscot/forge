from pathlib import Path

from forge_harness.agents.base import extract_json
from forge_harness.core.state import Task
from forge_harness.core.store import StateStore


def test_extract_json_from_code_fence_and_prose():
    assert extract_json('```json\n{"status":"PASS"}\n```')["status"] == "PASS"
    assert extract_json('Result follows: {"status":"FAIL","n":2} thanks')["n"] == 2


def test_state_store_persists_run_and_tasks(tmp_path: Path):
    store = StateStore(tmp_path / ".harness" / "state.db")
    store.create_run("run1", str(tmp_path), "spec", "autonomous", {"example": True})
    task = Task(id="home", type="screen", title="Home", description="Build home").to_dict()
    store.save_tasks("run1", [task])
    loaded = store.get_run("run1")
    assert loaded is not None
    assert loaded["metadata"]["example"] is True
    assert loaded["tasks"][0]["id"] == "home"
    store.update_task("run1", {**task, "status": "passed", "last_report": {"accepted": True}}, 0)
    assert store.get_run("run1")["tasks"][0]["status"] == "passed"
