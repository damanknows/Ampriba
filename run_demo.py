"""run_demo.py

End-to-end procurement demo wiring agent/runner.py to the real security modules
(KillSwitch, evaluate_action, suspicious_content, install_firewall) and audit logging.

Run from the repository root:
    python run_demo.py
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
import socket
import sys
import threading
import time
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import BrowserContext, Page

from agent.runner import LOGS_DIR, run
from security.firewall import evaluate_action, install_firewall
from security.kill_switch import KillSwitch
from security.sanitizer import suspicious_content
from shared.config import HOST, SUPPLIER_A_PORT, SUPPLIER_B_PORT, SUPPLIER_C_PORT
from shared.schemas import AuditEvent, BrowserAction, DOMElement, PolicyVerdict, ShoppingTask

SAMPLE_SHIPPING_ADDRESS = "Morgan Lee, 9876543210, 42 Market Street, Pune, Maharashtra, 411001"
AUDIT_JSONL_PATH = LOGS_DIR / "audit.jsonl"
RUN_RESULT_PATH = LOGS_DIR / "run_result.json"

_FIREWALL_TASKS: set[asyncio.Task[None]] = set()


def build_policy_check_fn(ks: KillSwitch) -> Callable[[BrowserAction, str | None], PolicyVerdict]:
    """Return a policy check callback enforcing KillSwitch before evaluate_action."""

    def policy_check_fn(action: BrowserAction, target_text: str | None = None) -> PolicyVerdict:
        allowed_by_switch = ks.record_action(action)
        if not allowed_by_switch:
            return PolicyVerdict(
                allowed=False,
                risk_level="blocked",
                rule_triggered="kill_switch",
                message=ks.reason or "Kill switch triggered.",
            )
        return evaluate_action(action, target_text)

    return policy_check_fn


def sanitize_fn(
    elements: list[DOMElement],
    raw_text: str = "",
) -> tuple[list[DOMElement], list[str]]:
    """Scan raw_text and each DOMElement's text with suspicious_content, dropping flagged elements."""
    flags: list[str] = []
    if raw_text:
        raw_rule = suspicious_content(raw_text)
        if raw_rule is not None:
            flags.append(f"raw_text:{raw_rule}")

    clean_elements: list[DOMElement] = []
    for element in elements:
        rule = suspicious_content(element.text)
        if rule is not None:
            flags.append(f"{element.id}:{rule}")
        else:
            clean_elements.append(element)

    return clean_elements, flags


async def install_firewall_fn(context: BrowserContext) -> None:
    """Wrap security.firewall.install_firewall(page) so it covers every page in a BrowserContext."""

    async def _attach(page: Page) -> None:
        if page.is_closed():
            return
        await install_firewall(page)

    def _on_page(page: Page) -> None:
        async def _safe_attach() -> None:
            try:
                await _attach(page)
            except Exception as exc:
                if not page.is_closed():
                    print(f"[FIREWALL WARNING] Failed to attach to new page: {exc}", file=sys.stderr)

        task = asyncio.create_task(_safe_attach())
        _FIREWALL_TASKS.add(task)
        task.add_done_callback(_FIREWALL_TASKS.discard)

    context.on("page", _on_page)
    for existing_page in list(context.pages):
        await _attach(existing_page)


def build_audit_emit_fn(
    audit_path: Path = AUDIT_JSONL_PATH,
) -> tuple[Callable[[AuditEvent], Any], list[Path]]:
    """Use monitor.audit_logger.AuditLogger if available; otherwise append JSON lines to logs/audit.jsonl."""
    try:
        from monitor.audit_logger import AuditLogger  # type: ignore[import-not-found]

        audit_logger = AuditLogger()
        log_paths: list[Path] = [RUN_RESULT_PATH]
        for attr in ("log_path", "file_path", "path"):
            candidate = getattr(audit_logger, attr, None)
            if candidate:
                log_paths.append(Path(candidate))
                break

        async def _emit_with_logger(event: AuditEvent) -> None:
            for method_name in ("emit", "log", "log_event", "write"):
                method = getattr(audit_logger, method_name, None)
                if callable(method):
                    res = method(event)
                    if inspect.isawaitable(res):
                        await res
                    return
            if callable(audit_logger):
                res = audit_logger(event)
                if inspect.isawaitable(res):
                    await res
                return
            raise RuntimeError("AuditLogger does not expose emit/log/log_event/write or __call__.")

        return _emit_with_logger, log_paths
    except ImportError:
        pass

    def audit_emit_fn(event: AuditEvent) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        print(
            f"[AUDIT step={event.step_num}] {event.action.action_type} "
            f"({event.verdict.risk_level}:{event.verdict.rule_triggered}) @ {event.url}"
        )

    return audit_emit_fn, [RUN_RESULT_PATH, audit_path]


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _ensure_portals_running() -> list[Any]:
    """Start any local supplier portals not already listening and return server handles to stop later."""
    portal_specs = (
        ("portals.supplier_a", SUPPLIER_A_PORT),
        ("portals.supplier_b", SUPPLIER_B_PORT),
        ("portals.supplier_c", SUPPLIER_C_PORT),
    )
    missing = [(mod, port) for mod, port in portal_specs if not _is_port_open(HOST, port)]
    if not missing:
        return []

    import importlib
    import uvicorn

    servers: list[Any] = []
    for mod_name, port in missing:
        mod = importlib.import_module(mod_name)
        config = uvicorn.Config(mod.app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append(server)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if all(_is_port_open(HOST, port) for _mod, port in missing):
            break
        time.sleep(0.1)
    return servers


async def main() -> dict[str, Any]:
    task = ShoppingTask(
        items=["Printer Paper"],
        max_budget=1000.0,
        shipping_address=SAMPLE_SHIPPING_ADDRESS,
    )
    ks = KillSwitch(task.max_budget)
    policy_check_fn = build_policy_check_fn(ks)
    audit_emit_fn, log_paths = build_audit_emit_fn()

    started_servers = _ensure_portals_running()
    try:
        result = await run(
            task=task,
            policy_check_fn=policy_check_fn,
            audit_emit_fn=audit_emit_fn,
            sanitize_fn=sanitize_fn,
            install_firewall_fn=install_firewall_fn,
            kill_switch=ks,
        )
    finally:
        for server in started_servers:
            server.should_exit = True

    print("\n=== DEMO RUN SUMMARY ===")
    print(f"Status: {result['status']}")
    print(f"Chosen supplier: {result['chosen_supplier']}")
    print(f"Unsafe suppliers: {result['unsafe_suppliers']}")
    print(f"Total tokens: {result['total_tokens']}")
    print("Log files:")
    for path in log_paths:
        print(f"  - {path.resolve()}")
    return result


if __name__ == "__main__":
    asyncio.run(main())
