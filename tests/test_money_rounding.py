import unittest

from budgeting.excel.money import largest_remainder as money_largest_remainder
from budgeting.excel.money import yuan_to_cents
from budgeting.services.allocations import largest_remainder as allocation_largest_remainder


class MoneyRoundingTests(unittest.TestCase):
    def test_yuan_to_cents_keeps_half_up_decimal_precision(self):
        self.assertEqual(yuan_to_cents("0.05"), 5)
        self.assertEqual(yuan_to_cents("10.01"), 1001)
        self.assertEqual(yuan_to_cents("0.005"), 1)

    def test_dict_largest_remainder_keeps_five_cents_over_twelve_months(self):
        months = {f"{i:02d}": 0 for i in range(1, 13)}
        allocation = money_largest_remainder(5, months)
        self.assertEqual(sum(allocation.values()), 5)
        self.assertEqual([allocation[f"{i:02d}"] for i in range(1, 13)], [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0])

    def test_dict_largest_remainder_keeps_ten_yuan_one_cent_over_twelve_months(self):
        months = {f"{i:02d}": 0 for i in range(1, 13)}
        allocation = money_largest_remainder(1001, months)
        self.assertEqual(sum(allocation.values()), 1001)
        self.assertEqual([allocation[f"{i:02d}"] for i in range(1, 6)], [84, 84, 84, 84, 84])
        self.assertEqual([allocation[f"{i:02d}"] for i in range(6, 13)], [83, 83, 83, 83, 83, 83, 83])

    def test_service_largest_remainder_keeps_total_for_negative_adjustment(self):
        allocation = allocation_largest_remainder(-5, [(f"{i:02d}", 0) for i in range(1, 13)])
        self.assertEqual(sum(cents for _, cents in allocation), -5)
        self.assertEqual([cents for _, cents in allocation[:5]], [-1, -1, -1, -1, -1])


if __name__ == "__main__":
    unittest.main()
