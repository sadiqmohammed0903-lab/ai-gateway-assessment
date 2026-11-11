"""In-memory mock customer store."""

import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal


class CustomerNotFoundError(Exception):
    pass


class RefundExceedsBalanceError(Exception):
    pass


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    balance: Decimal


def _seed() -> dict[str, Customer]:
    rows = [
        ("CUST-10001", "Ada Lovelace", "ada@example.com", "250.00"),
        ("CUST-10002", "Alan Turing", "alan@example.com", "1200.50"),
        ("CUST-10003", "Grace Hopper", "grace@example.com", "75.25"),
        ("CUST-10004", "Linus Torvalds", "linus@example.com", "0.00"),
        ("CUST-10005", "Margaret Hamilton", "margaret@example.com", "5000.00"),
        ("CUST-10006", "Dennis Ritchie", "dennis@example.com", "42.00"),
    ]
    return {cid: Customer(cid, name, email, Decimal(bal)) for cid, name, email, bal in rows}


class CustomerStore:
    def __init__(self) -> None:
        self._customers = _seed()
        self._lock = threading.Lock()

    def get(self, customer_id: str) -> Customer:
        try:
            return self._customers[customer_id]
        except KeyError:
            raise CustomerNotFoundError(customer_id) from None

    def refund(self, customer_id: str, amount: Decimal) -> tuple[str, Decimal]:
        with self._lock:
            customer = self.get(customer_id)
            if amount > customer.balance:
                raise RefundExceedsBalanceError(customer_id)
            customer.balance -= amount
            return f"RF-{uuid.uuid4().hex[:8].upper()}", customer.balance
