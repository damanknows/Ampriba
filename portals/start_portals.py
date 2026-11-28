"""Start all three mock supplier portals and supervise their processes."""

from __future__ import annotations

import importlib
import multiprocessing
import os
import socket
import sys
import time
from typing import Any

from shared.config import SUPPLIER_A_PORT, SUPPLIER_B_PORT, SUPPLIER_C_PORT


SUPPLIERS: tuple[tuple[str, str, int], ...] = (
    ("Supplier A", "portals.supplier_a", SUPPLIER_A_PORT),
    ("Supplier B", "portals.supplier_b", SUPPLIER_B_PORT),
    ("Supplier C", "portals.supplier_c", SUPPLIER_C_PORT),
)


def _serve(module_name: str, port: int) -> None:
    """Import and run one portal inside its dedicated child process."""
    import uvicorn

    module = importlib.import_module(module_name)
    application: Any = getattr(module, "app")
    uvicorn.run(
        application,
        host=os.getenv("AMPRIBA_BIND_HOST", "127.0.0.1"),
        port=port,
        log_level="info",
    )


def _find_port_collision() -> tuple[str, int] | None:
    """Return the first configured port already bound on the local interface."""
    for name, _module, port in SUPPLIERS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Match Uvicorn's wildcard bind so an existing 0.0.0.0 listener
            # cannot slip through as it can when probing only 127.0.0.1.
            probe.bind(("0.0.0.0", port))
        except OSError:
            return name, port
        finally:
            probe.close()
    return None


def _stop_children(children: list[multiprocessing.Process]) -> None:
    """Stop and reap every process, escalating only when graceful exit stalls."""
    active = [child for child in children if child.pid is not None and child.is_alive()]
    for child in active:
        child.terminate()
    for child in children:
        if child.pid is None:
            continue
        child.join(timeout=5)
        if child.is_alive():
            print(f"Process {child.name} did not stop in time; killing it.", flush=True)
            child.kill()
            child.join(timeout=2)
        if child.exitcode is not None:
            print(f"Stopped {child.name} (exit code {child.exitcode}).", flush=True)


def _supervise(children: list[multiprocessing.Process]) -> int:
    """Wait for all portal children, returning failure if any exits unexpectedly."""
    while True:
        for child in children:
            if not child.is_alive():
                child.join()
                print(
                    f"ERROR: {child.name} exited unexpectedly (exit code {child.exitcode}).",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
        time.sleep(0.25)


def main() -> int:
    # Import in the parent first to fail early for missing modules or app variables.
    for name, module_name, port in SUPPLIERS:
        module = importlib.import_module(module_name)
        if not getattr(module, "app", None):
            print(f"ERROR: {module_name} does not expose an 'app'.", file=sys.stderr)
            return 1
        if port <= 0 or port > 65535:
            print(f"ERROR: {name} has invalid configured port {port}.", file=sys.stderr)
            return 1

    collision = _find_port_collision()
    if collision:
        name, port = collision
        print(
            f"ERROR: {name}'s configured port {port} is already in use. "
            "No supplier processes were started.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print("AMPRIBA SUPPLIER PORTAL MANAGER", flush=True)
    for name, _module, port in SUPPLIERS:
        print(f"{name}: http://localhost:{port}", flush=True)
    print("All supplier portals are starting. Press Ctrl+C to stop them.", flush=True)

    context = multiprocessing.get_context("spawn")
    children: list[multiprocessing.Process] = []
    exit_code = 0
    try:
        for name, module_name, port in SUPPLIERS:
            child = context.Process(
                target=_serve,
                args=(module_name, port),
                name=name.replace(" ", "-"),
            )
            child.start()
            children.append(child)
        exit_code = _supervise(children)
    except KeyboardInterrupt:
        print("\nShutdown requested; stopping supplier portals...", flush=True)
    except Exception as exc:
        print(f"ERROR: Could not start portal manager: {exc}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        _stop_children(children)
    return exit_code


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
