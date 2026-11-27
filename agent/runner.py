"""agent/runner.py

Unified shopping agent runner with dependency-injected security policy,
sanitization, auditing, network firewall, and kill switch hooks.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

# Ensure project root is in sys.path when running this module directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.browser import BrowserManager
from agent.planner import next_action
from agent.stubs import fake_sanitize
from shared.config import SUPPLIER_URLS
from shared.schemas import AuditEvent, BrowserAction, DOMElement, PolicyVerdict, ShoppingTask

MAX_STEPS = 40
MAX_ACTION_REPEATS = 3
LOGS_DIR = PROJECT_ROOT / "logs"

_PRICE_RUPEE_RE = re.compile(r"₹\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_PRICE_NUM_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")
_STOCK_RE = re.compile(r"(\d[\d,]*)\s*(?:in\s+stock|available)\b", re.IGNORECASE)
_CAPTCHA_TEXT_RE = re.compile(r"\bcaptcha\b", re.IGNORECASE)
_PINCODE_RE = re.compile(r"\b(\d{6})\b")
_PHONE_RE = re.compile(r"(\+?\d[\d\s()-]{8,14}\d)")


def parse_price(text: str) -> float | None:
    """Parse a currency amount (handling ₹ and commas) from visible text."""
    if not text:
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


def parse_stock(text: str) -> int | None:
    """Parse available stock count from portal stock labels ('in stock' or 'available')."""
    if not text:
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
    raw = shipping_address.strip()
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
        re.sub(r"^(?:name|full name|phone|address|city|state|pin|pincode)\s*:\s*", "", part.strip(), flags=re.IGNORECASE)
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


def pick_best_supplier(
    prices_found: dict[str, dict[str, float] | float | int],
    unsafe_suppliers: set[str] | list[str] | tuple[str, ...],
    max_budget: float,
    required_items: list[str] | None = None,
    stock_found: dict[str, dict[str, int]] | None = None,
) -> str | None:
    """Return the cheapest safe supplier fitting max_budget, or None if none qualify."""
    unsafe = set(unsafe_suppliers)
    best_supplier: str | None = None
    best_total: float | None = None

    for supplier, entry in prices_found.items():
        if supplier in unsafe:
            continue

        if isinstance(entry, dict):
            if not entry:
                continue
            if required_items is not None:
                if any(entry.get(item) is None for item in required_items):
                    continue
                if stock_found is not None:
                    supplier_stock = stock_found.get(supplier, {})
                    if any((supplier_stock.get(item) or 0) <= 0 for item in required_items):
                        continue
                total = sum(float(entry[item]) for item in required_items)
            else:
                total = sum(float(v) for v in entry.values())
        elif isinstance(entry, (int, float)):
            total = float(entry)
        else:
            continue

        if total > max_budget:
            continue

        if best_total is None or total < best_total:
            best_total = total
            best_supplier = supplier

    return best_supplier


def _detect_captcha(url: str, visible_text: str, raw_text: str) -> bool:
    """Detect whether the current page is a CAPTCHA / human verification challenge."""
    if "/captcha" in url.lower():
        return True
    combined = f"{visible_text}\n{raw_text}"
    lowered = combined.lower()
    if "verify you are human" in lowered:
        return True
    if _CAPTCHA_TEXT_RE.search(combined):
        return True
    return False


def _extract_item_metrics(visible_text: str, item_name: str) -> tuple[float | None, int | None]:
    """Read price and stock for *item_name* from visible page text."""
    lowered = visible_text.lower()
    if "no products found" in lowered or "no matching supplies" in lowered:
        return None, 0

    idx = lowered.find(item_name.lower())
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
) -> DOMElement | None:
    """Find the first matching element generically by tag and label/text keywords."""
    tag_set = {t.lower() for t in tags}
    for el in elements:
        if el.tag.lower() not in tag_set:
            continue
        text_lower = el.text.lower()
        if any(ex in text_lower for ex in exclude):
            continue
        if any(kw in text_lower for kw in keywords):
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
    step_count: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    history: list[str] = field(default_factory=list)
    action_counts: Counter[tuple[Any, ...]] = field(default_factory=Counter)
    unsafe_reasons: dict[str, str] = field(default_factory=dict)
    prices_found: dict[str, dict[str, float]] = field(default_factory=dict)
    stock_found: dict[str, dict[str, int]] = field(default_factory=dict)
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


async def _inspect_page_safety(
    state: _RunnerState,
    supplier: str,
) -> tuple[list[DOMElement], str, str]:
    """Extract elements, visible text, and raw text, and enforce CAPTCHA + sanitization."""
    current_url = state.bm.page.url if state.bm.page is not None else ""
    elements = await state.bm.extract_elements()
    raw_text = await state.bm.get_raw_text()
    visible_text = await state.bm.get_visible_text()

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
    await _inspect_page_safety(state, supplier)

    state.step_count += 1
    step_num = state.step_count

    verdict: PolicyVerdict = await _maybe_await(state.policy_check_fn(action, target_text))
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
        await state.bm.click(action.selector)
        if state.bm.page is not None:
            await state.bm.page.wait_for_load_state("domcontentloaded")
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

    state.history.append(f"{supplier}:{action.action_type}:{target_text or action.url or action.selector}")


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

        user_el = _find_element(clean_elements, tags=("input",), keywords=("username", "user", "email"), exclude=("password",))
        pass_el = _find_element(clean_elements, tags=("input",), keywords=("password",))
        login_btn = _find_element(clean_elements, tags=("button", "input"), keywords=("sign in", "log in", "login"))

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
            return

        await _llm_fallback_step(state, supplier)


async def _search_item(state: _RunnerState, supplier: str, base_url: str, item: str) -> None:
    """Ensure we are on the catalog page and search for *item*."""
    current_url = state.bm.page.url if state.bm.page is not None else ""
    if not current_url.startswith(base_url) or any(seg in current_url for seg in ("/login", "/cart", "/checkout")):
        await _execute_pipeline(
            state,
            supplier,
            BrowserAction(action_type="navigate", url=f"{base_url}/", reason=f"Open {supplier} catalog"),
        )

    for _ in range(3):
        clean_elements, _, _ = await _inspect_page_safety(state, supplier)
        search_input = _find_element(clean_elements, tags=("input",), keywords=("search",))
        search_btn = _find_element(clean_elements, tags=("button", "input"), keywords=("search",))
        if search_input and search_btn:
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="type",
                    element_id=search_input.id,
                    selector=search_input.selector,
                    value=item,
                    reason=f"Search for {item}",
                ),
                target_text=search_input.text,
            )
            await _execute_pipeline(
                state,
                supplier,
                BrowserAction(
                    action_type="click",
                    element_id=search_btn.id,
                    selector=search_btn.selector,
                    reason=f"Submit search for {item}",
                ),
                target_text=search_btn.text,
            )
            return

        await _llm_fallback_step(state, supplier)


async def _phase1_recon_suppliers(state: _RunnerState) -> None:
    """Phase 1: For each supplier, log in, search every item, and record price & stock."""
    for supplier, base_url in SUPPLIER_URLS.items():
        try:
            await _login_supplier(state, supplier, base_url)
            state.prices_found[supplier] = {}
            state.stock_found[supplier] = {}

            for item in state.task.items:
                await _search_item(state, supplier, base_url, item)
                _, visible_text, _ = await _inspect_page_safety(state, supplier)
                price, stock = _extract_item_metrics(visible_text, item)
                if price is not None:
                    state.prices_found[supplier][item] = price
                if stock is not None:
                    state.stock_found[supplier][item] = stock
        except _SupplierAborted:
            continue


async def _phase3_4_checkout(state: _RunnerState, supplier: str) -> None:
    """Phase 3 & 4: Add items, open checkout, fill shipping, and stop before final pay."""
    base_url = SUPPLIER_URLS[supplier]

    # Ensure logged in on chosen supplier (handles returning from another supplier cleanly)
    await _execute_pipeline(
        state,
        supplier,
        BrowserAction(action_type="navigate", url=f"{base_url}/", reason=f"Return to {supplier} catalog"),
    )
    if state.bm.page is not None and "/login" in state.bm.page.url.lower():
        await _login_supplier(state, supplier, base_url)

    for item in state.task.items:
        await _search_item(state, supplier, base_url, item)
        for _ in range(3):
            clean_elements, _, _ = await _inspect_page_safety(state, supplier)
            add_btn = _find_element(clean_elements, tags=("button",), keywords=("add to cart",))
            if add_btn:
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
                break
            await _llm_fallback_step(state, supplier)

    # Proceed from cart to checkout
    for _ in range(3):
        clean_elements, _, _ = await _inspect_page_safety(state, supplier)
        current_url = state.bm.page.url if state.bm.page is not None else ""
        if "/checkout" in current_url.lower():
            break
        checkout_el = _find_element(clean_elements, tags=("a", "button"), keywords=("checkout",))
        if checkout_el:
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
            break
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
    for field_key, keywords in field_specs:
        field_el = _find_element(clean_elements, tags=("input", "textarea"), keywords=keywords)
        if field_el is not None:
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
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOGS_DIR / "run_result.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


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
    chosen_supplier: str | None = None

    async with BrowserManager(headless=True) as bm:
        if install_firewall_fn is not None and bm.page is not None:
            await _maybe_await(install_firewall_fn(bm.page))

        state = _RunnerState(
            task=task,
            bm=bm,
            policy_check_fn=policy_check_fn,
            audit_emit_fn=audit_emit_fn,
            sanitize_fn=sanitize_fn or fake_sanitize,
            kill_switch=kill_switch,
            max_llm_calls=max_llm_calls,
        )

        try:
            await _phase1_recon_suppliers(state)

            chosen_supplier = pick_best_supplier(
                state.prices_found,
                list(state.unsafe_reasons.keys()),
                task.max_budget,
                required_items=task.items,
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
