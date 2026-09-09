from decimal import Decimal, ROUND_HALF_UP


def yuan_to_cents(value) -> int:
    if value in (None, ""):
        return 0
    amount = Decimal(str(value))
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_yuan(cents: int) -> Decimal:
    return Decimal(cents) / Decimal("100")


def aggregate_occ(parts):
    num = sum(int(n or 0) for n, _ in parts)
    den = sum(int(d or 0) for _, d in parts)
    return num, den, Decimal(num) / Decimal(den) if den else Decimal("0")
