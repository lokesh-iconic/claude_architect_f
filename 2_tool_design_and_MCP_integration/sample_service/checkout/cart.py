"""Cart assembly."""

from decimal import Decimal

from .pricing import apply_discounts


class Cart:
    def __init__(self) -> None:
        self.lines: list[tuple[str, int, Decimal]] = []
        self.codes: list[str] = []

    def add(self, sku: str, qty: int, unit_price: Decimal) -> None:
        self.lines.append((sku, qty, unit_price))

    def apply_code(self, code: str) -> None:
        # CHK-104: no exclusivity check happens here either.
        self.codes.append(code)

    def subtotal(self) -> Decimal:
        return sum((qty * price for _, qty, price in self.lines), Decimal("0"))

    def total(self) -> Decimal:
        return apply_discounts(self.subtotal(), self.codes)
