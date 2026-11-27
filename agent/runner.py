"""agent/runner.py

Unified shopping agent runner with dependency-injected security policy,
sanitization, auditing, network firewall, and kill switch hooks.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
import logging
import math
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.parse import quote_plus
import warnings

# Ensure project root is in sys.path when running this module directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError

from agent.browser import BrowserManager
from agent.planner import next_action
from agent.stubs import fake_sanitize
from shared.config import SUPPLIER_URLS
from shared.schemas import AuditEvent, BrowserAction, DOMElement, PolicyVerdict, ShoppingTask

MAX_STEPS = 40
MAX_ACTION_REPEATS = 3
LOGS_DIR = PROJECT_ROOT / "logs"
logger = logging.getLogger(__name__)

_PRICE_RUPEE_RE = re.compile(r"₹\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_PRICE_NUM_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")
_STOCK_RE = re.compile(r"(\d[\d,]*)\s*(?:in\s+stock|available)\b", re.IGNORECASE)
_CAPTCHA_TEXT_RE = re.compile(r"\bcaptcha\b", re.IGNORECASE)
_PINCODE_RE = re.compile(r"\b(\d{6})\b")
_PHONE_RE = re.compile(r"(\+?\d[\d\s()-]{8,14}\d)")


def parse_price(text: Any) -> float | None:
    """Parse a currency amount (handling ₹ and commas) from visible text."""
    if not isinstance(text, str) or not text:
        return None
    match = _PRICE_RUPEE_RE.search(text)
    if match is None:
        match = _PRICE_NUM_RE.search(text)
    if match is None:
        return None
    raw_num = match.group(1).replace(",", "")
    try:
        return float(raw_num)
    except ValueError:
        return None


def parse_stock(text: Any) -> int | None:
    """Parse available stock count from portal stock labels ('in stock' or 'available')."""
    if not isinstance(text, str) or not text:
        return None
    match = _STOCK_RE.search(text)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_shipping_address(shipping_address: str) -> dict[str, str]:
    """Extract shipping form fields from a freeform ShoppingTask.shipping_address string."""
    raw = shipping_address.strip() if isinstance(shipping_address, str) else ""
    pincode = "411001"
    phone = "9876543210"

    pin_match = _PINCODE_RE.search(raw)
    if pin_match:
        pincode = pin_match.group(1)
        raw = (raw[: pin_match.start()] + " " + raw[pin_match.end() :]).strip()

    phone_match = _PHONE_RE.search(raw)
    if phone_match:
        candidate_phone = re.sub(r"\s+", " ", phone_match.group(1)).strip()
        if re.fullmatch(r"[0-9+() -]{10,16}", candidate_phone):
            phone = candidate_phone
            raw = (raw[: phone_match.start()] + " " + raw[phone_match.end() :]).strip()

    parts = [
        re.sub(
            r"^(?:name|full name|phone|address|city|state|pin|pincode)\s*:\s*",
            "",
            part.strip(),
            flags=re.IGNORECASE,
        )
        for part in re.split(r"[,;\n]+", raw)
    ]
    parts = [p for p in parts if p]

    full_name = "Procurement Team"
    address = "42 Market Street"
    city = "Pune"
    state = "Maharashtra"

    if len(parts) >= 4:
        full_name = parts[0]
        address = ", ".join(parts[1:-2])
        city = parts[-2]
        state = parts[-1]
    elif len(parts) == 3:
        address, city, state = parts
    elif len(parts) == 2:
        address, city = parts
    elif len(parts) == 1:
        address = parts[0]

    if len(full_name) < 2:
        full_name = "Procurement Team"
    if len(address) < 5:
        address = f"{address} Road".strip()
        if len(address) < 5:
            address = "42 Market Street"
    if len(city) < 2:
        city = "Pune"
    if len(state) < 2:
        state = "Maharashtra"

    return {
        "full_name": full_name[:100],
        "phone": phone,
        "pincode": pincode,
        "address": address[:200],
        "city": city[:80],
        "state": state[:80],
    }


def _is_valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def pick_best_supplier(
    prices_found: dict[str, dict[str, float] | float | int],
    unsafe_suppliers: set[str] | list[str] | tuple[str, ...] | None,
    max_budget: float,
    required_items: list[str] | None = None,
    stock_found: dict[str, dict[str, int]] | None = None,
) -> str | None:
    """Return the cheapest safe supplier fitting max_budget, or None if none qualify."""
    if not isinstance(prices_found, dict) or not _is_valid_number(max_budget) or max_budget < 0:
        return None
    if required_items is not None and len(required_items) == 0:
        return None

    unsafe = set(unsafe_suppliers or ())
    best_supplier: str | None = None
    best_total: float | None = None

    for supplier, entry in prices_found.items():
        if supplier in unsafe:
            continue

        if isinstance(entry, dict):
            if not entry:
                continue
            if required_items is not None:
                if any(not _is_valid_number(entry.get(item)) or float(entry[item]) < 0 for item in required_items):
                    continue
                if stock_found is not None:
                    supplier_stock = stock_found.get(supplier, {})
                    if any((supplier_stock.get(item) or 0) <= 0 for item in required_items):
                        continue
                total = sum(float(entry[item]) for item in required_items)
            else:
                if any(not _is_valid_number(v) or float(v) < 0 for v in entry.values()):
                    continue
                total = sum(float(v) for v in entry.values())
        elif _is_valid_number(entry) and float(entry) >= 0:
            total = float(entry)
        else:
            continue

        if total > float(max_budget):
            continue

        if best_total is None or total < best_total:
            best_total = total
            best_supplier = supplier

    return best_supplier


def _candidate_search_queries(item: str) -> list[str]:
    """Generate search queries trying the full item name first, then shorter keywords."""
    cleaned = " ".join(item.split()).strip()
    if not cleaned:
        return []

    queries: list[str] = [cleaned]
    seen: set[str] = {cleaned.lower()}

    words = [w.strip(",.;:-_()[]{}") for w in cleaned.split()]
    words = [w for w in words if w]

    if len(words) > 1:
        # Contiguous shorter sub-phrases from longer to shorter (trailing phrases first)
        for length in range(len(words) - 1, 1, -1):
            for start in range(len(words) - length, -1, -1):
                phrase = " ".join(words[start : start + length])
                if phrase.lower() not in seen:
                    seen.add(phrase.lower())
                    queries.append(phrase)

        # Individual words: head noun is typically at the end, prefer length >= 3
        for word in reversed(words):
            if len(word) >= 3 and word.lower() not in seen:
                seen.add(word.lower())
                queries.append(word)
        for word in reversed(words):
            if word.lower() not in seen:
                seen.add(word.lower())
                queries.append(word)

    return queries


def _detect_captcha(url: str, visible_text: str, raw_text: str) -> bool:
    """Detect whether the current page is a CAPTCHA / human verification challenge."""
    if "/captcha" in url.lower():
        return True
    if "verify you are human" in visible_text.lower() or "verify you are human" in raw_text.lower():
        return True
    if _CAPTCHA_TEXT_RE.search(visible_text):
        return True
    return False


def _extract_item_metrics(
    visible_text: str,
    item_name: str,
    matched_query: str | None = None,
) -> tuple[float | None, int | None]:
    """Read price and stock for *item_name* from visible page text."""
    lowered = visible_text.lower()
    if "no products found" in lowered or "no matching supplies" in lowered:
        return None, 0

    idx = lowered.find(item_name.lower())
    if idx < 0 and matched_query:
        idx = lowered.find(matched_query.lower())
    if idx < 0 and "₹" not in visible_text:
        return None, 0

    snippet = visible_text[idx:] if idx >= 0 else visible_text
    price = parse_price(snippet)
    stock = parse_stock(snippet)
    return price, stock


def _find_element(
    elements: list[DOMElement],
    *,
    tags: tuple[str, ...],
    keywords: tuple[str, ...],
    exclude: tuple[str, ...] = (),
    exclude_ids: set[int] | None = None,
) -> DOMElement | None:
    """Find the first matching element generically by tag priority and label/text/selector."""
    blocked_ids = exclude_ids or set()
    for wanted_tag in tags:
        tag_lower = wanted_tag.lower()
        # First pass: match visible/label text
        for el in elements:
            if el.id in blocked_ids or el.tag.lower() != tag_lower:
                continue
            text_lower = el.text.lower()
            if any(ex in text_lower for ex in exclude):
                continue
            if any(kw in text_lower for kw in keywords):
                return el
        # Second pass: fallback to normalized selector (e.g. name/id attributes)
        for el in elements:
            if el.id in blocked_ids or el.tag.lower() != tag_lower:
                continue
            sel_norm = re.sub(r"[-_]+", " ", el.selector.lower())
            if any(ex in sel_norm for ex in exclude):
                continue
            if any(kw in sel_norm for kw in keywords):
                return el
    return None


@dataclass
class _RunnerState:
    task: ShoppingTask
    bm: BrowserManager
    policy_check_fn: Callable[..., Any]
    audit_emit_fn: Callable[..., Any]
    sanitize_fn: Callable[..., Any]
    kill_switch: Any
    max_llm_calls: int
    install_firewall_fn: Callable[..., Any] | None = None
    step_count: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    history: list[str] = field(default_factory=list)
    action_counts: Counter[tuple[Any, ...]] = field(default_factory=Counter)
    unsafe_reasons: dict[str, str] = field(default_factory=dict)
    prices_found: dict[str, dict[str, float]] = field(default_factory=dict)
    stock_found: dict[str, dict[str, int]] = field(default_factory=dict)
    matched_queries: dict[str, dict[str, str]] = field(default_factory=dict)
    terminal_status: str | None = None
    terminal_reason: str = ""

    def mark_unsafe(self, supplier: str, reason: str) -> None:
        if supplier not in self.unsafe_reasons:
            self.unsafe_reasons[supplier] = reason


class _SupplierAborted(Exception):
    """Raised internally when the current supplier is marked unsafe."""


class _RunHalted(Exception):
    """Raised internally when the entire run must stop immediately."""


async def _maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


async def _setup_supplier_context(state: _RunnerState) -> None:
    """Create a fresh browser context for a supplier and install the firewall if provided."""
    sig = inspect.signature(state.bm.new_ephemeral_context)
    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_kwargs or "service_workers" in sig.parameters:
        context = await state.bm.new_ephemeral_context(service_workers="block")
    else:
        context = await state.bm.new_ephemeral_context()

    if state.install_firewall_fn is not None and context is not None:
        await _maybe_await(state.install_firewall_fn(context))


async def _click_and_settle(bm: BrowserManager, selector: str) -> None:
    """Click *selector* and wait for any resulting navigation or firewall abort to settle."""
    page = bm.page
    if page is None or not hasattr(page, "on"):
        await bm.click(selector)
        if page is not None:
            await page.wait_for_load_state("domcontentloaded")
        return

    nav_started = asyncio.Event()
    nav_settled = asyncio.Event()
    nav_failed = asyncio.Event()

    def _on_request(req: Any) -> None:
        try:
            if req.is_navigation_request() and req.frame == page.main_frame:
                nav_started.set()
        except Exception:
            pass

    def _on_frame_navigated(frame: Any) -> None:
        if nav_started.is_set() and frame == page.main_frame:
            nav_settled.set()

    def _on_request_failed(req: Any) -> None:
        try:
            if req.is_navigation_request() and req.frame == page.main_frame:
                nav_failed.set()
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("framenavigated", _on_frame_navigated)
    page.on("requestfailed", _on_request_failed)
    try:
        await bm.click(selector)
        if not nav_started.is_set():
            try:
                await asyncio.wait_for(nav_started.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
        if nav_started.is_set():
            if not nav_settled.is_set():
                if nav_failed.is_set():
                    try:
                        await asyncio.wait_for(nav_settled.wait(), timeout=0.3)
                    except asyncio.TimeoutError:
                        pass
                else:
                    settled_task = asyncio.create_task(nav_settled.wait())
                    failed_task = asyncio.create_task(nav_failed.wait())
                    done, pending = await asyncio.wait(
                        {settled_task, failed_task},
                        timeout=3.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if failed_task in done and not nav_settled.is_set():
                        try:
                            await asyncio.wait_for(nav_settled.wait(), timeout=0.3)
                        except asyncio.TimeoutError:
                            pass
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                await asyncio.sleep(0.1)
        else:
            await page.wait_for_load_state("domcontentloaded")
    finally:
        for event_name, cb in (
            ("request", _on_request),
            ("framenavigated", _on_frame_navigated),
            ("requestfailed", _on_request_failed),
        ):
            try:
                page.remove_listener(event_name, cb)
            except Exception:
                pass


async def _inspect_page_safety(
    state: _RunnerState,
    supplier: str,
) -> tuple[list[DOMElement], str, str]:
    """Extract elements, visible text, and raw text, and enforce CAPTCHA + sanitization."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            current_url = state.bm.page.url if state.bm.page is not None else ""
            elements = await state.bm.extract_elements()
            raw_text = await state.bm.get_raw_text()
            visible_text = await state.bm.get_visible_text()
            break
        except Exception as exc:
            last_exc = exc
            if "Execution context was destroyed" in str(exc) and attempt < 2:
                if state.bm.page is not None:
                    try:
                        await state.bm.page.wait_for_load_state("domcontentloaded", timeout=2000)
                    except Exception:
                        pass
                await asyncio.sleep(0.1)
                continue
            raise
    else:
        assert last_exc is not None
        raise last_exc

    if _detect_captcha(current_url, visible_text, raw_text):
        state.mark_unsafe(supplier, f"CAPTCHA detected at {current_url}")
        raise _SupplierAborted()

    clean_elements, flags = await _maybe_await(state.sanitize_fn(elements, raw_text))
    if flags:
        state.mark_unsafe(supplier, f"Sanitizer flagged content: {flags[0]}")
        raise _SupplierAborted()

    return clean_elements, visible_text, raw_text


