"""Stock levels, reserved when an order is placed and released when it is cancelled."""

import json
from pathlib import Path


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self, stock: dict[str, int] | None = None):
        self.stock = dict(stock or {})

    @classmethod
    def load(cls, path: Path) -> "Inventory":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def reserve(self, sku: str, quantity: int) -> None:
        available = self.stock.get(sku, 0)
        if quantity > available:
            raise OutOfStock(f"{sku}: {quantity} wanted, {available} left")
        self.stock[sku] = available - quantity

    def release(self, sku: str, quantity: int) -> None:
        self.stock[sku] = self.stock.get(sku, 0) + quantity
