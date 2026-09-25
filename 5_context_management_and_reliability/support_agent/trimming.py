"""Cut verbose tool output down to the fields the conversation uses.

Each projection is an explicit allowlist rather than a denylist: a field the
upstream adds tomorrow stays out of context until someone decides it
belongs there. The trimmed record is also what `facts.py` reads, so nothing
the case-facts block depends on can be trimmed away by accident -- a test
checks that every fact field survives trimming.
"""

from __future__ import annotations

import json
from typing import Any


def _latest_scan(shipment: dict[str, Any] | None) -> dict[str, Any] | None:
    scans = (shipment or {}).get("scans") or []
    if not scans:
        return None
    last = scans[-1]
    return {"code": last["code"], "at": last["at"][:10], "description": last["description"]}


def trim_customer(raw: dict[str, Any]) -> dict[str, Any]:
    verified = bool(raw.get("verification", {}).get("matched"))
    if not verified:
        # An unverified lookup must not hand the model an id it could refund against.
        return {"verified": False,
                "reason": "email found, but the postal code did not match the account"}
    return {
        "verified": True,
        "customer_id": raw["id"],
        "name": raw["profile"]["display_name"],
        "email": raw["profile"]["email"],
        "tier": raw["loyalty"]["tier"],
        "order_ids": list(raw.get("order_ids", [])),
    }


def trim_order_header(header: dict[str, Any]) -> dict[str, Any]:
    amounts = header["amounts"]
    return {
        "order_id": header["id"],
        "customer_id": header["customer_id"],
        "status": header["status"],
        "placed_on": header["placed_at"][:10],
        "total": amounts["total"],
        "currency": header["currency"],
        "refunded_amount": amounts.get("refunded", 0.0),
        "items": [{"name": li["name"], "qty": li["qty"], "unit_price": li["unit_price"]}
                  for li in header["line_items"]],
    }


def trim_order(raw: dict[str, Any]) -> dict[str, Any]:
    out = trim_order_header(raw)
    out["delivered_on"] = raw["delivered_at"][:10] if raw.get("delivered_at") else None
    out["refundable_until"] = raw.get("refundable_until")
    out["latest_tracking"] = _latest_scan(raw.get("shipment"))
    return out


def trim_refund(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_id": raw["id"],
        "order_id": raw["order_id"],
        "amount": raw["amount"],
        "currency": raw["currency"],
        "status": raw["status"],
        "settlement_eta_days": raw["processor"]["settlement_eta_days"],
    }


def trim_ticket(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": raw["id"],
        "queue": raw["queue"],
        "reason_category": raw["reason_category"],
        "estimated_wait_minutes": raw["estimated_wait_minutes"],
    }


TRIMMERS = {
    "get_customer": trim_customer,
    "lookup_order": trim_order,
    "process_refund": trim_refund,
    "escalate_to_human": trim_ticket,
}


def trim(tool: str, raw: dict[str, Any]) -> dict[str, Any]:
    return TRIMMERS[tool](raw)


def trim_partial(tool: str, steps: list[tuple[str, Any]]) -> dict[str, Any]:
    """Trim whatever a timed-out handler finished, step by step."""
    out: dict[str, Any] = {}
    for step, value in steps:
        if tool == "lookup_order" and step == "order_header":
            out["order_header"] = trim_order_header(value)
        elif tool == "lookup_order" and step == "shipment":
            out["latest_tracking"] = _latest_scan(value)
        elif tool == "get_customer" and step == "profile":
            out["profile_fetched"] = True  # never leak an unverified profile
        else:
            out[step] = "completed"
    return out


def size(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), default=str))