async def _execute_pipeline(
    state: _RunnerState,
    supplier: str,
    action: BrowserAction,
    *,
    target_text: str | None = None,
    tokens_used: int = 0,
    is_final_payment: bool = False,
) -> None:
    """Run every browser action through kill switch, sanitize, policy, audit, and execution."""
    if state.kill_switch is not None and getattr(state.kill_switch, "triggered", False):
        abort_fn = getattr(state.kill_switch, "abort", None)
        if callable(abort_fn):
            await _maybe_await(abort_fn(state.bm))
        state.terminal_status = "halted"
        state.terminal_reason = "Kill switch triggered."
        raise _RunHalted()

    if state.step_count >= MAX_STEPS:
        state.terminal_status = "halted"
        state.terminal_reason = f"Reached maximum step limit ({MAX_STEPS})."
        raise _RunHalted()

    current_url = state.bm.page.url if state.bm.page is not None else ""
    sig = (
        supplier,
        current_url,
        action.action_type,
        action.url,
        action.selector,
        action.value,
    )
    state.action_counts[sig] += 1
    if state.action_counts[sig] > MAX_ACTION_REPEATS:
        state.terminal_status = "halted"
        state.terminal_reason = f"Action repeated more than {MAX_ACTION_REPEATS} times."
        raise _RunHalted()

    # Extract elements & raw text and run CAPTCHA + sanitize checks on the current page.
    clean_elements, _, _ = await _inspect_page_safety(state, supplier)

    # Refresh target_text from live extracted elements if possible
    resolved_target_text = target_text
    if action.element_id is not None:
        live_el = next((el for el in clean_elements if el.id == action.element_id), None)
        if live_el is not None:
            resolved_target_text = live_el.text
    elif action.selector:
        live_el = next((el for el in clean_elements if el.selector == action.selector), None)
        if live_el is not None:
            resolved_target_text = live_el.text

    state.step_count += 1
    step_num = state.step_count

    verdict: PolicyVerdict = await _maybe_await(
        state.policy_check_fn(action, resolved_target_text)
    )
    screenshot_path: str | None = None
    if state.bm.page is not None:
        screenshot_path = await state.bm.save_screenshot(step_num=step_num, name=supplier.lower())

    event = AuditEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        step_num=step_num,
        url=current_url,
        action=action,
        verdict=verdict,
        screenshot_path=screenshot_path,
        tokens_used=tokens_used,
    )
    await _maybe_await(state.audit_emit_fn(event))

    if state.kill_switch is not None and getattr(state.kill_switch, "triggered", False):
        abort_fn = getattr(state.kill_switch, "abort", None)
        if callable(abort_fn):
            await _maybe_await(abort_fn(state.bm))
        state.terminal_status = "halted"
        state.terminal_reason = "Kill switch triggered."
        raise _RunHalted()

    if verdict.risk_level == "blocked":
        state.mark_unsafe(supplier, f"Policy blocked action: {verdict.message}")
        raise _SupplierAborted()

    if verdict.risk_level == "requires_approval" or is_final_payment:
        state.terminal_status = "awaiting_approval"
        state.terminal_reason = verdict.message or "Awaiting human approval before final payment."
        raise _RunHalted()

    if not verdict.allowed:
        state.mark_unsafe(supplier, f"Policy denied action: {verdict.message}")
        raise _SupplierAborted()

    # Safe verdict -> execute the browser action
    if action.action_type == "navigate":
        if not action.url:
            raise ValueError("navigate action requires url")
        await state.bm.goto(action.url, wait_until="domcontentloaded")
    elif action.action_type == "click":
        if not action.selector:
            raise ValueError("click action requires selector")
        await _click_and_settle(state.bm, action.selector)
    elif action.action_type == "type":
        if not action.selector or action.value is None:
            raise ValueError("type action requires selector and value")
        await state.bm.fill(action.selector, action.value)
    elif action.action_type == "extract":
        await state.bm.extract_elements()
    elif action.action_type == "halt":
        state.terminal_status = "halted"
        state.terminal_reason = action.reason
        raise _RunHalted()

    state.history.append(
        f"{supplier}:{action.action_type}:{resolved_target_text or action.url or action.selector}"
    )


