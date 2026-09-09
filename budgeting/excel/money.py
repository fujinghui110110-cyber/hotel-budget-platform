from decimal import Decimal, ROUND_HALF_UP


def yuan_to_cents(value):
    if value in (None, ""):
        return None
    return int((Decimal(str(value)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_yuan(cents):
    return Decimal(cents or 0) / Decimal("100")


def largest_remainder(delta_cents, weights_by_code):
    codes = sorted(weights_by_code)
    total_weight = sum(abs(int(weights_by_code[c])) for c in codes)
    if total_weight == 0:
        weights = {c: Decimal(1) for c in codes}
        total_weight = len(codes)
    else:
        weights = {c: Decimal(abs(int(weights_by_code[c]))) for c in codes}
    sign = -1 if delta_cents < 0 else 1
    amount = abs(int(delta_cents))
    floors = {}
    remainders = []
    for code in codes:
        raw = Decimal(amount) * weights[code] / Decimal(total_weight)
        floor = int(raw)
        floors[code] = floor
        remainders.append((raw - Decimal(floor), code))
    remaining = amount - sum(floors.values())
    for _, code in sorted(remainders, key=lambda x: (-x[0], x[1]))[:remaining]:
        floors[code] += 1
    return {code: sign * cents for code, cents in floors.items()}
