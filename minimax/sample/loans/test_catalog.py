import unittest

from catalog import open_loans


ROWS = [
    {"id": "B", "due": "2026-04-09", "returned": False},
    {"id": "A", "due": "2026-04-10", "returned": False},
    {"id": "D", "due": "2026-04-08", "returned": True},
    {"id": "C", "due": "2026-04-11", "returned": False},
]
TODAY = "2026-04-10"


class Target(unittest.TestCase):
    def test_due_today_is_still_on_time(self):
        self.assertEqual(open_loans(ROWS, TODAY, overdue_only=True), ["B"])


class Regression(unittest.TestCase):
    def test_default_preserves_open_register_order(self):
        self.assertEqual(open_loans(ROWS, TODAY), ["B", "A", "C"])

    def test_empty_register(self):
        self.assertEqual(open_loans([], TODAY, overdue_only=True), [])
