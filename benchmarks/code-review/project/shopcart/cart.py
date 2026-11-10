"""A cart of line items."""

from dataclasses import dataclass, field

from .pricing import apply_discount, tier_discount, with_tax


@dataclass
class Line:
    sku: str
    unit_price: int
    quantity: int

    @property
    def amount(self) -> int:
        return self.unit_price * self.quantity


@dataclass
class Cart:
    lines: list[Line] = field(default_factory=list)

    def add(self, sku: str, unit_price: int, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        for line in self.lines:
            if line.sku == sku:
                line.quantity += quantity
                return
        self.lines.append(Line(sku, unit_price, quantity))

    def remove(self, sku: str) -> None:
        for i, line in enumerate(self.lines):
            if line.sku == sku:
                del self.lines[i]
                return
        raise KeyError(sku)

    def subtotal(self) -> int:
        return sum(line.amount for line in self.lines)

    def total(self) -> int:
        """Subtotal, less the tier discount, plus tax."""
        subtotal = self.subtotal()
        return with_tax(apply_discount(subtotal, tier_discount(subtotal)))
