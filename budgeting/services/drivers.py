from decimal import Decimal, ROUND_HALF_UP

from budgeting.excel.money import cents_to_yuan, yuan_to_cents


RATIO_SCALE = 10_000
DRIVERS = {
    "OCC": "出租率",
    "ADR": "平均房价（单价）",
    "ROOMS": "房间数",
    "ROOM_REV": "客房收入",
}

ROOMS = "R0023"
SELLABLE = "R0024"
SOLD = "R0028"
OCC = "R0029"
ADR = "R0030"
REVPAR = "R0031"
ROOM_REV = "R0041"
TOTAL_REV = "R0032"
GOP = "R0033"
NET = "R0101"
_ALL = (ROOMS, SELLABLE, SOLD, OCC, ADR, REVPAR, ROOM_REV, TOTAL_REV, GOP, NET)


def _decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round_int(value):
    return int(_decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _raw(baseline, code):
    item = baseline.get(code)
    if not item:
        return {"value_int": 0, "ratio_num": 0, "ratio_den": 0, "unit": None}
    return {
        "value_int": int(item.get("value_int") or 0),
        "ratio_num": int(item.get("ratio_num") or 0),
        "ratio_den": int(item.get("ratio_den") or 0),
        "unit": item.get("unit"),
    }


def _money(baseline, code):
    return _raw(baseline, code)["value_int"]


def _count(baseline, code):
    return _raw(baseline, code)["value_int"]


def _ratio(baseline, code):
    item = _raw(baseline, code)
    den = item["ratio_den"]
    if den:
        return float(Decimal(item["ratio_num"]) / Decimal(den))
    return float(Decimal(item["value_int"]) / Decimal(RATIO_SCALE))


def _wan_to_cents(wan):
    return _round_int(_decimal(wan) * Decimal("1000000"))


def _natural_from(baseline, driver):
    if driver == "OCC":
        return round(_ratio(baseline, OCC) * 100, 2)
    if driver == "ADR":
        return float(cents_to_yuan(_money(baseline, ADR)))
    if driver == "ROOMS":
        return _count(baseline, ROOMS)
    if driver == "ROOM_REV":
        return float(cents_to_yuan(_money(baseline, ROOM_REV)) / Decimal("10000"))
    return None


def simulate_driver(baseline, driver, to_value):
    if driver not in DRIVERS:
        raise ValueError(f"未知驱动：{driver}")
    missing = [rc for rc in _ALL if rc not in baseline]
    if missing:
        raise ValueError("缺少年度指标 " + ",".join(missing) + "，无法测算")

    rooms = _count(baseline, ROOMS)
    sellable = _count(baseline, SELLABLE)
    sold = _count(baseline, SOLD)
    occ = _decimal(str(_ratio(baseline, OCC)))
    adr = _money(baseline, ADR)
    revpar = _money(baseline, REVPAR)
    room_rev = _money(baseline, ROOM_REV)
    total_rev = _money(baseline, TOTAL_REV)
    gop = _money(baseline, GOP)
    net = _money(baseline, NET)

    days = _decimal(sellable) / _decimal(rooms) if rooms else _decimal(sellable or 365)

    def cdiv(num, den):
        return _round_int(_decimal(num) / _decimal(den)) if den else 0

    if driver == "OCC":
        occ2 = _decimal(to_value) / Decimal("100")
        if not Decimal("0") <= occ2 <= Decimal("1"):
            raise ValueError("出租率需在 0-100 之间")
        sold2 = min(sellable, max(0, _round_int(_decimal(sellable) * occ2)))
        adr2, rooms2, sellable2 = adr, rooms, sellable
        room_rev2 = sold2 * adr2
        revpar2 = cdiv(room_rev2, sellable2)
    elif driver == "ADR":
        adr2 = yuan_to_cents(to_value)
        if not adr2 or adr2 <= 0:
            raise ValueError("平均房价需大于 0")
        rooms2, sellable2, sold2, occ2 = rooms, sellable, min(sold, sellable), occ
        room_rev2 = sold2 * adr2
        revpar2 = cdiv(room_rev2, sellable2)
    elif driver == "ROOMS":
        rooms2 = _round_int(to_value)
        if rooms2 <= 0:
            raise ValueError("房间数需大于 0")
        sellable2 = max(0, _round_int(_decimal(rooms2) * days))
        occ2, adr2 = occ, adr
        sold2 = min(sellable2, max(0, _round_int(_decimal(sellable2) * occ2)))
        room_rev2 = sold2 * adr2
        revpar2 = cdiv(room_rev2, sellable2)
    else:
        room_rev2 = _wan_to_cents(to_value)
        if room_rev2 < 0:
            raise ValueError("客房收入不得为负数")
        rooms2, sellable2, sold2, occ2 = rooms, sellable, min(sold, sellable), occ
        adr2 = cdiv(room_rev2, sold2)
        revpar2 = cdiv(room_rev2, sellable2)

    delta = room_rev2 - room_rev
    return {
        "driver": driver,
        "driver_label": DRIVERS[driver],
        "from_value": _natural_from(baseline, driver),
        "to_value": float(to_value) if driver != "ROOMS" else _round_int(to_value),
        "delta_room_rev": delta,
        "rows": [
            ("ROOMS", "房间数", "COUNT", rooms, rooms2),
            ("SELLABLE", "总可卖房", "COUNT", sellable, sellable2),
            ("SOLD", "已售房", "COUNT", sold, sold2),
            ("OCC", "出租率", "RATIO", float(occ), float(occ2)),
            ("ADR", "平均房价", "YUAN", adr, adr2),
            ("REVPAR", "RevPAR", "YUAN", revpar, revpar2),
            ("ROOM_REV", "客房收入", "MONEY", room_rev, room_rev2),
            ("TOTAL_REV", "酒店总收入", "MONEY", total_rev, total_rev + delta),
            ("GOP", "经营毛利润", "MONEY", gop, gop + delta),
            ("NET", "净利润", "MONEY", net, net + delta),
        ],
    }


def format_cascade_value(unit, value):
    if unit == "RATIO":
        return f"{float(value):.1%}"
    if unit == "MONEY":
        return f"{cents_to_yuan(value) / Decimal('10000'):,.2f}"
    if unit == "YUAN":
        return f"{cents_to_yuan(value):,.0f}"
    return f"{int(value):,}"


def driver_value_label(driver, value):
    if value in (None, ""):
        return "—"
    if driver == "OCC":
        return f"{float(value):.1f}%"
    if driver == "ADR":
        return f"{float(value):,.0f} 元"
    if driver == "ROOMS":
        return f"{int(float(value)):,} 间"
    return f"{float(value):,.2f} 万元"
