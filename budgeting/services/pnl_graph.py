"""Full-P&L adjustment engine: parse template formulas into a dependency DAG,
recompute every derived row when a leaf row is edited.

The workbook's row-to-row dependencies live at the *monthly* level: the annual
(J) column is almost always a ``SUM(L..W)``/``AVERAGE(L..W)`` rollup of the 12
monthly columns.  So we recompute monthly values bottom-up, then roll the
annual column from the recomputed months (SUM/AVERAGE) or re-evaluate the
annual formula (RATIO/DERIVED rows like OCC / ADR / RevPAR).

Formula grammar (very small, verified against the manifest):
  cell ref ``L41``, vertical range ``L45:L48``, ``SUM(...)``/``AVERAGE(...)``,
  cross-sheet leaf ref ``Sheet!K22`` (or ``'Sheet'!K22``) — treated as an
  external constant, ``+ - * /``, ``ROUND(x,n)``, ``IF(a=0,0,b)``,
  ``IFERROR(x,0)``, integer literals.
"""

import re
from decimal import Decimal, ROUND_HALF_UP

from budgeting.excel.money import cents_to_yuan, yuan_to_cents

RATIO_SCALE = 10_000
MONTH_COLS = "LMNOPQRSTUVW"
ZZ_MONTH_COLS = "FGHIJKLMNOPQ"
ANNUAL_COL = "J"
ZZ_ANNUAL_COL = "S"
MONTHS = [f"{i:02d}" for i in range(1, 13)]

# ``Sheet!Ref`` (quoted or unquoted sheet name, cell or ``cell:cell`` range).
CROSS_REF = re.compile(r"(?:'[^']*'|[A-Za-z0-9_一-鿿()（）\-]+)![A-Z]+\d+(?::[A-Z]+\d+)?")
_CELL = re.compile(r"([A-Z]+)(\d+)")

_TOKEN_RE = re.compile(
    r"""
      (?P<num>\d+(?:\.\d+)?)
    | (?P<cell>[A-Z]+\d+(?::[A-Z]+\d+)?)
    | (?P<func>[A-Za-z]+)
    | (?P<op>[+\-*/=])
    | (?P<lp>\()
    | (?P<rp>\))
    | (?P<comma>,)
    """,
    re.VERBOSE,
)


