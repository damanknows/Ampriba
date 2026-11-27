"""Thread-safe, one-way stop switch for a shopping session."""

from __future__ import annotations

import json
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

    from agent.browser import BrowserManager
    from shared.schemas import BrowserAction


class KillSwitch:
    def __init__(self, max_budget: float):
        if not isinstance(max_budget, (int, float)) or not (0 <= max_budget < float("inf")):
            raise ValueError("max_budget must be finite and non-negative")
        self._max_budget = max_budget
        self._lock = Lock()
        self._reason: str | None = None
        self._last_action: str | None = None
        self._identical_actions = 0

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def trigger(self, reason: str = "Manual stop") -> None:
        """Stop all subsequent actions, preserving the first reason."""
        with self._lock:
            if self._reason is None:
                self._reason = reason or "Manual stop"

    def record_action(self, action: BrowserAction, projected_total: float = 0) -> bool:
        """Check the projected total and halt on four consecutive identical actions."""
        with self._lock:
            if self._reason is None:
                if not isinstance(projected_total, (int, float)) or not (
                    0 <= projected_total <= self._max_budget
                ):
                    self._reason = "Projected total is invalid or exceeds budget"
                elif getattr(action, "action_type", None) == "halt":
                    self._reason = action.reason or "Halt requested"
                else:
                    dump = getattr(action, "model_dump", None)
                    signature = json.dumps(
                        dump() if dump else repr(action), sort_keys=True, default=str
                    )
                    if signature == self._last_action:
                        self._identical_actions += 1
                    else:
                        self._last_action = signature
                        self._identical_actions = 1
                    if self._identical_actions > 3:
                        self._reason = "Repeated identical action circuit breaker"
            return self._reason is None

    async def abort(self, browser: BrowserContext | Browser | BrowserManager) -> None:
        """Latch before closing the session on the browser's owning event loop."""
        self.trigger("Session aborted")
        try:
            close = getattr(browser, "close")
            await close()
        finally:
            context = getattr(browser, "context", None)
            if context is not None:
                # A context wrapper may not close its owner; ensure the session is gone.
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    browser.context = None
                except (AttributeError, TypeError):
                    pass
            for attribute in ("page", "_last_elements", "session_state"):
                value = getattr(browser, attribute, None)
                if isinstance(value, list):
                    value.clear()
                elif value is not None:
                    try:
                        setattr(browser, attribute, None)
                    except (AttributeError, TypeError):
                        pass