async def _llm_fallback_step(state: _RunnerState, supplier: str) -> None:
    """Ask the LLM planner for the next action and execute it through the pipeline."""
    if state.llm_calls >= state.max_llm_calls:
        state.terminal_status = "halted"
        state.terminal_reason = f"Reached max_llm_calls ({state.max_llm_calls})."
        raise _RunHalted()

    clean_elements, _, _ = await _inspect_page_safety(state, supplier)
    current_url = state.bm.page.url if state.bm.page is not None else ""
    action, tokens = await next_action(state.task, current_url, clean_elements, state.history)
    state.llm_calls += 1
    state.total_tokens += tokens

    target_text = None
    if action.element_id is not None:
        matched = next((el for el in clean_elements if el.id == action.element_id), None)
        if matched is not None:
            target_text = matched.text
    elif action.selector:
        matched = next((el for el in clean_elements if el.selector == action.selector), None)
        if matched is not None:
            target_text = matched.text

    await _execute_pipeline(
        state,
        supplier,
        action,
        target_text=target_text,
        tokens_used=tokens,
    )


async def _login_supplier(state: _RunnerState, supplier: str, base_url: str) -> None:
    """Navigate to supplier login page and authenticate with demo/demo."""
    await _execute_pipeline(
        state,
        supplier,
        BrowserAction(action_type="navigate", url=f"{base_url}/login", reason=f"Open {supplier} login"),
    )

    for _ in range(4):
        clean_elements, _, _ = await _inspect_page_safety(state, supplier)
        current_url = state.bm.page.url if state.bm.page is not None else ""
        if "/login" not in current_url.lower():
            return

        user_el = _find_element(
            clean_elements,
            tags=("input",),
            keywords=("username", "user", "email"),
            exclude=("password",),
        )
        pass_el = _find_element(
            clean_elements,
            tags=("input",),
            keywords=("password",),
            exclude_ids={user_el.id} if user_el else None,
        )
        used_ids = {el.id for el in (user_el, pass_el) if el is not None}
        login_btn = _find_element(
            clean_elements,
            tags=("button", "input"),
            keywords=("sign in", "log in", "login"),
            exclude_ids=used_ids,
        )

        if user_el and pass_el and login_btn:
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="type",
                    element_id=user_el.id,
                    selector=user_el.selector,
                    value="demo",
                    reason="Enter demo username",
                ),
                target_text=user_el.text,
            )
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="type",
                    element_id=pass_el.id,
                    selector=pass_el.selector,
                    value="demo",
                    reason="Enter demo password",
                ),
                target_text=pass_el.text,
            )
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="click",
                    element_id=login_btn.id,
                    selector=login_btn.selector,
                    reason="Submit login form",
                ),
                target_text=login_btn.text,
            )
            continue

        await _llm_fallback_step(state, supplier)