def load_manifest(template=None):
    """Return the manifest dict for ``template`` (or the active template)."""
    import json
    from pathlib import Path

    from django.conf import settings

    if template is None:
        from budgeting.services.workflow import active_template

        template = active_template()
    if not template or not template.manifest_path:
        return {"reports": {}}
    path = Path(template.manifest_path)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    if not path.exists():
        return {"reports": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def build_report_graph(manifest, report_code):
    """Return ``(rows, num_to_code)`` for one report.

    ``rows[rc]`` = ``{label, unit, aggregation, kind, row_num, monthly, annual,
    deps}`` where ``kind`` is ``leaf`` (editable, cross-sheet/input only) or
    ``derived`` (same-sheet arithmetic).  ``num_to_code`` maps the Excel row
    number (``L41`` -> 41) to its ``row_code``.
    """
    report = (manifest.get("reports") or {}).get(report_code)
    if not report:
        raise ValueError("未知报表：" + str(report_code))
    month_cols = ZZ_MONTH_COLS if str(report_code).startswith("PL_ZZ") else MONTH_COLS
    annual_col = ZZ_ANNUAL_COL if str(report_code).startswith("PL_ZZ") else ANNUAL_COL

    rows = {}
    for item in report.get("mapping") or []:
        src = item.get("source") or {}
        cell = src.get("cell") or item.get("cell") or ""
        m = _CELL.match(cell)
        if not m:
            continue
        col, rnum = m.group(1), int(m.group(2))
        rc = item["row_code"]
        r = rows.setdefault(
            rc,
            {
                "label": item.get("row_label") or rc,
                "unit": (item.get("unit") or "MONEY").upper(),
                "aggregation": (item.get("aggregation") or "SUM").upper(),
                "row_num": rnum,
                "monthly": "",
                "annual": "",
                "kind": "leaf",
                "deps": [],
            },
        )
        if col == month_cols[0]:
            r["monthly"] = item.get("formula") or ""
        elif col == annual_col:
            r["annual"] = item.get("formula") or ""

    num_to_code = {r["row_num"]: rc for rc, r in rows.items()}

    for rc, r in rows.items():
        stripped = CROSS_REF.sub("", r["monthly"])
        deps = []
        for m in re.finditer(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", stripped):
            col1, r1 = m.group(1), int(m.group(2))
            col2 = m.group(3)
            r2 = int(m.group(4)) if m.group(4) else None
            if col1 not in annual_col + month_cols:
                continue
            rng = range(r1, r2 + 1) if (r2 is not None and col1 == col2) else [r1]
            for rn in rng:
                dep_rc = num_to_code.get(rn)
                if dep_rc and dep_rc != rc and dep_rc not in deps:
                    deps.append(dep_rc)
        r["kind"] = "derived" if deps else "leaf"
        r["deps"] = deps

    return rows, num_to_code


def _topo_order(rows):
    order = []
    seen = set()
    temp = set()

    def visit(rc):
        if rc in seen:
            return
        if rc in temp:
            raise ValueError("公式依赖存在环：" + rc)
        temp.add(rc)
        for dep in rows[rc]["deps"]:
            visit(dep)
        temp.discard(rc)
        seen.add(rc)
        order.append(rc)

    for rc in rows:
        visit(rc)
    return order


# --- formula parsing -------------------------------------------------------


def _tokenize(formula):
    formula = CROSS_REF.sub("0", formula).replace("++", "+")
    tokens = []
    pos = 0
    while pos < len(formula):
        if formula[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(formula, pos)
        if not m:
            raise ValueError("无法解析公式片段：" + formula[pos : pos + 20])
        pos = m.end()
        kind = m.lastgroup
        val = m.group()
        if kind == "num":
            tokens.append(("num", float(val)))
        elif kind == "cell":
            mm = _CELL.match(val)
            col, row = mm.group(1), int(mm.group(2))
            if ":" in val:
                c2, r2 = _CELL.match(val.split(":")[1]).group(1), int(_CELL.match(val.split(":")[1]).group(2))
                tokens.append(("range", col, row, c2, r2))
            else:
                tokens.append(("cell", col, row))
        elif kind == "func":
            tokens.append(("func", val.upper()))
        elif kind == "op":
            tokens.append(("op", val))
        elif kind == "lp":
            tokens.append(("lp", "("))
        elif kind == "rp":
            tokens.append(("rp", ")"))
        elif kind == "comma":
            tokens.append(("comma", ","))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else ("eof", None)

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self):
        node = self.equality()
        if self.peek()[0] != "eof":
            raise ValueError("公式解析未耗尽：" + repr(self.peek()))
        return node

    def equality(self):
        node = self.expr()
        if self.peek()[0] == "op" and self.peek()[1] == "=":
            self.next()
            node = ("binop", "=", node, self.expr())
        return node

    def expr(self):
        node = self.term()
        while self.peek()[0] == "op" and self.peek()[1] in "+-":
            op = self.next()[1]
            node = ("binop", op, node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.peek()[0] == "op" and self.peek()[1] in "*/":
            op = self.next()[1]
            node = ("binop", op, node, self.factor())
        return node

    def factor(self):
        t = self.peek()
        if t[0] == "op" and t[1] == "-":
            self.next()
            return ("neg", self.factor())
        if t[0] == "op" and t[1] == "+":
            self.next()
            return self.factor()
        if t[0] == "num":
            self.next()
            return ("num", t[1])
        if t[0] == "cell":
            self.next()
            return ("cell", t[1], t[2])
        if t[0] == "range":
            self.next()
            return ("range", t[1], t[2], t[3], t[4])
        if t[0] == "func":
            self.next()
            if self.peek()[0] != "lp":
                raise ValueError("函数缺少括号：" + t[1])
            self.next()
            args = []
            if self.peek()[0] != "rp":
                args.append(self.equality())
                while self.peek()[0] == "comma":
                    self.next()
                    args.append(self.equality())
            if self.peek()[0] != "rp":
                raise ValueError("函数括号不匹配")
            self.next()
            return ("func", t[1], args)
        if t[0] == "lp":
            self.next()
            node = self.expr()
            if self.peek()[0] != "rp":
                raise ValueError("括号不匹配")
            self.next()
            return node
        raise ValueError("意外 token：" + repr(t))


def parse_formula(formula):
    return _Parser(_tokenize(formula)).parse()


# --- evaluation ------------------------------------------------------------

_FUNCS = {"SUM", "AVERAGE", "ROUND", "IF", "IFERROR", "ISERROR"}


def _eval(node, cell_fn, range_fn):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "cell":
        return cell_fn(node[1], node[2])
    if kind == "range":
        return range_fn(node[1], node[2], node[3], node[4])
    if kind == "neg":
        return -_eval(node[1], cell_fn, range_fn)
    if kind == "binop":
        op = node[1]
        a = _eval(node[2], cell_fn, range_fn)
        b = _eval(node[3], cell_fn, range_fn)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b
        if op == "=":
            return 1.0 if a == b else 0.0
    if kind == "func":
        name, args = node[1], node[2]
        if name == "SUM":
            return sum(_flatten(_eval(a, cell_fn, range_fn) for a in args))
        if name == "AVERAGE":
            vals = list(_flatten(_eval(a, cell_fn, range_fn) for a in args))
            return sum(vals) / len(vals) if vals else 0
        if name == "ROUND":
            return round(_eval(args[0], cell_fn, range_fn), int(_eval(args[1], cell_fn, range_fn)))
        if name == "IF":
            cond = _eval(args[0], cell_fn, range_fn)
            return _eval(args[1], cell_fn, range_fn) if cond else _eval(args[2], cell_fn, range_fn)
        if name == "IFERROR":
            try:
                return _eval(args[0], cell_fn, range_fn)
            except (ZeroDivisionError, ValueError):
                return _eval(args[1], cell_fn, range_fn)
        if name == "ISERROR":
            try:
                _eval(args[0], cell_fn, range_fn)
            except (ZeroDivisionError, ValueError):
                return 1.0
            return 0.0
    raise ValueError("无法求值节点：" + repr(node))


def _flatten(seq):
    for v in seq:
        if isinstance(v, (list, tuple)):
            yield from v
        else:
            yield v


def _numeric(value_int, ratio_num, ratio_den):
    """Convert a NormalizedValue's stored fields to a numeric (cents / count /
    ratio-as-float)."""
    if ratio_den:
        return ratio_num / ratio_den
    return value_int


def recompute(rows, num_to_code, values, overrides=None):
    """Recompute all rows.

    ``values`` / ``overrides``: ``{row_code: {period: numeric}}`` where periods
    are ``01..12`` and ``YEAR``.  A ``YEAR`` override on a leaf scales its 12
    months proportionally so the annual matches the target.  Returns
    ``{row_code: {period: numeric}}``.

    Derived rows are evaluated as ``F_same(new) + residual`` where ``residual =
    baseline - F_same(baseline)`` folds every cross-sheet (external) constant
    and any baseline rounding into one number.  This keeps unchanged rows
    *exactly* at their baseline and handles mixed rows like R0075
    (``E3市场销售!K29 - L74 + 明细表!L75``).
    """
    overrides = overrides or {}
    baseline = {rc: {p: values.get(rc, {}).get(p, 0) for p in MONTHS} for rc in rows}
    months = {rc: dict(baseline[rc]) for rc in rows}

    # Apply leaf overrides: month edits directly; a YEAR edit scales months.
    for rc, per in overrides.items():
        if rc not in rows:
            raise ValueError("未知行代码：" + rc)
        if "YEAR" in per:
            target = per["YEAR"]
            base_year = sum(float(months[rc][p]) for p in MONTHS)
            if base_year:
                factor = target / base_year
                months[rc] = {p: int(round(months[rc][p] * factor)) for p in MONTHS}
            else:
                months[rc] = {p: target / 12.0 for p in MONTHS}
        for p, v in per.items():
            if p != "YEAR":
                months[rc][p] = v

    topo = _topo_order(rows)

    # Residual = baseline - F_same(baseline), evaluated against baseline values.
    residual = {rc: {} for rc in rows}
    for rc in topo:
        r = rows[rc]
        if r["kind"] != "derived":
            continue
        ast = parse_formula(r["monthly"])
        for month in MONTHS:
            f_base = _eval(
                ast,
                lambda col, row, month=month: baseline.get(num_to_code.get(row), {}).get(month, 0),
                lambda c1, r1, c2, r2, month=month: _range_values(baseline, num_to_code, c1, r1, c2, r2, month),
            )
            residual[rc][month] = baseline[rc][month] - f_base

    # Annual residual for RATIO/DERIVED rows, so an unchanged row's YEAR stays
    # exactly at baseline even when the seed/annual formula is not self-consistent.
    annual_residual = {rc: 0 for rc in rows}
    for rc in topo:
        r = rows[rc]
        if r["kind"] != "derived":
            continue
        if r["aggregation"] in ("SUM", "AVERAGE"):
            continue
        ast = parse_formula(r["annual"])
        f_base = _eval(
            ast,
            lambda col, row: values.get(num_to_code.get(row), {}).get("YEAR", 0),
            lambda c1, r1, c2, r2: _annual_range(rows, num_to_code, values, c1, r1, c2, r2),
        )
        annual_residual[rc] = values.get(rc, {}).get("YEAR", 0) - f_base

    result = {rc: {} for rc in rows}

    # Monthly pass.
    for rc in topo:
        r = rows[rc]
        for month in MONTHS:
            if r["kind"] == "leaf":
                result[rc][month] = months[rc][month]
                continue
            ast = parse_formula(r["monthly"])
            f_new = _eval(
                ast,
                lambda col, row, month=month: result.get(num_to_code.get(row), {}).get(month, 0),
                lambda c1, r1, c2, r2, month=month: _range_values(result, num_to_code, c1, r1, c2, r2, month),
            )
            value = f_new + residual[rc][month]
            if r["unit"] == "MONEY":
                value = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            result[rc][month] = value

    # Annual pass.
    for rc in topo:
        r = rows[rc]
        if r["kind"] == "leaf" and r["aggregation"] not in ("SUM", "AVERAGE"):
            result[rc]["YEAR"] = values.get(rc, {}).get("YEAR", 0)
            continue
        if r["aggregation"] in ("SUM", "AVERAGE"):
            vals = [result[rc][m] for m in MONTHS]
            if r["aggregation"] == "AVERAGE":
                result[rc]["YEAR"] = sum(vals) / len(vals) if vals else 0
            else:
                result[rc]["YEAR"] = int(round(sum(vals)))
        else:  # RATIO / DERIVED -> re-evaluate annual formula (J-cells) + residual
            ast = parse_formula(r["annual"])
            result[rc]["YEAR"] = _eval(
                ast,
                lambda col, row: result[num_to_code[row]]["YEAR"] if row in num_to_code else 0,
                lambda c1, r1, c2, r2: _annual_range(rows, num_to_code, result, c1, r1, c2, r2),
            ) + annual_residual[rc]

    return result


def _range_values(values, num_to_code, c1, r1, c2, r2, month):
    """Resolve a range for the *monthly* pass: vertical (same column) -> rows in
    one month; horizontal (same row) -> one row across months."""
    if c1 == c2:
        return [values.get(num_to_code.get(r), {}).get(month, 0) for r in range(r1, r2 + 1) if r in num_to_code]
    rc = num_to_code.get(r1)
    return [values.get(rc, {}).get(m, 0) for m in MONTHS] if rc else []


def _annual_range(rows, num_to_code, result, c1, r1, c2, r2):
    if r1 == r2:  # horizontal: one row across months
        rc = num_to_code.get(r1)
        return [result.get(rc, {}).get(m, 0) for m in MONTHS] if rc else []
    return [result.get(num_to_code.get(r), {}).get("YEAR", 0) for r in range(r1, r2 + 1) if r in num_to_code]


def display_value(unit, value):
    """Format a recomputed value for the before/after grid (万元 / 元 / % / 间)."""
    if unit == "RATIO":
        return f"{float(value):.1%}"
    if unit == "MONEY":
        return f"{cents_to_yuan(int(round(value))) / 10000:,.2f}"
    if unit == "COUNT":
        return f"{int(round(value)):,}"
    return f"{value:,.0f}"


def edit_value(unit, value):
    """Engine numeric -> raw display number for an editable input (万元 / 间 / %)."""
    if unit == "RATIO":
        return round(float(value) * 100, 2)
    if unit == "MONEY":
        return float(cents_to_yuan(int(round(value)))) / 10000
    return int(round(value))


def parse_edit(unit, display):
    """Raw display number -> engine numeric (万元->分, %->fraction, 间->int)."""
    if unit == "RATIO":
        return float(display) / 100.0
    if unit == "MONEY":
        return yuan_to_cents(float(display) * 10000)
    return int(round(float(display)))
