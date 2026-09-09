from decimal import Decimal, ROUND_FLOOR


def largest_remainder(delta_cents: int, weighted_items):
    items = [(str(code), abs(int(weight or 0))) for code, weight in weighted_items]
    if not items:
        return []
    total_weight = sum(weight for _, weight in items)
    if total_weight == 0:
        items = [(code, 1) for code, _ in items]
        total_weight = len(items)

    sign = -1 if delta_cents < 0 else 1
    amount = abs(int(delta_cents))
    allocated = []
    floor_sum = 0
    for code, weight in items:
        exact = Decimal(amount) * Decimal(weight) / Decimal(total_weight)
        base = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        allocated.append([code, base, exact - Decimal(base)])
        floor_sum += base

    for row in sorted(allocated, key=lambda r: (-r[2], r[0]))[: amount - floor_sum]:
        row[1] += 1
    return [(code, cents * sign) for code, cents, _ in sorted(allocated, key=lambda r: r[0])]