async def _search_query(state: _RunnerState, supplier: str, base_url: str, query: str) -> None:
    """Search the supplier catalog using the ?q= query endpoint, falling back to form/LLM if needed."""
    search_url = f"{base_url}/?q={quote_plus(query)}"
    await _execute_pipeline(
        state,
        supplier,
        BrowserAction(action_type="navigate", url=search_url, reason=f"Search {supplier} for {query}"),
    )

    current_url = state.bm.page.url if state.bm.page is not None else ""
    if "/login" in current_url.lower():
        await _login_supplier(state, supplier, base_url)
        await _execute_pipeline(
            state,
            supplier,
            BrowserAction(action_type="navigate", url=search_url, reason=f"Search {supplier} for {query}"),
        )


async def _phase1_recon_suppliers(state: _RunnerState) -> None:
    """Phase 1: For each supplier, use a fresh context, log in, search each item, and read price & stock."""
    for supplier, base_url in SUPPLIER_URLS.items():
        try:
            await _setup_supplier_context(state)
            await _login_supplier(state, supplier, base_url)
            state.prices_found[supplier] = {}
            state.stock_found[supplier] = {}
            state.matched_queries[supplier] = {}

            for item in state.task.items:
                for query in _candidate_search_queries(item):
                    await _search_query(state, supplier, base_url, query)
                    _, visible_text, _ = await _inspect_page_safety(state, supplier)
                    price, stock = _extract_item_metrics(visible_text, item, matched_query=query)
                    if price is not None:
                        state.prices_found[supplier][item] = price
                        if stock is not None:
                            state.stock_found[supplier][item] = stock
                        state.matched_queries[supplier][item] = query
                        break
                    if stock is not None and item not in state.stock_found[supplier]:
                        state.stock_found[supplier][item] = stock
        except _SupplierAborted:
            continue
        except _RunHalted:
            raise
        except Exception as exc:
            state.mark_unsafe(supplier, f"Supplier check failed: {exc}")
            continue


