"""Customers and their e-mail addresses."""

import re
from dataclasses import dataclass

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


@dataclass
class Customer:
    id: int
    name: str
    email: str


def valid_email(address: str) -> bool:
    return bool(EMAIL.match(address or ""))


def find_customer(customers: list[Customer], customer_id: int) -> Customer | None:
    for customer in customers:
        if customer.id == customer_id:
            return customer
    return None


def greeting(customers: list[Customer], customer_id: int) -> str:
    customer = find_customer(customers, customer_id)
    if customer is None:
        return "Hello!"
    return f"Hello, {customer.name.split()[0]}!"
