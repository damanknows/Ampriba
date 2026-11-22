from typing import Literal
from pydantic import BaseModel


class ShoppingTask(BaseModel):
    items: list[str]
    max_budget: float
    shipping_address: str


class DOMElement(BaseModel):
    id: int
    tag: str
    text: str
    selector: str


class BrowserAction(BaseModel):
    action_type: Literal["navigate", "click", "type", "extract", "halt"]
    url: str | None = None
    element_id: int | None = None
    selector: str | None = None
    value: str | None = None
    reason: str


class PolicyVerdict(BaseModel):
    allowed: bool
    risk_level: Literal["safe", "requires_approval", "blocked"]
    rule_triggered: str
    message: str


class AuditEvent(BaseModel):
    timestamp: str
    step_num: int
    url: str
    action: BrowserAction
    verdict: PolicyVerdict
    screenshot_path: str | None = None
    tokens_used: int = 0