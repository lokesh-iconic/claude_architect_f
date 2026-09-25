"""Synthetic support dataset.

Every record here is fabricated: `example.com` addresses, fictional
`555-01xx` numbers, no payment card data. The "raw" records are deliberately
verbose, shaped like what a real order or CRM API returns (scan histories,
warehouse codes, audit trails), because the point of `trimming.py` is to show
how much of that never needs to enter the model's context.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# Fixed clock so refund-window decisions are reproducible.
POLICY_TODAY = date(2026, 9, 25)
REFUND_WINDOW_DAYS = 30
AUTO_REFUND_LIMIT = 500.00
CURRENCY = "USD"


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    qty: int
    unit_price: float


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    status: str
    placed_on: date
    delivered_on: date | None
    items: tuple[Item, ...]
    shipping: float = 0.0
    tax_rate: float = 0.0
    refunded: float = 0.0
    carrier: str = "Example Parcel"

    @property
    def subtotal(self) -> float:
        return round(sum(i.qty * i.unit_price for i in self.items), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.tax_rate, 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.tax + self.shipping, 2)

    @property
    def refundable_until(self) -> date | None:
        return self.delivered_on + timedelta(days=REFUND_WINDOW_DAYS) if self.delivered_on else None


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    postal_code: str
    tier: str
    joined: date
    phone: str


CUSTOMERS: dict[str, Customer] = {
    c.customer_id: c
    for c in [
        Customer("C-1001", "Dana Whitfield", "dana.whitfield@example.com", "94107", "gold",
                 date(2021, 3, 2), "+1-555-0100"),
        Customer("C-1002", "Priya Raman", "priya.raman@example.com", "10001", "standard",
                 date(2023, 11, 19), "+1-555-0101"),
        Customer("C-1003", "Marcus Lee", "marcus.lee@example.com", "60614", "business",
                 date(2019, 7, 8), "+1-555-0102"),
    ]
}

ORDERS: dict[str, Order] = {
    o.order_id: o
    for o in [
        Order("ORD-5521", "C-1001", "delivered", date(2026, 9, 2), date(2026, 9, 6),
              (Item("ESP-200", "Espresso machine", 1, 229.99),), shipping=20.00),
        Order("ORD-5530", "C-1001", "in_transit", date(2026, 9, 15), None,
              (Item("GRD-BURR", "Grinder burr set", 1, 82.00),), shipping=7.50),
        Order("ORD-4410", "C-1001", "delivered", date(2026, 6, 1), date(2026, 6, 4),
              (Item("FRO-10", "Milk frother", 1, 39.00),)),
        Order("ORD-6002", "C-1002", "delivered", date(2026, 9, 10), date(2026, 9, 12),
              (Item("KET-1", "Gooseneck kettle", 1, 64.00),)),
        Order("ORD-6003", "C-1002", "delivered", date(2026, 9, 10), date(2026, 9, 13),
              (Item("KET-1", "Gooseneck kettle", 1, 64.00),)),
        Order("ORD-7100", "C-1003", "delivered", date(2026, 9, 1), date(2026, 9, 4),
              (Item("GRD-PRO", "Commercial grinder", 1, 1420.00),)),
    ]
}


def customer_by_email(email: str) -> Customer | None:
    wanted = email.strip().lower()
    return next((c for c in CUSTOMERS.values() if c.email == wanted), None)


def orders_for(customer_id: str) -> list[Order]:
    return sorted((o for o in ORDERS.values() if o.customer_id == customer_id),
                  key=lambda o: o.placed_on, reverse=True)


# --------------------------------------------------------------------------
# Raw upstream records: what the CRM / order service "really" returns.
# --------------------------------------------------------------------------


def raw_customer(c: Customer) -> dict[str, Any]:
    orders = orders_for(c.customer_id)
    return {
        "id": c.customer_id,
        "object": "customer",
        "profile": {
            "display_name": c.name,
            "email": c.email,
            "email_verified_at": f"{c.joined.isoformat()}T10:04:11Z",
            "phone": c.phone,
            "locale": "en-US",
            "timezone": "America/Los_Angeles",
        },
        "addresses": [
            {"type": t, "line1": "100 Example Street", "city": "Exampleton",
             "postal_code": c.postal_code, "country": "US", "validated": True,
             "geocode": {"lat": 37.0, "lng": -122.0, "precision": "rooftop"}}
            for t in ("billing", "shipping")
        ],
        "loyalty": {
            "tier": c.tier,
            "points": 4120,
            "history": [
                {"event": "points_earned", "points": 50 + i * 5,
                 "at": f"2026-0{1 + i % 8}-1{i % 9}T08:00:00Z", "source": "purchase"}
                for i in range(18)
            ],
        },
        "marketing_preferences": {
            "email": True, "sms": False, "push": False,
            "segments": ["coffee_enthusiast", "early_adopter", "newsletter_weekly"],
        },
        "order_ids": [o.order_id for o in orders],
        "risk": {"score": 0.04, "model": "risk-v7", "evaluated_at": "2026-09-20T00:00:00Z"},
        "devices": [
            {"fingerprint": f"fp_{c.customer_id.lower()}_{n}", "last_seen": "2026-09-2{n}T12:00:00Z",
             "user_agent": "ExampleBrowser/1.0"}
            for n in range(4)
        ],
        "internal_notes": [
            {"author": "system", "at": "2026-05-01T00:00:00Z",
             "note": "Account migrated from legacy CRM; no action needed."},
        ],
        "created_at": f"{c.joined.isoformat()}T10:00:00Z",
        "updated_at": "2026-09-20T00:00:00Z",
    }


def raw_order_header(o: Order) -> dict[str, Any]:
    return {
        "id": o.order_id,
        "object": "order",
        "customer_id": o.customer_id,
        "status": o.status,
        "placed_at": f"{o.placed_on.isoformat()}T15:32:09Z",
        "currency": CURRENCY,
        "amounts": {"subtotal": o.subtotal, "tax": o.tax, "shipping": o.shipping,
                    "total": o.total, "refunded": o.refunded},
        "line_items": [
            {"sku": i.sku, "name": i.name, "qty": i.qty, "unit_price": i.unit_price,
             "warehouse_bin": f"WH2-{i.sku}-B{n}", "supplier_code": f"SUP-{n:04d}",
             "hs_code": "8516.71", "weight_g": 5200, "dimensions_cm": [40, 30, 35],
             "fulfilment_node": "WH2"}
            for n, i in enumerate(o.items)
        ],
        "payment": {"method": "card", "processor": "ExamplePay",
                    "processor_ref": f"pay_{o.order_id.lower()}", "captured": True},
        "fraud_signals": {"avs": "match", "velocity_ok": True, "manual_review": False},
        "audit_log": [
            {"at": f"{o.placed_on.isoformat()}T15:3{n}:00Z", "actor": "system", "event": e}
            for n, e in enumerate(["created", "payment_authorised", "payment_captured",
                                   "released_to_warehouse", "picked", "packed"])
        ],
    }


def raw_shipment(o: Order) -> dict[str, Any]:
    scans = [
        {"at": f"{(o.placed_on + timedelta(days=d)).isoformat()}T0{d % 10}:15:00Z",
         "facility": f"HUB-{100 + d}", "code": code, "description": code.replace("_", " ")}
        for d, code in enumerate(["label_created", "picked_up", "arrived_hub", "departed_hub",
                                  "arrived_hub", "out_for_delivery"])
    ]
    if o.delivered_on:
        scans.append({"at": f"{o.delivered_on.isoformat()}T17:40:00Z", "facility": "LOCAL",
                      "code": "delivered", "description": "delivered, left at front door"})
    return {
        "carrier": o.carrier,
        "tracking_number": f"EX{o.order_id[-4:]}0000US",
        "service_level": "ground",
        "scans": scans,
        "estimated_delivery": (o.placed_on + timedelta(days=12)).isoformat(),
    }


def mutable_orders() -> dict[str, dict[str, Any]]:
    """Per-session copy of refund state, so sessions never leak into each other."""
    return {oid: {"refunded": o.refunded} for oid, o in copy.deepcopy(ORDERS).items()}
