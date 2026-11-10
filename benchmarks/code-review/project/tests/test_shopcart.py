import json
import tempfile
import unittest
from pathlib import Path

from shopcart.cart import Cart
from shopcart.customers import Customer, greeting, valid_email
from shopcart.inventory import Inventory, OutOfStock
from shopcart.pricing import apply_discount, round_money, tier_discount


class PricingTest(unittest.TestCase):
    def test_round_and_discount(self):
        self.assertEqual(round_money(10.5), 11)
        self.assertEqual(apply_discount(1000, 0.1), 900)
        self.assertEqual(tier_discount(10_000), 0.10)
        self.assertEqual(tier_discount(4_999), 0.0)


class CartTest(unittest.TestCase):
    def test_total(self):
        cart = Cart()
        cart.add("a", 6_000)
        cart.add("b", 1_000, 2)
        cart.remove("b")
        self.assertEqual(cart.subtotal(), 6_000)
        self.assertEqual(cart.total(), round_money(6_000 * 0.95 * 1.13))


class InventoryTest(unittest.TestCase):
    def test_reserve_release_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock.json"
            path.write_text(json.dumps({"a": 3}), encoding="utf-8")
            inv = Inventory.load(path)
        inv.reserve("a", 2)
        self.assertRaises(OutOfStock, inv.reserve, "a", 2)
        inv.release("a", 2)
        self.assertEqual(inv.stock["a"], 3)


class CustomerTest(unittest.TestCase):
    def test_email_and_greeting(self):
        self.assertTrue(valid_email("a@b.co"))
        self.assertFalse(valid_email("nope"))
        self.assertEqual(greeting([Customer(1, "Ada Lovelace", "a@b.co")], 1), "Hello, Ada!")
        self.assertEqual(greeting([], 2), "Hello!")


if __name__ == "__main__":
    unittest.main()