def _pick_add_to_cart_button(
    elements: list[DOMElement],
    item: str,
    query: str,
) -> DOMElement | None:
    """Find the Add to Cart button, preferring a match for *item*/*query* when multiple exist."""
    candidates = [
        el
        for el in elements
        if el.tag.lower() in {"button", "input"}
        and "add to cart" in el.text.lower()
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    keywords = [w.lower() for w in f"{item} {query}".split() if len(w) >= 3]
    for el in candidates:
        haystack = f"{el.text} {el.selector}".lower()
        if any(kw in haystack for kw in keywords):
            return el
    return candidates[0]


async def _phase3_4_checkout(state: _RunnerState, supplier: str) -> None:
    """Phase 3 & 4: Fresh context on chosen supplier, add items, open checkout, fill shipping, stop before final pay."""
    base_url = SUPPLIER_URLS[supplier]
    await _setup_supplier_context(state)
    await _login_supplier(state, supplier, base_url)

    for item in state.task.items:
        preferred = state.matched_queries.get(supplier, {}).get(item)
        queries = [preferred] if preferred else _candidate_search_queries(item)
        added = False

        for query in queries:
            await _search_query(state, supplier, base_url, query)
            for _ in range(3):
                clean_elements, _, _ = await _inspect_page_safety(state, supplier)
                add_btn = _pick_add_to_cart_button(clean_elements, item, query)
                if add_btn is not None:
                    await _execute_pipeline(
                        state,
                        supplier,
                        BrowserAction(
                            action_type="click",
                            element_id=add_btn.id,
                            selector=add_btn.selector,
                            reason=f"Add {item} to cart",
                        ),
                        target_text=add_btn.text,
                    )
                    added = True
                    break
                if len(queries) > 1:
                    break
                await _llm_fallback_step(state, supplier)
            if added:
                break

        if not added:
            state.terminal_status = "halted"
            state.terminal_reason = f"Could not add '{item}' to cart on {supplier}."
            raise _RunHalted()

    # Proceed from cart to checkout
    for _ in range(4):
        clean_elements, _, _ = await _inspect_page_safety(state, supplier)
        current_url = state.bm.page.url if state.bm.page is not None else ""
        if "/checkout" in current_url.lower():
            break
        checkout_el = _find_element(clean_elements, tags=("a", "button"), keywords=("checkout",))
        if checkout_el is not None:
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="click",
                    element_id=checkout_el.id,
                    selector=checkout_el.selector,
                    reason="Proceed to checkout",
                ),
                target_text=checkout_el.text,
            )
            continue
        if "/cart" not in current_url.lower():
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(action_type="navigate", url=f"{base_url}/cart", reason="Open cart"),
            )
            continue
        await _llm_fallback_step(state, supplier)

    # Fill shipping fields on /checkout
    shipping = parse_shipping_address(state.task.shipping_address)
    field_specs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("full_name", ("full name", "name")),
        ("phone", ("phone", "mobile", "tel")),
        ("pincode", ("pin code", "pincode", "postal", "zip")),
        ("address", ("street address", "address", "street")),
        ("city", ("city",)),
        ("state", ("state",)),
    )

    clean_elements, _, _ = await _inspect_page_safety(state, supplier)
    filled_ids: set[int] = set()
    for field_key, keywords in field_specs:
        field_el = _find_element(
            clean_elements,
            tags=("input", "textarea"),
            keywords=keywords,
            exclude_ids=filled_ids,
        )
        if field_el is not None:
            filled_ids.add(field_el.id)
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="type",
                    element_id=field_el.id,
                    selector=field_el.selector,
                    value=shipping[field_key],
                    reason=f"Fill shipping {field_key}",
                ),
                target_text=field_el.text,
            )
        else:
            await _llm_fallback_step(state, supplier)
            clean_elements, _, _ = await _inspect_page_safety(state, supplier)

    # Phase 4: Submit final button through pipeline -> policy requires_approval halts without clicking
    clean_elements, _, _ = await _inspect_page_safety(state, supplier)
    pay_btn = _find_element(
        clean_elements,
        tags=("button", "input"),
        keywords=("place order", "confirm order", "pay now", "pay"),
    )
    if pay_btn is not None:
        await _execute_pipeline(
            state,
            supplier,
            BrowserAction(
                action_type="click",
                element_id=pay_btn.id,
                selector=pay_btn.selector,
                reason="Reach final payment button",
            ),
            target_text=pay_btn.text,
            is_final_payment=True,
        )
    else:
        state.terminal_status = "awaiting_approval"
        state.terminal_reason = "Checkout filled; stopped before final payment button."


