# Safety Layer Integration

The Python safety layer is deterministic enforcement, not planner guidance. Install the browser firewall before untrusted navigation and use a fresh Playwright context with `service_workers="block"`:

```python
context = await browser.new_context(service_workers="block")
page = await context.new_page()
await install_firewall(page)
```

Firewall is installed on the BrowserContext so it covers all pages, popups, and redirects; `install_firewall` accepts a BrowserContext (or a Page for convenience).

`install_firewall` applies to HTTP(S) requests on the page and its context (including popup first requests), blocks WebSockets, aborts redirects, and permits only exact origins in `shared.config.ALLOWED_ORIGINS`. It is Playwright-level enforcement, not a substitute for OS/container egress restrictions. Never add competing routes that continue requests. Fail closed and discard the context if route installation fails. Keep service workers blocked to prevent requests bypassing routing.

Before passing page content to an LLM, use `sanitize_page_text(page)` for visible text and `sanitize_dom(page, elements)` for extracted interactive elements. Both discard hidden text and return flags; suspicious page text quarantines the entire page, and suspicious targets are omitted. Re-evaluate current rendered text for a target immediately before an action; do not trust cached or planner-supplied labels.

For already-extracted `DOMElement` lists without a `Page`, `await sanitize_fn(elements)` applies the same text/metadata classification rules and returns `(clean_elements, flags)`.

Before action execution, call `evaluate_action(action, current_element_text)`. Never execute a blocked verdict. A `requires_approval` verdict is not permission to proceed: stop for explicit human approval of that exact action and target. Run `detect_captcha(page)` after navigation and before interaction; any blocked result means halt, do not solve or bypass the challenge.

Call `KillSwitch.record_action` before each action and trigger `KillSwitch.abort(browser_manager)` on a false return or any safety halt. The switch is thread-safe; abort closes the active context and clears page/session element references. Explicitly bind its budget to the procurement task's authoritative spend total and fail closed if that total is unavailable.

## Kill Switch Contract

- **Singleton access**: Use `get_kill_switch()` to obtain the shared thread-safe `KillSwitch` instance across the runner and monitor (and `reset_kill_switch()` in tests).
- **Latched state**: `.triggered` is a thread-safe boolean property (`True` once killed). `trigger(reason="manual")` latches the switch idempotently and preserves the first reason.
- **Idempotent teardown**: `await abort(browser)` sets `.triggered = True`, closes `browser` (wrapping `browser.close()` in `try`/`except` so `None` or already-closed sessions are safe), and clears active session references.
- **Circuit breakers**: `check_budget(spent, cap)` triggers when `spent > cap`, `check_repeats(action_key)` triggers when the same action key is seen more than 3 times consecutively, and `record_action(action, projected_total)` enforces both checks together.

Run the focused regression suite from the project root with:

```sh
pytest -q security/trap_tests.py
```
