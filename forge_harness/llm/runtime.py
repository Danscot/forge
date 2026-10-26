from __future__ import annotations

import base64
import json
import logging
import mimetypes
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, fatal: bool = False, status_code: int | None = None, provider: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.fatal = fatal
        self.status_code = status_code
        self.provider = provider


@dataclass
class LLMResult:
    content: str
    reasoning: str = ""
    provider: str = ""
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0
    attempts: int = 1
    finish_reason: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[str, dict[str, Any]], None]


def _set_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    bits = [part for part in dotted_key.split(".") if part]
    if not bits:
        return
    current = target
    for bit in bits[:-1]:
        existing = current.get(bit)
        if not isinstance(existing, dict):
            existing = {}
            current[bit] = existing
        current = existing
    current[bits[-1]] = value


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "".join(chunks)
    return ""


def _usage(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
        "thinking_tokens": ("thinking_tokens", "reasoning_tokens"),
    }
    result: dict[str, int] = {}
    for canonical, keys in aliases.items():
        for key in keys:
            if payload.get(key) is not None:
                try:
                    result[canonical] = int(payload[key])
                    break
                except (TypeError, ValueError):
                    pass
    if "total_tokens" not in result and ("prompt_tokens" in result or "completion_tokens" in result):
        result["total_tokens"] = result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
    return result


