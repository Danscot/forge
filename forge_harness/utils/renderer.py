from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


class RenderError(RuntimeError):
    pass


class PlaywrightRenderer:
    def __init__(self, project_root: Path, config: dict[str, Any], logger: logging.Logger | None = None):
        self.project_root = project_root
        self.config = config
        self.logger = logger or logging.getLogger("forge.renderer")

    def capture(self, base_url: str, route: str, screenshot_path: Path, test_plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError as exc:
            raise RenderError("Playwright is not installed. Install with `pip install -e '.[render]'` and run `playwright install chromium`.") from exc
        project_cfg = self.config.get("project", {})
        viewport = project_cfg.get("browser_viewport", {}) or {}
        width, height = int(viewport.get("width", 390)), int(viewport.get("height", 844))
        scale = float(viewport.get("device_scale_factor", 2))
        timeout_ms = int(project_cfg.get("render_timeout_ms", 45000))
        destination = urljoin(base_url.rstrip("/") + "/", route.lstrip("/"))
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page_errors: list[str] = []
        console_errors: list[str] = []
        failed_requests: list[str] = []
        test_results: list[dict[str, Any]] = []
        started = time.monotonic()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=scale, is_mobile=True, has_touch=True)
                page = context.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)[:800]))
                page.on("console", lambda msg: console_errors.append(msg.text[:800]) if msg.type == "error" else None)
                page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url}: {req.failure}"[:800]))
                try:
                    response = page.goto(destination, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 12000))
                    except PlaywrightTimeoutError:
                        # Persistent polling/web sockets should not block a screenshot.
                        pass
                    try:
                        page.evaluate("() => document.fonts && document.fonts.ready")
                    except Exception:
                        pass
                    page.wait_for_timeout(250)
                    title = page.title()
                    body_text = page.locator("body").inner_text(timeout=5000)[:6000]
                    element_counts = {
                        "buttons": page.locator("button, [role=button]").count(),
                        "links": page.locator("a[href]").count(),
                        "inputs": page.locator("input, textarea, select").count(),
                        "images": page.locator("img").count(),
                    }
                    full_page = bool(project_cfg.get("screenshot_full_page", False))
                    # Run interaction tests against the live route before capturing the
                    # evidence screenshot. A screenshot taken before clicks/navigation
                    # shows the default screen and makes route-specific assertions fail.
                    for step in test_plan or []:
                        test_results.append(self._run_test_step(page, step, timeout_ms))
                    page.screenshot(path=str(screenshot_path), full_page=full_page, animations="disabled", timeout=timeout_ms)
                    status_code = response.status if response else None
                finally:
                    browser.close()
        except Exception as exc:
            raise RenderError(f"Playwright render failed for {destination}: {exc}") from exc
        duration = time.monotonic() - started
        return {
            "url": destination,
            "screenshot_path": str(screenshot_path),
            "duration_s": round(duration, 3),
            "http_status": status_code,
            "title": title,
            "body_preview": body_text,
            "element_counts": element_counts,
            "page_errors": page_errors,
            "console_errors": console_errors[:100],
            "failed_requests": failed_requests[:100],
            "test_results": test_results,
            "functional_pass": status_code is not None and status_code < 400 and not page_errors and all(test.get("passed", False) for test in test_results),
        }

    @staticmethod
    def _test_locator(page: Any, selector: str) -> Any:
        """Resolve browser selectors emitted for React Native testIDs.

        React Native Web commonly exposes ``testID="home-heading"`` as
        ``data-testid="home-heading"`` rather than ``id="home-heading"``.
        Planner-generated checks historically used CSS ID selectors (``#home-heading``),
        so try the requested selector first and fall back to the matching testID when
        the requested ID does not exist. Other CSS selectors are left untouched.
        """
        locator = page.locator(selector)
        match = re.fullmatch(r"#([A-Za-z_][A-Za-z0-9_-]*)", selector)
        if not match:
            return locator
        try:
            if locator.count() > 0 and locator.first.is_visible():
                return locator
            test_id = match.group(1).replace('\\', '\\\\').replace('"', '\\"')
            test_id_locator = page.locator(f'[data-testid="{test_id}"]')
            if test_id_locator.count() > 0:
                return test_id_locator
        except Exception:
            # Keep the original selector so Playwright returns its usual actionable
            # timeout/error if count inspection itself is unavailable.
            return locator
        return locator

    @staticmethod
    def _run_test_step(page: Any, step: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        action = str(step.get("action", "")).strip()
        selector = str(step.get("selector", "")).strip()
        value = str(step.get("value", ""))
        description = str(step.get("description", f"{action} {selector}"))
        try:
            if action == "assert_visible":
                locator = PlaywrightRenderer._test_locator(page, selector)
                locator.first.wait_for(state="visible", timeout=min(timeout_ms, 7000))
            elif action == "click":
                PlaywrightRenderer._test_locator(page, selector).first.click(timeout=min(timeout_ms, 7000))
            elif action == "fill":
                PlaywrightRenderer._test_locator(page, selector).first.fill(value, timeout=min(timeout_ms, 7000))
            elif action == "assert_text":
                locator = PlaywrightRenderer._test_locator(page, selector)
                locator.first.wait_for(state="visible", timeout=min(timeout_ms, 7000))
                if value not in locator.first.inner_text(timeout=3000):
                    raise AssertionError(f"Expected text {value!r} not found")
            elif action == "navigate":
                page.goto(urljoin(page.url.rstrip("/") + "/", value.lstrip("/")), wait_until="domcontentloaded", timeout=timeout_ms)
            else:
                return {"action": action, "description": description, "passed": False, "error": f"Unsupported test action: {action}"}
            return {"action": action, "description": description, "passed": True}
        except Exception as exc:
            return {"action": action, "description": description, "passed": False, "error": str(exc)[:1000]}
