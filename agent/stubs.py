"""agent/stubs.py

Temporary stand-in implementations for security policy, auditing, and DOM sanitization
until Members 3 and 4 integrate the full modules.
"""

from __future__ import annotations

import inspect
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit

# Ensure project root is in sys.path when running this module directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import SUPPLIER_URLS
from shared.schemas import AuditEvent, BrowserAction, DOMElement, PolicyVerdict

_APPROVAL_KEYWORDS = ("pay", "place order", "confirm order")
_INJECTION_PHRASES = ("ignore previous instructions", "ignore all previous")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)
_ALLOWED_ORIGINS = {
    (parsed.scheme.lower(), parsed.netloc.lower())
    for parsed in (urlsplit(url) for url in SUPPLIER_URLS.values())
    if parsed.scheme and parsed.netloc
}


def _is_allowed_supplier_url(url: str | None) -> bool:
    """Return True if *url* uses the scheme and host:port of an allowed supplier."""
    if not url or not url.strip():
        return False
    parsed = urlsplit(url.strip())
    return (parsed.scheme.lower(), parsed.netloc.lower()) in _ALLOWED_ORIGINS


def fake_policy(action: BrowserAction, target_text: str | None = None) -> PolicyVerdict:
    """Stub policy check for browser actions.

    - Returns 'blocked' for 'navigate' actions to URLs outside SUPPLIER_URLS.
    - Returns 'requires_approval' for 'click' actions matching 'pay',
      'place order', or 'confirm order'.
    - Returns 'safe' otherwise.
    """
    if action.action_type == "navigate":
        if not _is_allowed_supplier_url(action.url):
            return PolicyVerdict(
                allowed=False,
                risk_level="blocked",
                rule_triggered="stub_blocked_url",
                message=f"Navigation to '{action.url}' is outside SUPPLIER_URLS.",
            )

    if action.action_type == "click":
        haystack = " ".join(
            part
            for part in (target_text, action.selector)
            if part
        ).lower()
        for keyword in _APPROVAL_KEYWORDS:
            if keyword in haystack:
                return PolicyVerdict(
                    allowed=False,
                    risk_level="requires_approval",
                    rule_triggered="stub_payment_approval",
                    message=f"Click action requires approval (matched '{keyword}').",
                )

    return PolicyVerdict(
        allowed=True,
        risk_level="safe",
        rule_triggered="none",
        message="Action allowed by stub policy.",
    )


def fake_audit(event: AuditEvent) -> None:
    """Stub audit logger that prints the serialized AuditEvent JSON."""
    print(event.model_dump_json())


def _scan_text_for_flags(text: str, source: str) -> list[str]:
    """Scan a text string for prompt-injection phrases and off-list URLs."""
    if not text:
        return []
    normalized_text = " ".join(text.lower().split())
    found: list[str] = []

    for phrase in _INJECTION_PHRASES:
        if phrase in normalized_text:
            found.append(f"{source} contains injection phrase '{phrase}'.")

    for match in _URL_PATTERN.findall(text):
        candidate_url = match.rstrip(".,;:!?")
        if not _is_allowed_supplier_url(candidate_url):
            found.append(f"{source} contains URL outside SUPPLIER_URLS: {candidate_url}")

    return found


def fake_sanitize(
    elements: list[DOMElement],
    page_text: str = "",
) -> tuple[list[DOMElement], list[str]]:
    """Stub DOM sanitizer scanning elements and page_text for injections or off-list URLs."""
    clean_elements: list[DOMElement] = []
    flags: list[str] = []

    for element in elements:
        element_flags = _scan_text_for_flags(
            element.text,
            f"Element {element.id} ({element.selector})",
        )
        if element_flags:
            flags.extend(element_flags)
        else:
            clean_elements.append(element)

    if page_text:
        flags.extend(_scan_text_for_flags(page_text, "Page text"))

    return clean_elements, flags


class FakeKillSwitch:
    """Stub kill switch that records trigger state and aborts the browser."""

    def __init__(self) -> None:
        self.triggered: bool = False

    async def abort(self, browser: Any) -> None:
        """Mark the kill switch as triggered and close the browser if provided."""
        self.triggered = True
        if browser is None:
            return
        close_fn = getattr(browser, "close", None)
        if callable(close_fn):
            result = close_fn()
            if inspect.isawaitable(result):
                await result