def _write_run_result(result: dict[str, Any]) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = LOGS_DIR / "run_result.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception:
        pass


def _error_result(status: str, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "chosen_supplier": None,
        "prices_found": {},
        "unsafe_suppliers": [],
        "unsafe_reasons": {},
        "total_tokens": 0,
        "steps": 0,
        "reason": reason,
    }
    _write_run_result(result)
    return result


def _validate_run_inputs(
    task: Any,
    policy_check_fn: Any,
    audit_emit_fn: Any,
    sanitize_fn: Any,
    install_firewall_fn: Any,
    max_llm_calls: Any,
) -> tuple[ShoppingTask | None, str | None]:
    """Validate inputs to run() without raising."""
    if not callable(policy_check_fn):
        return None, "policy_check_fn must be callable."
    if not callable(audit_emit_fn):
        return None, "audit_emit_fn must be callable."
    if sanitize_fn is not None and not callable(sanitize_fn):
        return None, "sanitize_fn must be callable when provided."
    if install_firewall_fn is not None and not callable(install_firewall_fn):
        return None, "install_firewall_fn must be callable when provided."
    if isinstance(max_llm_calls, bool) or not isinstance(max_llm_calls, int) or max_llm_calls < 0:
        return None, "max_llm_calls must be a non-negative integer."

    parsed_task: ShoppingTask
    if isinstance(task, ShoppingTask):
        parsed_task = task
    elif isinstance(task, dict):
        try:
            parsed_task = ShoppingTask.model_validate(task)
        except ValidationError as exc:
            return None, f"Invalid task payload: {exc}"
    else:
        return None, "task must be a ShoppingTask instance."

    cleaned_items = [item.strip() for item in parsed_task.items if isinstance(item, str) and item.strip()]
    if not cleaned_items or len(cleaned_items) != len(parsed_task.items):
        return None, "task.items must be a non-empty list of non-empty strings."

    if not _is_valid_number(parsed_task.max_budget) or float(parsed_task.max_budget) <= 0:
        return None, "task.max_budget must be a positive finite number."

    if not isinstance(parsed_task.shipping_address, str) or not parsed_task.shipping_address.strip():
        return None, "task.shipping_address must be a non-empty string."

    normalized_task = ShoppingTask(
        items=cleaned_items,
        max_budget=float(parsed_task.max_budget),
        shipping_address=parsed_task.shipping_address.strip(),
    )
    return normalized_task, None


