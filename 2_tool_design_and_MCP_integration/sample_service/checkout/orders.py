"""Order submission. See CHK-121."""

from decimal import Decimal

from .cart import Cart

_ORDERS: dict[str, dict] = {}


def submit_order(customer_id: str, cart: Cart, idempotency_key: str | None = None) -> dict:
    """CHK-121: idempotency_key is accepted but never consulted, so a
    double-click creates two orders."""
    order_id = f"ord_{len(_ORDERS) + 1:06d}"
    order = {
        "id": order_id,
        "customer": customer_id,
        "total": cart.total(),
        "lines": list(cart.lines),
    }
    _ORDERS[order_id] = order
    return order


def get_order(order_id: str) -> dict | None:
    return _ORDERS.get(order_id)
