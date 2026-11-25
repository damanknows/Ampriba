"""agent/stubs.py

Temporary stand-in implementations for security policy, auditing, and DOM sanitization
until Members 3 and 4 integrate the full modules.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure project root is in sys.path when running this module directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.schemas import AuditEvent, BrowserAction, DOMElement, PolicyVerdict

_APPROVAL_KEYWORDS = ("pay", "place order", "confirm order")


def fake_policy(action: BrowserAction, target_text: str | None = None) -> PolicyVerdict:
    """Stub policy check for browser actions.

    Returns a 'safe' verdict for all actions except 'click' actions whose
    reason or target text contains 'pay', 'place order', or 'confirm order',
    which return 'requires_approval'.
    """
    if action.action_type == "click":
        haystack = " ".join(
            part
            for part in (action.reason, target_text, action.selector, action.value)
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


def fake_sanitize(elements: list[DOMElement]) -> tuple[list[DOMElement], list[str]]:
    """Stub DOM sanitizer that returns elements unchanged with no flags."""
    return elements, []