def _image_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class LLMRuntime:
    """Centralized OpenAI-compatible chat-completions runtime.

    All model-related tunables live in YAML profiles. Provider-specific parameters are
    mapped via llm.providers.<provider>.parameter_map, including dotted/nested fields.
    """

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None, event_callback: EventCallback | None = None):
        self.config = config
        self.llm_cfg = config.get("llm", {})
        self.logger = logger or logging.getLogger("forge.llm")
        self.event_callback = event_callback
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_usage: dict[str, int] = {}
        self.call_count = 0
        self.retry_count = 0

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.event_callback:
            try:
                self.event_callback(kind, payload)
            except Exception:
                self.logger.debug("LLM event callback failed", exc_info=True)

    def _profile(self, agent: str) -> dict[str, Any]:
        profiles = self.llm_cfg.get("agents", {})
        profile = profiles.get(agent)
        if not profile:
            raise LLMError(f"No model profile configured for agent '{agent}'", fatal=True)
        merged = dict(profile)
        merged.setdefault("streaming", self.llm_cfg.get("streaming", True))
        merged.setdefault("timeout_s", self.llm_cfg.get("request_timeout_s", 120))
        return merged

    @staticmethod
    def _parse_model(profile: dict[str, Any], default_provider: str) -> tuple[str, str]:
        raw = str(profile.get("model", "")).strip()
        if not raw:
            provider = str(profile.get("provider") or default_provider)
            model = str(profile.get("model_id") or "").strip()
        elif ":" in raw:
            provider, model = raw.split(":", 1)
        else:
            provider, model = str(profile.get("provider") or default_provider), raw
        if not provider or not model:
            raise LLMError("Agent model must be configured as 'provider:model' or provider + model_id", fatal=True)
        return provider, model

    def _candidate_profiles(self, agent: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = [profile]
        fallbacks = profile.get("fallback_models", []) or []
        global_fallbacks = self.llm_cfg.get("fallback_providers", []) or []
        for fallback in [*fallbacks, *global_fallbacks]:
            item = dict(profile)
            if isinstance(fallback, str):
                if ":" in fallback:
                    item["model"] = fallback
                else:
                    # A bare provider name uses the same configured model ID if possible.
                    _, model = self._parse_model(profile, str(self.llm_cfg.get("default_provider", "")))
                    item["model"] = f"{fallback}:{model}"
            elif isinstance(fallback, dict):
                item.update(fallback)
            else:
                continue
            candidates.append(item)
        # De-duplicate in order.
        result = []
        seen = set()
        for candidate in candidates:
            try:
                key = self._parse_model(candidate, str(self.llm_cfg.get("default_provider", "")))
            except LLMError:
                continue
            if key not in seen:
                result.append(candidate)
                seen.add(key)
        return result

    def _build_payload(self, provider_name: str, model_name: str, profile: dict[str, Any], messages: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
        provider_cfg = self.llm_cfg.get("providers", {}).get(provider_name)
        if not isinstance(provider_cfg, dict):
            raise LLMError(f"Provider '{provider_name}' is not configured under llm.providers", fatal=True)
        payload = _deep_merge_dicts({}, provider_cfg.get("static_request_params", {}) or {})
        payload["model"] = model_name
        payload["messages"] = messages
        parameter_map = provider_cfg.get("parameter_map", {}) or {}
        canonical_values = {
            "temperature": profile.get("temperature"),
            "top_p": profile.get("top_p"),
            "max_output_tokens": profile.get("max_output_tokens"),
            "stream": stream,
            "thinking_enabled": profile.get("thinking_enabled"),
            "thinking_budget": profile.get("thinking_budget"),
        }
        for name, value in canonical_values.items():
            if value is None:
                continue
            if name.startswith("thinking_") and name not in parameter_map:
                self._emit("parameter_not_mapped", provider=provider_name, parameter=name)
                continue
            target = parameter_map.get(name, name)
            if target is None or target == "":
                continue
            if isinstance(target, str) and "." in target:
                _set_dotted(payload, target, value)
            else:
                payload[str(target)] = value
        if not stream:
            payload.pop("stream_options", None)
        custom = profile.get("request_params", {}) or {}
        if not isinstance(custom, dict):
            raise LLMError("Agent request_params must be a mapping", fatal=True)
        payload = _deep_merge_dicts(payload, custom)
        return payload

    def _messages_with_images(self, messages: list[dict[str, Any]], image_paths: list[str | Path] | None) -> list[dict[str, Any]]:
        copied = [dict(message) for message in messages]
        paths = [Path(p) for p in (image_paths or []) if Path(p).is_file()]
        if not paths:
            return copied
        max_bytes = int(self.config.get("project", {}).get("max_image_bytes", 7_000_000))
        blocks = []
        max_count = max(1, int(self.llm_cfg.get("max_images_per_request", 6)))
        max_total_bytes = max_bytes if self.llm_cfg.get("max_total_image_bytes") is None else max(1, int(self.llm_cfg.get("max_total_image_bytes", max_bytes)))
        total_bytes = 0
        for path in paths:
            size = path.stat().st_size
            if size > max_bytes:
                self._emit("image_skipped", path=str(path), reason="file_exceeds_max_image_bytes", size=size)
                continue
            if len(blocks) >= max_count:
                self._emit("image_skipped", path=str(path), reason="max_images_per_request_reached", size=size)
                continue
            if total_bytes + size > max_total_bytes:
                self._emit("image_skipped", path=str(path), reason="max_total_image_bytes_exceeded", size=size)
                continue
            blocks.append({"type": "image_url", "image_url": {"url": _image_data_uri(path)}})
            total_bytes += size
        if not blocks:
            return copied
        for message in reversed(copied):
            if message.get("role") == "user":
                content = message.get("content", "")
                message["content"] = ([{"type": "text", "text": content}] if isinstance(content, str) else list(content)) + blocks
                break
        return copied

    def complete(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        *,
        image_paths: list[str | Path] | None = None,
        on_event: EventCallback | None = None,
        profile_override: dict[str, Any] | None = None,
    ) -> LLMResult:
        saved_callback = self.event_callback
        if on_event:
            self.event_callback = on_event
        try:
            profile = self._profile(agent)
            if profile_override:
                profile.update(profile_override)
            all_messages = self._messages_with_images(messages, image_paths)
            candidates = self._candidate_profiles(agent, profile)
            errors: list[str] = []
            failures: list[LLMError] = []
            for candidate_index, candidate in enumerate(candidates):
                provider, model = self._parse_model(candidate, str(self.llm_cfg.get("default_provider", "")))
                try:
                    result = self._call_candidate(agent, provider, model, candidate, all_messages)
                    if candidate_index:
                        self.logger.warning("LLM fallback provider succeeded", extra={"agent": agent, "provider": provider, "model": model})
                    return result
                except LLMError as exc:
                    failures.append(exc)
                    errors.append(f"{provider}:{model}: {exc}")
                    self.logger.warning("LLM candidate failed", extra={"agent": agent, "provider": provider, "model": model, "retryable": exc.retryable, "fatal": exc.fatal, "status_code": exc.status_code})
                    self._emit("provider_failed", agent=agent, provider=provider, model=model, error=str(exc), status_code=exc.status_code)
                    # Try a configured fallback after a terminal error or an exhausted retry budget.
                    continue
            raise LLMError(
                "All configured LLM candidates failed. " + " | ".join(errors),
                retryable=any(item.retryable for item in failures),
                fatal=bool(failures) and all(item.fatal for item in failures),
                status_code=next((item.status_code for item in reversed(failures) if item.status_code is not None), None),
            )
        finally:
            self.event_callback = saved_callback

    def _call_candidate(self, agent: str, provider: str, model: str, profile: dict[str, Any], messages: list[dict[str, Any]]) -> LLMResult:
        provider_cfg = self.llm_cfg.get("providers", {}).get(provider)
        if not isinstance(provider_cfg, dict):
            raise LLMError(f"Provider '{provider}' is not configured", fatal=True)
        key_env = str(provider_cfg.get("api_key_env", ""))
        import os
        api_key = os.environ.get(key_env, "").strip() if key_env else ""
        if not api_key:
            raise LLMError(f"Missing API key environment variable {key_env!r} for provider '{provider}'", fatal=True, provider=provider)
        base_url = str(provider_cfg.get("base_url", "")).rstrip("/")
        if not base_url:
            raise LLMError(f"Provider '{provider}' has no base_url", fatal=True, provider=provider)
        url = f"{base_url}/chat/completions"
        stream = bool(profile.get("streaming", self.llm_cfg.get("streaming", True)))
        payload = self._build_payload(provider, model, profile, messages, stream)
        if bool(self.llm_cfg.get("log_payloads", False)) or bool(self.config.get("logging", {}).get("capture_payloads", False)):
            limit = max(500, int(self.llm_cfg.get("log_payload_char_limit", 5000)))
            request_preview = json.dumps(payload, ensure_ascii=False, default=str)
            request_preview = re.sub(r"data:[^;]+;base64,[A-Za-z0-9+/=]+", "[IMAGE_DATA_OMITTED]", request_preview)
            self.logger.info("LLM request payload capture", extra={"agent": agent, "provider": provider, "model": model, "payload_preview": request_preview[:limit]})
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        headers.update(provider_cfg.get("extra_headers", {}) or {})
        retries = max(0, int(self.llm_cfg.get("max_retries", 3)))
        initial = max(0.0, float(self.llm_cfg.get("initial_backoff_s", 1.0)))
        max_backoff = max(initial, float(self.llm_cfg.get("max_backoff_s", 25.0)))
        multiplier = max(1.0, float(self.llm_cfg.get("backoff_multiplier", 2.0)))
        jitter = min(1.0, max(0.0, float(self.llm_cfg.get("jitter_fraction", 0.25))))
        timeout_s = float(profile.get("timeout_s", self.llm_cfg.get("request_timeout_s", 120)))
        connect_timeout = float(self.llm_cfg.get("connect_timeout_s", 20))
        timeout = httpx.Timeout(timeout_s, connect=connect_timeout)
        started = time.monotonic()
        last_error: LLMError | None = None
        for attempt in range(1, retries + 2):
            self.call_count += 1
            self._emit("llm_request_start", agent=agent, provider=provider, model=model, attempt=attempt, max_attempts=retries + 1, streaming=stream, context_window_tokens=profile.get("context_window_tokens"))
            self.logger.info("LLM request started", extra={"agent": agent, "provider": provider, "model": model, "attempt": attempt, "streaming": stream})
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    with client.stream("POST", url, headers=headers, json=payload) as response:
                        if response.status_code >= 400:
                            body = response.read().decode("utf-8", errors="replace")
                            message = self._extract_error(body) or body[:1400] or response.reason_phrase
                            retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
                            if response.status_code in {401, 403}:
                                retryable = False
                            terminal_client_error = 400 <= response.status_code < 500 and not retryable
                            error = LLMError(f"HTTP {response.status_code}: {message}", retryable=retryable, fatal=terminal_client_error, status_code=response.status_code, provider=provider)
                            if response.status_code == 429:
                                retry_after = response.headers.get("Retry-After")
                                if retry_after:
                                    try:
                                        setattr(error, "retry_after", float(retry_after))
                                    except (TypeError, ValueError):
                                        pass
                            raise error
                        if stream:
                            result = self._consume_sse(response.iter_lines(), agent, provider, model, attempt, started)
                        else:
                            try:
                                data = json.loads(response.read().decode("utf-8"))
                            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                                raise LLMError(f"Provider returned invalid JSON: {exc}", retryable=True, provider=provider) from exc
                            result = self._consume_nonstream(data, agent, provider, model, attempt, started)
                self._record_usage(result.usage)
                self._emit("llm_request_done", agent=agent, provider=provider, model=model, attempt=attempt, duration_s=round(result.duration_s, 3), usage=result.usage, finish_reason=result.finish_reason)
                self.logger.info("LLM request completed", extra={"agent": agent, "provider": provider, "model": model, "attempt": attempt, "duration_s": round(result.duration_s, 3), "usage": result.usage, "finish_reason": result.finish_reason})
                if bool(self.llm_cfg.get("log_payloads", False)) or bool(self.config.get("logging", {}).get("capture_payloads", False)):
                    limit = max(500, int(self.llm_cfg.get("log_payload_char_limit", 5000)))
                    self.logger.info("LLM response payload capture", extra={"agent": agent, "provider": provider, "model": model, "content_preview": result.content[:limit], "reasoning_preview": result.reasoning[:min(limit, 2000)], "usage": result.usage})
                return result
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt > retries:
                    break
                self.retry_count += 1
                retry_after = getattr(exc, "retry_after", None)
                delay = min(max_backoff, initial * (multiplier ** (attempt - 1)))
                delay *= random.uniform(1 - jitter, 1 + jitter) if jitter else 1
                delay = min(max_backoff, delay)
                if retry_after is not None:
                    delay = max(delay, min(max_backoff, float(retry_after)))
                self.logger.warning("Retrying LLM request", extra={"agent": agent, "provider": provider, "model": model, "attempt": attempt, "next_attempt": attempt + 1, "delay_s": round(delay, 3), "error": str(exc)})
                self._emit("llm_retry", agent=agent, provider=provider, model=model, attempt=attempt, next_attempt=attempt + 1, delay_s=round(delay, 2), error=str(exc))
                time.sleep(delay)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = LLMError(f"Network/timeout error: {exc.__class__.__name__}: {exc}", retryable=True, provider=provider)
                if attempt > retries:
                    break
                self.retry_count += 1
                delay = min(max_backoff, initial * (multiplier ** (attempt - 1)))
                delay *= random.uniform(1 - jitter, 1 + jitter) if jitter else 1
                delay = min(max_backoff, delay)
                self.logger.warning("Retrying transient LLM network failure", extra={"agent": agent, "provider": provider, "model": model, "attempt": attempt, "next_attempt": attempt + 1, "delay_s": round(delay, 3), "error": str(exc)})
                self._emit("llm_retry", agent=agent, provider=provider, model=model, attempt=attempt, next_attempt=attempt + 1, delay_s=round(delay, 2), error=str(last_error))
                time.sleep(delay)
            except httpx.HTTPError as exc:
                last_error = LLMError(f"HTTP client error: {exc}", retryable=True, provider=provider)
                if attempt > retries:
                    break
                self.retry_count += 1
                delay = min(max_backoff, initial * (multiplier ** (attempt - 1)))
                delay *= random.uniform(1 - jitter, 1 + jitter) if jitter else 1
                delay = min(max_backoff, delay)
                self._emit("llm_retry", agent=agent, provider=provider, model=model, attempt=attempt, next_attempt=attempt + 1, delay_s=round(delay, 2), error=str(last_error))
                time.sleep(delay)
        duration = time.monotonic() - started
        self._emit("llm_candidate_exhausted", agent=agent, provider=provider, model=model, duration_s=round(duration, 3), error=str(last_error))
        raise last_error or LLMError(f"Unknown LLM error for {provider}:{model}")

    @staticmethod
    def _extract_error(body: str) -> str:
        try:
            payload = json.loads(body)
            error = payload.get("error", payload)
            if isinstance(error, dict):
                return str(error.get("message") or error.get("detail") or error.get("code") or "")
            return str(error)
        except Exception:
            return ""

    def _consume_sse(self, lines: Any, agent: str, provider: str, model: str, attempt: int, started: float) -> LLMResult:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        raw_last: dict[str, Any] = {}
        provider_cfg = self.llm_cfg.get("providers", {}).get(provider, {})
        reasoning_fields = provider_cfg.get("reasoning_fields", ["reasoning_content", "reasoning", "thinking"])
        for line in lines:
            if not line:
                continue
            if line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data_text = line[5:].strip()
            if data_text == "[DONE]":
                break
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                self._emit("stream_malformed_chunk", agent=agent, provider=provider, preview=data_text[:180])
                continue
            raw_last = data
            current_usage = _usage(data.get("usage"))
            if current_usage:
                usage.update(current_usage)
            for choice in data.get("choices", []) or []:
                delta = choice.get("delta") or {}
                for field in reasoning_fields:
                    value = delta.get(field)
                    if isinstance(value, str) and value:
                        reasoning_parts.append(value)
                        if self.llm_cfg.get("display_reasoning_stream", False):
                            self._emit("llm_reasoning_delta", agent=agent, provider=provider, model=model, text=value)
                        break
                text_chunk = _as_text(delta.get("content"))
                if text_chunk:
                    content_parts.append(text_chunk)
                    self._emit("llm_text_delta", agent=agent, provider=provider, model=model, text=text_chunk)
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
        return LLMResult(
            content="".join(content_parts), reasoning="".join(reasoning_parts), provider=provider, model=model,
            usage=usage, duration_s=time.monotonic() - started, attempts=attempt, finish_reason=finish_reason, raw_response=raw_last,
        )

    def _consume_nonstream(self, data: dict[str, Any], agent: str, provider: str, model: str, attempt: int, started: float) -> LLMResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"Provider response did not contain choices: {json.dumps(data)[:500]}", retryable=True, provider=provider)
        message = choices[0].get("message") or {}
        content = _as_text(message.get("content"))
        reasoning = ""
        for field in self.llm_cfg.get("providers", {}).get(provider, {}).get("reasoning_fields", ["reasoning_content", "reasoning", "thinking"]):
            if isinstance(message.get(field), str):
                reasoning = message[field]
                break
        if content:
            self._emit("llm_text_delta", agent=agent, provider=provider, model=model, text=content)
        if reasoning and self.llm_cfg.get("display_reasoning_stream", False):
            self._emit("llm_reasoning_delta", agent=agent, provider=provider, model=model, text=reasoning)
        return LLMResult(
            content=content, reasoning=reasoning, provider=provider, model=model, usage=_usage(data.get("usage")),
            duration_s=time.monotonic() - started, attempts=attempt, finish_reason=choices[0].get("finish_reason"), raw_response=data,
        )

    def _record_usage(self, usage: dict[str, int]) -> None:
        if not usage:
            return
        self.last_usage = dict(usage)
        for key in self.total_usage:
            self.total_usage[key] += int(usage.get(key, 0))
