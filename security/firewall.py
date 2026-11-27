"""Action policy and Playwright routing for the isolated shopping context."""

import re
import urllib.parse

from playwright.async_api import BrowserContext, Page, Route, WebSocketRoute

from security.sanitizer import is_allowed_url, suspicious_content
from shared.schemas import BrowserAction, PolicyVerdict


_IRREVERSIBLE = re.compile(r"pay|place[\s_-]*order|confirm[\s_-]*order|delete|transfer|buy[\s_-]*now|purchase", re.I)
_CHALLENGE = re.compile(r"captcha|verify (?:that )?you(?: are|'re) human|not a robot", re.I)


def evaluate_action(action: BrowserAction, element_text: str | None = None) -> PolicyVerdict:
    """Check before execution, using the current target's label, not planner-supplied text.

    The caller must stop on a blocked verdict and obtain human authorization for
    the exact target and action before proceeding on requires_approval.
    """
    if action.action_type == "halt":
        return PolicyVerdict(allowed=True, risk_level="safe", rule_triggered="none",
                             message="Stop requested.")

    if (action.action_type == "navigate" or action.url is not None) and not (
        action.url and is_allowed_url(action.url)
    ):
        return PolicyVerdict(allowed=False, risk_level="blocked", rule_triggered="url_allowlist",
                             message="Destination is missing or outside the allowed origins.")

    fields = (element_text, action.reason, action.selector, action.value, action.url)
    for text in fields:
        if text and suspicious_content(text) is not None:
            return PolicyVerdict(allowed=False, risk_level="blocked", rule_triggered="suspicious_content",
                                 message="Suspicious content in the action or target.")

    if action.action_type == "click" and any(_IRREVERSIBLE.search(text) for text in fields if text):
        return PolicyVerdict(allowed=False, risk_level="requires_approval", rule_triggered="irreversible_click",
                             message="Human approval required for this irreversible action.")

    return PolicyVerdict(allowed=True, risk_level="safe", rule_triggered="none",
                         message="No policy rule triggered.")


async def detect_captcha(page: Page) -> PolicyVerdict:
    """Inspect loaded frames without interacting; fail closed if inspection fails.

    Call after navigation and before acting; a negative snapshot is not proof that
    a dynamically loaded or unrecognized challenge is absent.
    """
    try:
        for frame in page.frames:
            if _CHALLENGE.search(frame.url) or await frame.locator(
                '[id*="captcha" i], [class*="captcha" i], [name*="captcha" i], '
                'iframe[src*="captcha" i], iframe[title*="captcha" i], .cf-turnstile'
            ).count() or await frame.get_by_text(_CHALLENGE).count():
                return PolicyVerdict(allowed=False, risk_level="blocked", rule_triggered="captcha",
                                     message="Human verification detected; stop this session.")
    except Exception:
        return PolicyVerdict(allowed=False, risk_level="blocked", rule_triggered="captcha_detection_error",
                             message="Verification state could not be inspected; stop this session.")
    return PolicyVerdict(allowed=True, risk_level="safe", rule_triggered="none",
                         message="No CAPTCHA detected.")


async def install_firewall(context: BrowserContext | Page) -> None:
    """Install once per fresh context, before navigation, popups, or untrusted code.

    Accepts a BrowserContext (or a Page for backward compatibility, resolving
    to its context). Requires Playwright >= 1.48 and a context created with
    service_workers="block". Do not install competing routes that continue
    requests. Discard the context if installation fails. These routes govern
    browser HTTP(S) and WebSockets, not arbitrary browser networking; use OS
    egress controls for hard isolation. 3xx are followed only when the resolved
    Location is inside ALLOWED_ORIGINS; every hop is re-checked.
    """
    if isinstance(context, Page):
        context = context.context

    async def handle_request(route: Route) -> None:
        response = None
        try:
            request_url = route.request.url
            if not is_allowed_url(request_url):
                await route.abort("blockedbyclient")
                return

            response = await route.fetch(max_redirects=0)
            if 300 <= response.status < 400:
                location = response.headers.get("location")
                if location:
                    target_url = urllib.parse.urljoin(request_url, location)
                    if is_allowed_url(target_url):
                        await route.continue_()
                        return
                await route.abort("blockedbyclient")
                return

            await route.fulfill(response=response)
        except Exception:
            # A failed check or fetch must never fall back to native networking.
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
        finally:
            if response is not None:
                await response.dispose()

    async def block_socket(socket: WebSocketRoute) -> None:
        await socket.close(code=1008, reason="WebSockets are disabled")

    await context.route_web_socket(re.compile(r".*"), block_socket)
    await context.route("**/*", handle_request)


install_firewall_fn = install_firewall
