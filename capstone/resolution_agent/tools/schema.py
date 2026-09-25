"""Tool contracts: the full JSON schemas, their API-safe copies, and a validator.

Each tool has one schema, and it is the contract. For the two tools that
change money or case state (`process_refund`, `escalate_to_human`) the input
*is* the action record: what the agent did and why.

Strict tool use (`strict: true`) guarantees the model's input matches the
schema's shape, but strict mode does not accept string or numeric
constraints (`minLength`, `pattern`, `exclusiveMinimum`). `api_tool_specs()`
therefore sends a copy with those keywords stripped, and `validate()` enforces
the full contract client-side, before any handler runs. That gap is why the
validation-retry loop exists even with strict mode on.

The validator is a deliberately small subset of JSON Schema -- exactly the
keywords these schemas use -- rather than a new dependency.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass
from typing import Any

from .data import CURRENCY
from .handlers import ESCALATION_REASONS

ACTION_TOOLS = ("process_refund", "escalate_to_human")
REFUND_TYPES = ["full", "partial"]
REFUND_REASON_CODES = [
    "damaged_item", "defective", "not_as_described", "wrong_item", "changed_mind", "late_delivery", "other",
]

CUSTOMER_ID = r"^C-\d{4}$"
ORDER_ID = r"^ORD-\d{4}$"

# Keywords strict tool use rejects or ignores; enforced by `validate()` instead.
CLIENT_SIDE_KEYWORDS = frozenset({
    "minLength", "maxLength", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems",
})


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


GET_CUSTOMER = _obj({
    "email": {"type": "string", "minLength": 3, "description": "Email address on the account, as the customer gave it."},
    "postal_code": {"type": "string", "pattern": r"^\d{5}$", "description": "Five-digit postal code on the account."},
})

LOOKUP_ORDER = _obj({
    "order_id": {"type": "string", "pattern": ORDER_ID, "description": "Order number, e.g. ORD-5521."},
})

PROCESS_REFUND = _obj({
    "customer_id": {"type": "string", "pattern": CUSTOMER_ID,
                    "description": "The customer_id get_customer returned with verified: true in this conversation."},
    "order_id": {"type": "string", "pattern": ORDER_ID,
                 "description": "The order being refunded. It must already have been fetched with lookup_order."},
    "amount": {"type": "number", "exclusiveMinimum": 0,
               "description": "Amount to return, no more than the order total less anything already refunded."},
    "currency": {"type": "string", "enum": [CURRENCY], "description": "The order's currency, from the case facts."},
    "refund_type": {"type": "string", "enum": REFUND_TYPES,
                    "description": "full if amount equals the whole refundable remainder, otherwise partial."},
    "reason_code": {"type": "string", "enum": REFUND_REASON_CODES,
                    "description": "Why the customer is owed money back."},
    "customer_statement": {"type": "string", "minLength": 3,
                           "description": "What the customer said about the problem, in their words."},
    "justification": {"type": "string", "minLength": 20,
                      "description": "Why this action and this amount are right under the refund policy, "
                                     "citing the case facts used (total, already refunded, window)."},
})

ESCALATE_TO_HUMAN = _obj({
    "reason_category": {"type": "string", "enum": list(ESCALATION_REASONS),
                        "description": "Which escalation criterion applies."},
    "customer_id": {"type": ["string", "null"], "pattern": CUSTOMER_ID,
                    "description": "The verified customer_id from the case facts; null only if nobody is verified."},
    "identity_verified": {"type": "boolean", "description": "Whether get_customer verified the customer."},
    "order_ids": {"type": "array", "items": {"type": "string", "pattern": ORDER_ID},
                  "description": "Orders the case is about; empty if none."},
    "customer_request": {"type": "string", "minLength": 10,
                         "description": "What the customer wants, in one or two sentences."},
    "root_cause": {"type": "string", "minLength": 20,
                   "description": "Why the agent cannot resolve it: the specific gap, rejection or conflict."},
    "recommended_action": {"type": "string", "minLength": 20,
                           "description": "The concrete next step for the human, e.g. what to check and what to do "
                                          "if it is confirmed. Not 'please review'."},
    "actions_taken": {"type": "array", "items": {"type": "string"},
                      "description": "What the agent already did, e.g. 'lookup_order ORD-5530: in_transit'."},
})

SCHEMAS: dict[str, dict[str, Any]] = {
    "get_customer": GET_CUSTOMER,
    "lookup_order": LOOKUP_ORDER,
    "process_refund": PROCESS_REFUND,
    "escalate_to_human": ESCALATE_TO_HUMAN,
}

DESCRIPTIONS: dict[str, str] = {
    "get_customer": (
        "Verify who the customer is from the email and postal code on their account. Returns verified: true "
        "with customer_id, name, email, tier and order_ids only when both match; otherwise verified: false and "
        "no id. Use it before any refund and whenever you need the account holder. It returns no order "
        "details and changes nothing."
    ),
    "lookup_order": (
        "Fetch one order by its ORD- number: status, dates, total, shipping, items, amount already refunded, "
        "refund-window end date and latest tracking scan. Read-only: it does not verify identity and never "
        "moves money."
    ),
    "process_refund": (
        "Return money for a delivered order to the original payment method, and record exactly what was "
        "refunded and why. Needs a customer_id verified by get_customer in this conversation, and an order "
        "already fetched in it. Not for price adjustments, compensation, goodwill credits, account changes, "
        "or anything the refund policy rejects (outside the 30-day window, above the auto-approval limit): "
        "those go to a person."
    ),
    "escalate_to_human": (
        "Hand the case to a human with a structured handoff: customer_id, root cause and a concrete recommended "
        "action. Use it when the customer asks for a person, when the request is outside the refund policy or "
        "your tools (including any account change), when a tool rejects a refund on policy grounds, or when "
        "you cannot make progress. The system attaches the case facts and recent errors. It is not a way to "
        "finish a request another tool can complete."
    ),
}


def tool_specs() -> list[dict[str, Any]]:
    """Full contracts, as the harness and the MCP server describe them."""
    return [{"name": name, "description": DESCRIPTIONS[name], "input_schema": copy.deepcopy(schema)}
            for name, schema in SCHEMAS.items()]


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in CLIENT_SIDE_KEYWORDS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def api_tool_specs() -> list[dict[str, Any]]:
    """What the Messages API receives: strict, with client-side-only keywords removed."""
    return [{**spec, "input_schema": _strip(spec["input_schema"]), "strict": True} for spec in tool_specs()]


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    field: str
    code: str
    message: str

    def to_payload(self) -> dict[str, str]:
        return asdict(self)


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate(schema: dict[str, Any], value: Any, path: str = "") -> list[Issue]:
    where = path or "(input)"
    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else list(types)
        if not any(_TYPES[t](value) for t in allowed):
            return [Issue(where, "wrong_type", f"expected {' or '.join(allowed)}, got {type(value).__name__}")]
    issues: list[Issue] = []
    if "enum" in schema and value not in schema["enum"]:
        issues.append(Issue(where, "not_in_enum", f"{value!r} is not one of {schema['enum']}"))
    if isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            issues.append(Issue(where, "too_short", f"needs at least {schema['minLength']} characters"))
        if "pattern" in schema and not re.fullmatch(schema["pattern"].strip("^$"), value):
            issues.append(Issue(where, "bad_format", f"{value!r} does not match {schema['pattern']}"))
    if _TYPES["number"](value) and "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
        issues.append(Issue(where, "out_of_range", f"must be greater than {schema['exclusiveMinimum']}"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                issues.append(Issue(f"{path}.{name}".lstrip("."), "missing", "required field is missing"))
        if schema.get("additionalProperties") is False:
            for name in sorted(set(value) - set(props)):
                issues.append(Issue(f"{path}.{name}".lstrip("."), "unexpected_field", "not part of the contract"))
        for name, sub in props.items():
            if name in value:
                issues.extend(validate(sub, value[name], f"{path}.{name}".lstrip(".")))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            issues.extend(validate(schema["items"], item, f"{where}[{i}]"))
    return issues


def validate_input(tool: str, args: dict[str, Any]) -> list[Issue]:
    return validate(SCHEMAS[tool], args)
