"""Prices are in cents (int) everywhere, so money never loses a fraction."""

TAX_RATE = 0.13
TIERS = [(10_000, 0.10), (5_000, 0.05)]  # (order total in cents at or above which, discount)


def round_money(amount: float) -> int:
    """Round half up to whole cents."""
    return int(amount + 0.5)


def tier_discount(total: int) -> float:
    """The discount rate for an order total: the first tier the total reaches."""
    for threshold, rate in TIERS:
        if total >= threshold:
            return rate
    return 0.0


def apply_discount(total: int, rate: float) -> int:
    """The total after a discount rate between 0 and 1."""
    if not 0 <= rate <= 1:
        raise ValueError(f"discount rate {rate} is outside 0..1")
    return round_money(total * (1 - rate))


def with_tax(total: int) -> int:
    return round_money(total * (1 + TAX_RATE))
