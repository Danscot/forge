from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from forge_harness.llm.runtime import LLMRuntime


class CompatHandler(BaseHTTPRequestHandler):
    request_count = 0

    def do_POST(self):
        type(self).request_count += 1
        _length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(_length)
        parsed = json.loads(body.decode("utf-8"))
        if type(self).request_count == 1:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"slow down"}}')
            return
        assert parsed["stream"] is True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "Checking "}, "index": 0}]},
            {"choices": [{"delta": {"content": "hello"}, "index": 0}]},
            {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop", "index": 0}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ]
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        return


def test_streaming_retry_usage_and_reasoning(monkeypatch):
    CompatHandler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), CompatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key")
    events = []
    try:
        config = {
            "llm": {
                "default_provider": "test",
                "streaming": True,
                "display_reasoning_stream": True,
                "request_timeout_s": 3,
                "connect_timeout_s": 1,
                "max_retries": 1,
                "initial_backoff_s": 0,
                "max_backoff_s": 0,
                "jitter_fraction": 0,
                "providers": {
                    "test": {
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                        "api_key_env": "TEST_PROVIDER_KEY",
                        "parameter_map": {"max_output_tokens": "max_tokens"},
                        "reasoning_fields": ["reasoning_content"],
                    }
                },
                "agents": {
                    "coder": {
                        "model": "test:sample-model",
                        "temperature": 0.2,
                        "top_p": 0.9,
                        "max_output_tokens": 512,
                        "streaming": True,
                    }
                },
            }
        }
        runtime = LLMRuntime(config, event_callback=lambda kind, payload: events.append((kind, payload)))
        result = runtime.complete("coder", [{"role": "user", "content": "say hello"}])
        assert result.content == "hello world"
        assert result.reasoning == "Checking "
        assert result.usage["total_tokens"] == 5
        assert runtime.retry_count == 1
        assert runtime.call_count == 2
        assert any(kind == "llm_retry" for kind, _ in events)
        assert any(kind == "llm_reasoning_delta" for kind, _ in events)
        assert runtime.total_usage["prompt_tokens"] == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_dotted_parameter_mapping():
    config = {
        "llm": {
            "default_provider": "test",
            # This test exercises raw, legacy profile mapping; role policy is disabled.
            "effort_policy": {"enabled": False},
            "providers": {"test": {"base_url": "http://localhost/v1", "parameter_map": {"max_output_tokens": "max_tokens", "thinking_enabled": "chat_template_kwargs.enable_thinking", "thinking_budget": "chat_template_kwargs.budget"}}},
            "agents": {"coder": {"model": "test:model", "max_output_tokens": 123, "thinking_enabled": True, "thinking_budget": 1234}},
        }
    }
    runtime = LLMRuntime(config)
    provider, model = runtime._parse_model(runtime._profile("coder"), "test")
    payload = runtime._build_payload(provider, model, runtime._profile("coder"), [{"role": "user", "content": "hi"}], True)
    assert payload["chat_template_kwargs"] == {"enable_thinking": True, "budget": 1234}
    assert payload["max_tokens"] == 123


def test_role_aware_effort_policy_overrides_legacy_uniform_budgets():
    config = {
        "llm": {
            "default_provider": "test",
            "providers": {"test": {"base_url": "http://localhost/v1", "parameter_map": {}}},
            "agents": {
                "planner": {"model": "test:model", "thinking_enabled": True, "thinking_budget": 4096, "max_output_tokens": 8192},
                "coder": {"model": "test:model", "thinking_enabled": True, "thinking_budget": 4096, "max_output_tokens": 12000},
                "critic": {"model": "test:model", "thinking_enabled": True, "thinking_budget": 3072, "max_output_tokens": 6000},
            },
        }
    }
    runtime = LLMRuntime(config)
    planner = runtime._profile("planner")
    coder = runtime._profile("coder")
    critic = runtime._profile("critic")

    assert planner["thinking_enabled"] is True
    assert planner["thinking_budget"] == 2048
    assert coder["thinking_enabled"] is False
    assert "thinking_budget" not in coder
    assert critic["thinking_enabled"] is False
    assert critic["max_output_tokens"] == 3200


def test_effort_policy_allows_per_agent_override_and_opt_out():
    config = {
        "llm": {
            "effort_policy": {"enabled": True, "agents": {"coder": {"thinking_enabled": True, "thinking_budget": 256}}},
            "providers": {"test": {"base_url": "http://localhost/v1", "parameter_map": {}}},
            "agents": {"coder": {"model": "test:model", "thinking_enabled": True, "thinking_budget": 4096, "max_output_tokens": 12000}},
        }
    }
    runtime = LLMRuntime(config)
    assert runtime._profile("coder")["thinking_enabled"] is True
    assert runtime._profile("coder")["thinking_budget"] == 256

    config["llm"]["effort_policy"] = {"enabled": False}
    legacy_runtime = LLMRuntime(config)
    assert legacy_runtime._profile("coder")["thinking_budget"] == 4096
