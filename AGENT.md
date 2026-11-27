# AGENT.md

## Overview

Ampriba is an autonomous multi-portal procurement agent built for **Track 4**. Given a `ShoppingTask` (requested items, `max_budget`, and `shipping_address`), the agent compares prices and stock across local supplier portals, selects the cheapest safe supplier within budget, prepares checkout, and halts for human approval before final payment.

## Models Used

- **LLM Fallback Planner** ([`agent/planner.py`](agent/planner.py)): Uses `openai.AsyncOpenAI` as an OpenAI-compatible client configured via environment variables:
  - `LLM_BASE_URL`
  - `LLM_API_KEY`
  - `LLM_MODEL` (defaults to `gpt-4o-mini` if unset)
- **Role**: Used **only** as a fallback planner (`next_action`) when deterministic DOM matching cannot determine the next action. Structured outputs are validated against the `BrowserAction` Pydantic schema (with one retry on malformed output).

## Stack

- **Runtime**: Python 3.14
- **Browser Automation**: Playwright (headless Chromium via `agent/browser.py`)
- **Validation & Schemas**: Pydantic v2 (`shared/schemas.py`)
- **Mock Portals**: FastAPI + Uvicorn (`portals/supplier_a.py`, `portals/supplier_b.py`, `portals/supplier_c.py`)

## How a Run Works

`agent/runner.py` executes `run()` in four phases across isolated ephemeral browser contexts:

1. **Phase 1 — Supplier Recon (`_phase1_recon_suppliers`)**: Opens a fresh context per supplier (`SUPPLIER_URLS` A, B, C), installs the network firewall, logs in, searches each item, and extracts price and stock. Suppliers triggering CAPTCHA detection, sanitizer flags, or blocked policy verdicts are marked unsafe and skipped.
2. **Phase 2 — Supplier Selection (`pick_best_supplier`)**: Picks the cheapest safe supplier whose total price for all required items fits within `max_budget` and has positive stock.
3. **Phase 3 — Cart & Checkout (`_phase3_4_checkout`)**: Opens a fresh context on the chosen supplier, logs in, adds each item to the cart, navigates to `/checkout`, and fills shipping fields parsed from `shipping_address`.
4. **Phase 4 — Human Approval Gate**: Submits the final payment button with `is_final_payment=True`, which halts with `status="awaiting_approval"` before clicking pay and writes the summary JSON to `result_path` (default `logs/run_result.json`).

**Single Execution Pipeline & Plain-Code-First Design**:
- Every browser action (`navigate`, `click`, `type`, `extract`, `halt`) passes through one gate (`_execute_pipeline`): kill-switch check → step (`MAX_STEPS = 40`) & repeat (`MAX_ACTION_REPEATS = 3`) guards → page inspection (`_inspect_page_safety`: CAPTCHA + `sanitize_fn`) → `policy_check_fn` → screenshot & `audit_emit_fn` → verdict enforcement → Playwright execution.
- Deterministic element/URL handlers (`_login_supplier`, `_search_query`, `_find_element`, `_pick_add_to_cart_button`) run first; `_llm_fallback_step` is invoked only when deterministic matching finds no match.

## Token and Cost

- **Happy path**: **0 LLM tokens** (all recon, cart, and checkout actions are handled deterministically).
- **Fallback planner call**: **~600 tokens** per call (measured at **591 prompt tokens** and **53 completion tokens**).
- **Hard caps**: Bounded per run by `max_llm_calls` (default `40`) and `MAX_STEPS = 40` total actions.
- **Cost-per-run formula**:

$$\text{Cost}_{\text{run}} = \sum_{i=1}^{N_{\text{calls}}} \left( \frac{P_i}{1{,}000{,}000} \cdot r_{\text{in}} + \frac{C_i}{1{,}000{,}000} \cdot r_{\text{out}} \right)$$

where $N_{\text{calls}} \le \text{max\_llm\_calls}$ is the number of LLM planner calls ($0$ on the happy path), $P_i \approx 591$ and $C_i \approx 53$ are prompt and completion tokens for call $i$, and $r_{\text{in}}, r_{\text{out}}$ are your configured model's rates per 1M input and output tokens.

## Safety Hooks

`agent/runner.py` **never imports** modules from `security/`. Instead, all safety controls are dependency-injected into `run()`:

- **Policy check (`policy_check_fn`)**: Evaluates each action and target text (`safe`, `blocked`, or `requires_approval`) before execution.
- **Sanitizer (`sanitize_fn`)**: Scans extracted DOM elements and raw page text for prompt injections or untrusted instructions; if omitted (`None`), `run()` logs a warning and falls back to `agent.stubs.fake_sanitize`.
- **Network firewall (`install_firewall_fn`)**: Attached to each ephemeral browser context to restrict requests to allowed supplier origins.
- **Kill switch (`kill_switch`)**: Checked before and after policy/audit steps; when triggered, aborts the browser and halts the run.
- **Audit emitter (`audit_emit_fn`)**: Records an `AuditEvent` (with timestamp, URL, action, verdict, screenshot path, and token count) for every step.

[`run_demo.py`](run_demo.py) wires the real implementations from `security/firewall.py`, `security/sanitizer.py`, and `security/kill_switch.py` into `run()`.

## How to Run

1. Start the local supplier portals in one terminal:
   ```bash
   python -m portals.start_portals
   ```
2. Run the end-to-end demo in another terminal:
   ```bash
   python run_demo.py
   ```

## Known Limitations

- **LLM fallback lightly tested**: The deterministic path handles standard portal flows, so `next_action` in `agent/planner.py` has limited end-to-end test coverage.
- **Firewall aborts same-origin 303 redirects**: The browser network firewall can abort legitimate same-origin `303` POST-redirect-GET navigations during form submissions.
- **Local mock portals only**: Configured strictly for the three local FastAPI supplier portals (`localhost:8001`–`8003`), not arbitrary external e-commerce sites.
