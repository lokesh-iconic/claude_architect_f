"""Order pricing. See CHK-104 and CHK-118."""

from decimal import Decimal

DISCOUNTS = {"SAVE10": Decimal("0.10"), "WELCOME15": Decimal("0.15")}


def apply_discounts(subtotal: Decimal, codes: list[str]) -> Decimal:
    """CHK-104: this sums every code instead of taking the best one, so two
    codes stack into a larger discount than either is worth."""
    total_rate = sum(DISCOUNTS.get(code, Decimal("0")) for code in codes)
    return subtotal * (Decimal("1") - total_rate)


def split_payment(total: Decimal, ways: int) -> list[Decimal]:
    """CHK-118: rounds each share independently, so the shares can miss the
    total by a cent."""
    share = (total / ways).quantize(Decimal("0.01"))
    return [share] * ways