async def run(
    task: ShoppingTask,
    policy_check_fn: Callable[..., Any],
    audit_emit_fn: Callable[..., Any],
    sanitize_fn: Callable[..., Any] | None = None,
    install_firewall_fn: Callable[..., Any] | None = None,
    kill_switch: Any = None,
    max_llm_calls: int = 40,
) -> dict[str, Any]:
    """Execute the multi-supplier shopping workflow with unified security enforcement."""
    normalized_task, validation_error = _validate_run_inputs(
        task,
        policy_check_fn,
        audit_emit_fn,
        sanitize_fn,
        install_firewall_fn,
        max_llm_calls,
    )
    if validation_error is not None or normalized_task is None:
        return _error_result("invalid_input", validation_error or "Invalid input.")

    chosen_supplier: str | None = None
    state: _RunnerState | None = None

    if sanitize_fn is None:
        warning_msg = (
            "WARNING: run() called with sanitize_fn=None; "
            "falling back to stub fake_sanitize. Provide a real sanitize_fn in production!"
        )
        logger.warning(warning_msg)
        warnings.warn(warning_msg, RuntimeWarning, stacklevel=2)
        print(warning_msg, file=sys.stderr, flush=True)
        effective_sanitize_fn = fake_sanitize
    else:
        effective_sanitize_fn = sanitize_fn

    try:
        async with BrowserManager(headless=True) as bm:
            state = _RunnerState(
                task=normalized_task,
                bm=bm,
                policy_check_fn=policy_check_fn,
                audit_emit_fn=audit_emit_fn,
                sanitize_fn=effective_sanitize_fn,
                install_firewall_fn=install_firewall_fn,
                kill_switch=kill_switch,
                max_llm_calls=max_llm_calls,
            )

            try:
                await _phase1_recon_suppliers(state)

                chosen_supplier = pick_best_supplier(
                    state.prices_found,
                    list(state.unsafe_reasons.keys()),
                    normalized_task.max_budget,
                    required_items=normalized_task.items,
                    stock_found=state.stock_found,
                )

                if chosen_supplier is None:
                    state.terminal_status = "halted"
                    state.terminal_reason = "No safe supplier fits max_budget and required items."
                else:
                    await _phase3_4_checkout(state, chosen_supplier)
            except _SupplierAborted:
                state.terminal_status = "halted"
                state.terminal_reason = "Chosen supplier became unsafe during checkout."
            except _RunHalted:
                pass
    except Exception as exc:
        if state is None:
            return _error_result("error", f"Runner failed: {exc}")
        state.terminal_status = "error"
        state.terminal_reason = f"Runner failed: {exc}"

    assert state is not None
    result: dict[str, Any] = {
        "status": state.terminal_status or "halted",
        "chosen_supplier": chosen_supplier,
        "prices_found": state.prices_found,
        "unsafe_suppliers": list(state.unsafe_reasons.keys()),
        "unsafe_reasons": state.unsafe_reasons,
        "total_tokens": state.total_tokens,
        "steps": state.step_count,
        "reason": state.terminal_reason,
    }
    _write_run_result(result)
    return result
