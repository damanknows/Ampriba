"""agent/browser.py

Async Playwright Chromium browser wrapper using isolated ephemeral contexts.
Provides:
- BrowserManager: Manages browser lifecycle and ephemeral contexts
- extract_elements(page): Extracts visible interactive elements as DOMElement instances
- save_screenshot: Takes and saves step screenshots to disk
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

# Ensure project root is in sys.path when running this module directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import (
    Browser as PlaywrightBrowser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

from shared.schemas import DOMElement


EXTRACT_ELEMENTS_JS = """() => {
    const escapeCss = (str) => {
        if (window.CSS && window.CSS.escape) {
            return window.CSS.escape(str);
        }
        return str.replace(/([ #;?%&,.+*~\\':"!^$[\\]()=>|\\/@])/g, '\\\\$1');
    };

    function getCssSelector(el) {
        // 1. Unique ID
        if (el.id && typeof el.id === 'string' && el.id.trim()) {
            try {
                const idSel = '#' + escapeCss(el.id.trim());
                if (document.querySelectorAll(idSel).length === 1) {
                    return idSel;
                }
            } catch (e) {}
        }

        const tag = el.tagName.toLowerCase();

        // 2. Unique test attributes
        for (const attr of ['data-testid', 'data-test', 'data-id', 'data-cy']) {
            const val = el.getAttribute(attr);
            if (val) {
                try {
                    const sel = `[${attr}="${escapeCss(val)}"]`;
                    if (document.querySelectorAll(sel).length === 1) {
                        return sel;
                    }
                } catch (e) {}
            }
        }

        // 3. Unique name attribute
        if (el.name && typeof el.name === 'string' && el.name.trim()) {
            try {
                const sel = `${tag}[name="${escapeCss(el.name.trim())}"]`;
                if (document.querySelectorAll(sel).length === 1) {
                    return sel;
                }
            } catch (e) {}
        }

        // 4. Hierarchical path using nth-of-type
        const path = [];
        let curr = el;
        while (curr && curr.nodeType === Node.ELEMENT_NODE && curr.tagName.toLowerCase() !== 'html') {
            const curTag = curr.tagName.toLowerCase();
            if (curr.id && typeof curr.id === 'string' && curr.id.trim()) {
                try {
                    const idSel = '#' + escapeCss(curr.id.trim());
                    if (document.querySelectorAll(idSel).length === 1) {
                        path.unshift(idSel);
                        break;
                    }
                } catch (e) {}
            }

            const parent = curr.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(
                    (c) => c.tagName.toLowerCase() === curTag
                );
                if (siblings.length > 1) {
                    const index = siblings.indexOf(curr) + 1;
                    path.unshift(`${curTag}:nth-of-type(${index})`);
                } else {
                    path.unshift(curTag);
                }
            } else {
                path.unshift(curTag);
            }
            curr = parent;
        }
        return path.join(' > ');
    }

    function getElementText(el) {
        const tag = el.tagName.toLowerCase();

        // Check associated label
        let labelText = '';
        if (el.id) {
            try {
                const lbl = document.querySelector(`label[for="${escapeCss(el.id)}"]`);
                if (lbl) {
                    labelText = lbl.innerText || lbl.textContent || '';
                }
            } catch (e) {}
        }
        if (!labelText) {
            const parentLabel = el.closest('label');
            if (parentLabel) {
                labelText = parentLabel.innerText || parentLabel.textContent || '';
            }
        }

        if (tag === 'button' || (tag === 'input' && ['button', 'submit', 'reset'].includes(el.type))) {
            const direct = el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '';
            return direct.trim() || labelText.trim();
        }

        if (tag === 'a') {
            const direct = el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '';
            return direct.trim();
        }

        if (tag === 'select') {
            const selectedText = el.selectedOptions && el.selectedOptions.length > 0 ? el.selectedOptions[0].text || el.selectedOptions[0].value || '' : '';
            const parts = [];
            if (labelText.trim()) parts.push(labelText.trim());
            if (selectedText.trim()) parts.push(selectedText.trim());
            if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label').trim());
            return parts.filter(Boolean).join(' | ');
        }

        if (tag === 'textarea' || tag === 'input') {
            const parts = [];
            if (labelText.trim()) parts.push(labelText.trim());
            const placeholder = el.getAttribute('placeholder');
            if (placeholder && placeholder.trim()) parts.push(placeholder.trim());
            if (el.value && el.value.trim() && el.type !== 'password') parts.push(el.value.trim());
            const aria = el.getAttribute('aria-label');
            if (aria && aria.trim() && !parts.includes(aria.trim())) parts.push(aria.trim());
            if (parts.length === 0 && el.name) parts.push(el.name);
            return parts.join(' | ');
        }

        // Generic fallback
        return (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    }

    const selectorQuery = 'input:not([type="hidden"]), button, a[href], select, textarea, [role="button"], [role="link"], [role="textbox"]';
    const candidates = Array.from(document.querySelectorAll(selectorQuery));
    const results = [];
    let idCounter = 1;

    for (const el of candidates) {
        // Visibility checks: check element and ancestors
        let isVisible = true;
        let currNode = el;
        while (currNode && currNode !== document.documentElement) {
            const style = window.getComputedStyle(currNode);
            if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                isVisible = false;
                break;
            }
            if (currNode.hasAttribute('hidden') || currNode.getAttribute('aria-hidden') === 'true') {
                isVisible = false;
                break;
            }
            currNode = currNode.parentElement;
        }

        if (!isVisible) {
            continue;
        }

        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) {
            continue;
        }

        const tag = el.tagName.toLowerCase();
        const rawText = getElementText(el);
        const text = rawText.replace(/\\s+/g, ' ').trim();
        const selector = getCssSelector(el);

        results.push({
            id: idCounter++,
            tag: tag,
            text: text,
            selector: selector
        });
    }

    return results;
}"""


async def extract_elements(page: Page) -> list[DOMElement]:
    """Extract visible interactive elements (inputs, buttons, links, selects, textareas)

    from the page and return them as a list of DOMElement objects with numeric IDs,
    tag, visible text, and CSS selector.
    """
    raw_elements: list[dict[str, Any]] = await page.evaluate(EXTRACT_ELEMENTS_JS)
    return [
        DOMElement(
            id=item["id"],
            tag=item["tag"],
            text=item["text"],
            selector=item["selector"],
        )
        for item in raw_elements
    ]


async def save_screenshot(
    page: Page,
    step_num: int | str = 0,
    name: str | None = None,
    directory: str | Path = "screenshots",
) -> str:
    """Take a screenshot of the current page and save it to disk.

    Returns the string path to the saved screenshot file.
    """
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{name}" if name else ""
    filename = f"step_{step_num}{suffix}.png"
    filepath = out_dir / filename
    await page.screenshot(path=str(filepath), full_page=False)
    return str(filepath.resolve())


class BrowserManager:
    """Async Playwright Chromium wrapper using isolated ephemeral contexts."""

    def __init__(
        self,
        headless: bool = True,
        screenshots_dir: str | Path = "screenshots",
        viewport: dict[str, int] | None = None,
        user_agent: str | None = None,
    ):
        self.headless = headless
        self.screenshots_dir = Path(screenshots_dir)
        self.viewport = viewport or {"width": 1280, "height": 800}
        self.user_agent = user_agent

        self.playwright: Playwright | None = None
        self.browser: PlaywrightBrowser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._last_elements: list[DOMElement] = []

    @property
    def last_elements(self) -> list[DOMElement]:
        """Return the most recently extracted elements."""
        return self._last_elements

    async def start(self) -> Page:
        """Start Playwright, launch Chromium, and create an isolated ephemeral context and page."""
        if self.playwright is None:
            self.playwright = await async_playwright().start()

        if self.browser is None:
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless
            )

        await self.new_ephemeral_context()
        if self.page is None:
            raise RuntimeError("Failed to initialize page in ephemeral context.")
        return self.page

    async def new_ephemeral_context(self, **kwargs: Any) -> BrowserContext:
        """Create a fresh, isolated ephemeral browser context.

        Existing context (if any) is closed to ensure clean isolation.
        """
        if self.browser is None:
            raise RuntimeError("Browser not started. Call start() first.")

        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None

        context_options: dict[str, Any] = {
            "viewport": self.viewport,
            "ignore_https_errors": True,
        }
        if self.user_agent:
            context_options["user_agent"] = self.user_agent
        context_options.update(kwargs)

        self.context = await self.browser.new_context(**context_options)
        self.page = await self.context.new_page()
        return self.context

    async def goto(self, url: str, **kwargs: Any) -> Response | None:
        """Navigate current page to a URL."""
        if self.page is None:
            raise RuntimeError("No active page. Call start() first.")
        return await self.page.goto(url, **kwargs)

    async def navigate(self, url: str, **kwargs: Any) -> Response | None:
        """Alias for goto."""
        return await self.goto(url, **kwargs)

    async def click(self, selector: str, **kwargs: Any) -> None:
        """Click element matching selector."""
        if self.page is None:
            raise RuntimeError("No active page. Call start() first.")
        await self.page.click(selector, **kwargs)

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        """Fill an input or textarea matching selector."""
        if self.page is None:
            raise RuntimeError("No active page. Call start() first.")
        await self.page.fill(selector, value, **kwargs)

    async def extract_elements(self, page: Page | None = None) -> list[DOMElement]:
        """Extract visible interactive elements from the current page (or supplied page)."""
        target_page = page or self.page
        if target_page is None:
            raise RuntimeError("No active page to extract elements from.")
        self._last_elements = await extract_elements(target_page)
        return self._last_elements

    async def save_screenshot(
        self,
        step_num: int | str = 0,
        name: str | None = None,
        page: Page | None = None,
    ) -> str:
        """Save a step screenshot and return the path."""
        target_page = page or self.page
        if target_page is None:
            raise RuntimeError("No active page to take screenshot from.")
        return await save_screenshot(
            target_page,
            step_num=step_num,
            name=name,
            directory=self.screenshots_dir,
        )

    async def take_screenshot(
        self,
        step_num: int | str = 0,
        name: str | None = None,
        page: Page | None = None,
    ) -> str:
        """Alias for save_screenshot."""
        return await self.save_screenshot(step_num=step_num, name=name, page=page)

    async def close(self) -> None:
        """Cleanly close page, ephemeral context, browser, and Playwright driver."""
        if self.page is not None:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None

        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None

        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


# Convenient aliases
BrowserWrapper = BrowserManager


if __name__ == "__main__":
    async def _test() -> None:
        print("Starting Playwright Chromium browser test...")
        async with BrowserManager(headless=True) as bm:
            print("Navigating to https://example.com...")
            await bm.goto("https://example.com")

            print("Extracting visible interactive elements...")
            elements = await bm.extract_elements()
            print(f"\nFound {len(elements)} visible interactive element(s):")
            for el in elements:
                print(f"  [ID {el.id}] <{el.tag}> text='{el.text}' selector='{el.selector}'")

            screenshot_path = await bm.save_screenshot(step_num=0, name="example_test")
            print(f"\nStep screenshot saved to: {screenshot_path}")
            print("\nBrowser test completed successfully!")

    asyncio.run(_test())
