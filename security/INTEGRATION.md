# Kill Switch & Trace Integration Contract (Member 4 / Monitor)

Strict boundary note: this document specifies the interface exposed by `security/kill_switch.py` and `shared/schemas.py` for `monitor/` and runner integration. Do not implement the logger here.

## 1. Import Path

```python
from security.kill_switch import get_kill_switch
```

## 2. `/api/kill` Handler (`monitor/server.py`)

In `monitor/server.py`, the `/api/kill` handler must:

1. Obtain the shared singleton: `ks = get_kill_switch()`.
2. Latch the kill switch: `ks.trigger("dashboard")`.
3. Broadcast `{"state": "KILLED", "reason": "dashboard"}` over the WebSocket.

```python
ks = get_kill_switch()
ks.trigger("dashboard")
await broadcast({"state": "KILLED", "reason": "dashboard"})
```

## 3. `/api/status` Endpoint

If `monitor/server.py` exposes `/api/status`, it should return:

```python
{"triggered": ks.triggered}
```

## 4. State & Thread-Safety Guarantees

- `triggered` is a read-only `@property` returning a `bool` (`False` until triggered, `True` once latched).
- `trigger(reason: str = "manual")` is idempotent and preserves the first trigger reason.
- `abort(browser)` is safe to call from a different thread or event loop (handles `None` or already-closed browsers and swallows close errors); all kill switch state is protected by `threading.Lock`.

## 5. Runner Trace Fields (`AuditEvent` → `trace.md`)

For each step, the runner should log the fields matching `shared.schemas.AuditEvent` so `trace.md` displays:

| `trace.md` Column | `AuditEvent` Field | Schema Type |
| --- | --- | --- |
| Step # | `step_num` | `int` |
| URL | `url` | `str` |
| Action | `action` | `BrowserAction` (`action_type`, `url`, `element_id`, `selector`, `value`, `reason`) |
| PolicyVerdict | `verdict` | `PolicyVerdict` (`allowed`, `risk_level`, `rule_triggered`, `message`) |
| Screenshot path | `screenshot_path` | `str \| None` (default `None`) |

(`AuditEvent` also carries `timestamp: str` and `tokens_used: int = 0`.)
