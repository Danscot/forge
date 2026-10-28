from __future__ import annotations

from typing import Any

from forge_harness.utils.renderer import PlaywrightRenderer


class FakeLocator:
    def __init__(self, selector: str, visible: bool):
        self.selector = selector
        self.visible = visible
        self.first = self
        self.filled: list[str] = []
        self.clicked = False

    def count(self) -> int:
        return 1 if self.visible or self.selector.startswith("#") else 0

    def is_visible(self) -> bool:
        return self.visible

    def wait_for(self, **kwargs: Any) -> None:
        if not self.visible:
            raise TimeoutError(f"not visible: {self.selector}")

    def fill(self, value: str, **kwargs: Any) -> None:
        if not self.visible:
            raise TimeoutError(f"not visible: {self.selector}")
        self.filled.append(value)

    def click(self, **kwargs: Any) -> None:
        if not self.visible:
            raise TimeoutError(f"not visible: {self.selector}")
        self.clicked = True

    def inner_text(self, **kwargs: Any) -> str:
        return "Home"


class FakePage:
    def __init__(self, visible_test_ids: set[str]):
        self.visible_test_ids = visible_test_ids
        self.locators: dict[str, FakeLocator] = {}

    def locator(self, selector: str) -> FakeLocator:
        if selector not in self.locators:
            if selector.startswith('[data-testid="'):
                test_id = selector.split('"')[1]
                visible = test_id in self.visible_test_ids
            elif selector.startswith("#"):
                visible = False
            else:
                visible = selector in self.visible_test_ids
            self.locators[selector] = FakeLocator(selector, visible)
        return self.locators[selector]


def test_id_css_selector_falls_back_to_react_native_test_id_for_visibility():
    page = FakePage({"home-heading"})
    result = PlaywrightRenderer._run_test_step(
        page, {"action": "assert_visible", "selector": "#home-heading"}, timeout_ms=7000
    )

    assert result["passed"] is True
    assert '[data-testid="home-heading"]' in page.locators


def test_id_css_selector_falls_back_to_react_native_test_id_for_fill():
    page = FakePage({"search-input"})
    result = PlaywrightRenderer._run_test_step(
        page, {"action": "fill", "selector": "#search-input", "value": "react"}, timeout_ms=7000
    )

    assert result["passed"] is True
    assert page.locators['[data-testid="search-input"]'].filled == ["react"]


def test_non_id_css_selectors_are_not_rewritten():
    page = FakePage({".course-card"})
    result = PlaywrightRenderer._run_test_step(
        page, {"action": "assert_visible", "selector": ".course-card"}, timeout_ms=7000
    )

    assert result["passed"] is True
    assert set(page.locators) == {".course-card"}
