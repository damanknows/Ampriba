"""Thread-safe, one-way stop switch for a shopping session."""

from __future__ import annotations

import inspect
import json
from threading import Lock
from typing import TYPE_CHECKING

from shared.config import DEFAULT_MAX_BUDGET

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

    from agent.browser import BrowserManager
    from shared.schemas import BrowserAction


class KillSwitch:
    def __init__(self, max_budget: float = DEFAULT_MAX_BUDGET):
        if isinstance(max_budget, bool) or not isinstance(max_budget, (int, float)) or not (0 <= max_budget < float("inf")):
            raise ValueError("max_budget must be finite and non-negative")
        self._max_budget = max_budget
        self._lock = Lock()
        self._triggered = False
        self._reason: str | None = None
        self._last_action: str | None = None
        self._identical_actions = 0

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def trigger(self, reason: str = "manual") -> None:
        """Stop all subsequent actions, preserving the first reason."""
        with self._lock:
            if not self._triggered:
                self._triggered = True
                self._reason = reason or "manual"

    def _record_repeat_locked(self, action_key: str, reason: str) -> None:
        if action_key == self._last_action:
            self._identical_actions += 1
        else:
            self._last_action = action_key
            self._identical_actions = 1
        if self._identical_actions > 3:
            self._triggered = True
            self._reason = reason

    def check_budget(self, spent: float, cap: float) -> None:
        """Trigger if spent exceeds cap (or values are invalid)."""
        with self._lock:
            if self._triggered:
                return
            if (
                isinstance(spent, bool)
                or isinstance(cap, bool)
                or not isinstance(spent, (int, float))
                or not isinstance(cap, (int, float))
                or spent != spent
                or cap != cap
                or spent > cap
            ):
                self._triggered = True
                self._reason = "Budget exceeded"

    def check_repeats(self, action_key: str) -> None:
        """Trigger if the same action key is seen more than 3 times consecutively."""
        with self._lock:
            if not self._triggered:
                self._record_repeat_locked(action_key, "Repeated identical action circuit breaker")

    def record_action(self, action: BrowserAction, projected_total: float = 0) -> bool:
        """Check the projected total and halt on four consecutive identical actions."""
        with self._lock:
            if not self._triggered:
                if (
                    isinstance(projected_total, bool)
                    or not isinstance(projected_total, (int, float))
                    or not (0 <= projected_total <= self._max_budget)
                ):
                    self._triggered = True
                    self._reason = "Projected total is invalid or exceeds budget"
                elif getattr(action, "action_type", None) == "halt":
                    self._triggered = True
                    self._reason = action.reason or "Halt requested"
                else:
                    dump = getattr(action, "model_dump", None)
                    signature = json.dumps(
                        dump() if dump else repr(action), sort_keys=True, default=str
                    )
                    self._record_repeat_locked(signature, "Repeated identical action circuit breaker")
            return not self._triggered

    async def abort(self, browser: BrowserContext | Browser | BrowserManager | None) -> None:
        """Latch before closing the session on the browser's owning event loop."""
        self.trigger("Session aborted")
        if browser is None:
            return
        try:
            close = getattr(browser, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        except Exception:
            pass
        finally:
            context = getattr(browser, "context", None)
            if context is not None:
                # A context wrapper may not close its owner; ensure the session is gone.
                try:
                    result = context.close()
                    if inspect.isawaitable(result):
                        await result
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


_singleton_lock = Lock()
_kill_switch: KillSwitch | None = None


def get_kill_switch() -> KillSwitch:
    """Return the lazily-created shared KillSwitch instance."""
    global _kill_switch
    with _singleton_lock:
        if _kill_switch is None:
            _kill_switch = KillSwitch()
        return _kill_switch


def reset_kill_switch() -> None:
    """Reset the shared KillSwitch singleton for tests."""
    global _kill_switch
    with _singleton_lock:
        _kill_switch = None
