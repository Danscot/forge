from __future__ import annotations

import io

from rich.console import Console

from forge_harness.utils.dashboard import Dashboard


def test_streamed_response_is_rendered_as_one_scrollable_message():
    output = io.StringIO()
    dashboard = Dashboard("test-run", "autonomous", {"llm": {"display_reasoning_stream": False}}, console=Console(file=output, width=100, force_terminal=False))
    dashboard.on_llm_event("llm_request_start", {"agent": "coder", "provider": "test", "model": "mock", "attempt": 1, "max_attempts": 2})
    dashboard.on_llm_event("llm_text_delta", {"agent": "coder", "text": '{"operations":['})
    dashboard.on_llm_event("llm_text_delta", {"agent": "coder", "text": '{"type":"create_file"}]}'})

    # Chunks are buffered rather than printed as disconnected chat messages.
    before_done = output.getvalue()
    assert '{"operations":[' not in before_done

    dashboard.on_llm_event("llm_request_done", {"agent": "coder", "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, "duration_s": 2.5, "finish_reason": "stop"})

    outputs = [message for message in dashboard.messages if message.kind == "assistant_output"]
    assert len(outputs) == 1
    assert outputs[0].content == '{"operations":[{"type":"create_file"}]}'
    assert outputs[0].content in output.getvalue()


def test_transcript_history_is_append_only_without_live_dashboard():
    output = io.StringIO()
    dashboard = Dashboard("test-run", "autonomous", {}, console=Console(file=output, width=100, force_terminal=False))
    dashboard.activity_message("INFO", "planning changes")
    dashboard.activity_message("ERROR", "patch failed")

    rendered = output.getvalue()
    assert "planning changes" in rendered
    assert "patch failed" in rendered
    assert len([message for message in dashboard.messages if message.kind == "system"]) == 2
