"""Full-P&L adjustment engine: parse template formulas into a dependency DAG,
recompute every derived row when a leaf row is edited.

The workbook's row-to-row dependencies live at the *monthly* level: the annual
(J) column is almost always a ``SUM(L..W)``/``AVERAGE(L..W)`` rollup of the 12
monthly columns.  So we recompute monthly values bottom-up, then roll the
annual column from the recomputed months (SUM/AVERAGE) or re-evaluate the
annual formula (RATIO/DERIVED rows like OCC / ADR / RevPAR).

Formula grammar (very small, verified against the manifest):
  cell ref ``L41``, vertical range ``L45:L48``, ``SUM(...)``/``AVERAGE(...)``,
  cross-sheet leaf ref ``Sheet!K22`` (or ``'Sheet'!K22``) — only with an
  explicit verified external constant, ``+ - * /``, ``ROUND(x,n)``,
  ``IF(a=0,0,b)``, ``IFERROR(x,0)``, integer literals.
"""

import re
from decimal import Decimal, ROUND_HALF_UP

from budgeting.excel.money import cents_to_yuan, largest_remainder, yuan_to_cents

RATIO_SCALE = 10_000
MONTH_COLS = "LMNOPQRSTUVW"
ZZ_MONTH_COLS = "FGHIJKLMNOPQ"
ANNUAL_COL = "J"
ZZ_ANNUAL_COL = "S"
MONTHS = [f"{i:02d}" for i in range(1, 13)]

# ``Sheet!Ref`` (quoted or unquoted sheet name, cell or ``cell:cell`` range).
CROSS_REF = re.compile(r"(?:'[^']*'|[A-Za-z0-9_一-鿿()（）\-]+)![A-Z]+\d+(?::[A-Z]+\d+)?")
_EXTERNAL_REF = re.compile(r"(?P<sheet>'[^']*'|[A-Za-z0-9_一-鿿()（）\-]+)!(?P<start>[A-Z]+\d+)(?::(?P<end>[A-Z]+\d+))?")
_CELL = re.compile(r"([A-Z]+)(\d+)")

_TOKEN_RE = re.compile(
    r"""
      (?P<xref>(?:'[^']*'|[A-Za-z0-9_一-鿿()（）\-]+)![A-Z]+\d+(?::[A-Z]+\d+)?)
    | (?P<num>\d+(?:\.\d+)?)
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

    from budgeting.services.template_paths import resolve_template_path

    if template is None:
        from budgeting.services.workflow import active_template

        template = active_template()
    if not template or not template.manifest_path:
        return {"reports": {}}
    path = resolve_template_path(template.manifest_path)
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
    formula = formula.replace("++", "+")
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
        if kind == "xref":
            tokens.append(("xref", val))
        elif kind == "num":
            tokens.append(("num", Decimal(val)))
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
        if t[0] == "xref":
            self.next()
            return ("xref", t[1])
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

_FUNCS = {"SUM", "AVERAGE", "COUNT", "ROUND", "IF", "IFERROR", "ISERROR"}


def _to_decimal(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _excel_round(value, places):
    places = int(_to_decimal(places))
    quantum = Decimal("1").scaleb(-places)
    return _to_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)


def _eval(node, cell_fn, range_fn, external_fn=None):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "xref":
        if external_fn is None:
            raise ValueError("公式包含未验证外部引用：" + node[1])
        return external_fn(node[1])
    if kind == "cell":
        return cell_fn(node[1], node[2])
    if kind == "range":
        return range_fn(node[1], node[2], node[3], node[4])
    if kind == "neg":
        return -_eval(node[1], cell_fn, range_fn, external_fn)
    if kind == "binop":
        op = node[1]
        a = _eval(node[2], cell_fn, range_fn, external_fn)
        b = _eval(node[3], cell_fn, range_fn, external_fn)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b
        if op == "=":
            return Decimal(1) if a == b else Decimal(0)
    if kind == "func":
        name, args = node[1], node[2]
        if name == "SUM":
            return sum(_flatten(_eval(a, cell_fn, range_fn, external_fn) for a in args), Decimal(0))
        if name == "AVERAGE":
            vals = list(_flatten(_eval(a, cell_fn, range_fn, external_fn) for a in args))
            return sum(vals, Decimal(0)) / Decimal(len(vals)) if vals else Decimal(0)
        if name == "COUNT":
            vals = list(_flatten(_eval(a, cell_fn, range_fn, external_fn) for a in args))
            return Decimal(sum(1 for value in vals if value is not None))
        if name == "ROUND":
            return _excel_round(
                _eval(args[0], cell_fn, range_fn, external_fn),
                _eval(args[1], cell_fn, range_fn, external_fn),
            )
        if name == "IF":
            cond = _eval(args[0], cell_fn, range_fn, external_fn)
            return _eval(args[1], cell_fn, range_fn, external_fn) if cond else _eval(args[2], cell_fn, range_fn, external_fn)
        if name == "IFERROR":
            try:
                return _eval(args[0], cell_fn, range_fn, external_fn)
            except (ZeroDivisionError, ValueError):
                return _eval(args[1], cell_fn, range_fn, external_fn)
        if name == "ISERROR":
            try:
                _eval(args[0], cell_fn, range_fn, external_fn)
            except (ZeroDivisionError, ValueError):
                return Decimal(1)
            return Decimal(0)
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


def _as_stored(unit, value):
    if unit == "MONEY":
        return int(_to_decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if unit == "COUNT":
        return int(_to_decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return _to_decimal(value)


def _assert_equal_stored(unit, actual, expected, row_code, period):
    if _as_stored(unit, actual) != _as_stored(unit, expected):
        raise ValueError(
            f"公式勾稽不平：{row_code} {period} 存量={expected}，公式={actual}"
        )


def _external_lookup(external_values):
    external_values = external_values or {}

    def lookup(ref):
        if ref not in external_values:
            raise ValueError("公式包含未验证外部引用：" + ref)
        value = external_values[ref]
        if isinstance(value, (list, tuple)):
            return [_to_decimal(v) for v in value]
        return _to_decimal(value)

    return lookup


def _allocate_year_to_months(row, current_months, target):
    if row["unit"] == "RATIO":
        raise ValueError("比例指标年度值不能自动摊月：" + row["label"])
    target_int = int(_to_decimal(target).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    base_year = sum(int(_to_decimal(current_months[p])) for p in MONTHS)
    delta = target_int - base_year
    allocation = largest_remainder(delta, {p: current_months[p] for p in MONTHS})
    return {p: int(_to_decimal(current_months[p])) + allocation[p] for p in MONTHS}


def _affected_rows(rows, overrides):
    affected = {rc for rc in overrides if rc in rows}
    changed = True
    while changed:
        changed = False
        for rc, row in rows.items():
            if rc not in affected and any(dep in affected for dep in row["deps"]):
                affected.add(rc)
                changed = True
    return affected


def _normal_formula(formula):
    return re.sub(r"\s+", "", str(formula or "").lstrip("=")).upper()


def _rounded_month_sum_formula(row_num, month_cols):
    terms = ",".join(f"ROUND({column}{row_num},2)" for column in month_cols)
    return f"ROUND(SUM({terms}),2)"


def annual_formula_overrides_rollup(row):
    """Whether an annual formula carries semantics beyond normal month rollup."""
    formula = _normal_formula(row.get("annual"))
    if not formula:
        return False
    row_num = row.get("row_num")
    if not row_num:
        return True
    monthly_columns = ZZ_MONTH_COLS if "F" in formula or "Q" in formula else MONTH_COLS
    first, last = monthly_columns[0], monthly_columns[-1]
    simple_rollups = {
        f"SUM({first}{row_num}:{last}{row_num})",
        f"AVERAGE({first}{row_num}:{last}{row_num})",
        f"ROUND(AVERAGE({first}{row_num}:{last}{row_num}),0)",
        _rounded_month_sum_formula(row_num, monthly_columns),
    }
    return formula not in simple_rollups


def external_refs_for_rows(rows, row_codes=None):
    """Return workbook external references used by selected row formulas.

    The result is ordered and de-duplicated.  It is intentionally only a
    reference inventory; callers must still provide explicit values.
    """
    selected = set(row_codes) if row_codes is not None else set(rows)
    refs = []
    for rc, row in rows.items():
        if rc not in selected:
            continue
        for formula in (row.get("monthly") or "", row.get("annual") or ""):
            refs.extend(CROSS_REF.findall(formula))
    return tuple(dict.fromkeys(refs))


def required_external_refs(rows, overrides=None):
    """Return external refs needed for this recompute scope.

    With overrides, only formulas affected by edited rows are considered, so an
    unrelated unknown cross-sheet formula does not block a local edit.  Without
    overrides, all formulas are considered for baseline validation.
    """
    affected = _affected_rows(rows, overrides or {}) if overrides else set(rows)
    return external_refs_for_rows(rows, affected)


def build_external_values(rows, overrides=None, provided=None):
    """Build an explicit ``external_values`` map for ``recompute``.

    ``provided`` is the only accepted source of cross-sheet values.  Missing
    affected references are reported instead of being treated as zero.
    """
    provided = provided or {}
    required = required_external_refs(rows, overrides)
    missing = [ref for ref in required if ref not in provided]
    if missing:
        raise ValueError("公式包含未验证外部引用：" + ",".join(missing))
    return {ref: provided[ref] for ref in required}


def _split_external_ref(ref):
    match = _EXTERNAL_REF.fullmatch(ref)
    if not match:
        raise ValueError("无法解析外部引用：" + str(ref))
    sheet = match.group("sheet")
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1]
    return sheet, match.group("start"), match.group("end")


def _external_cell_sequence(start, end=None):
    if not end:
        return [start]
    start_match = _CELL.fullmatch(start)
    end_match = _CELL.fullmatch(end)
    if not start_match or not end_match:
        raise ValueError("无法解析外部引用区间：" + start + ":" + end)
    start_col, start_row = start_match.group(1), int(start_match.group(2))
    end_col, end_row = end_match.group(1), int(end_match.group(2))
    if start_col != end_col:
        raise ValueError("UNVERIFIED跨表横向区间引用：" + start + ":" + end)
    step = 1 if end_row >= start_row else -1
    return [f"{start_col}{row}" for row in range(start_row, end_row + step, step)]


def _normalized_external_value(value):
    unit = str(value.unit).upper()
    if unit == "RATIO":
        if value.ratio_den:
            return Decimal(value.ratio_num or 0) / Decimal(value.ratio_den)
        return Decimal(value.value_int or 0) / Decimal(RATIO_SCALE)
    return Decimal(value.value_int or 0)


def load_external_values(upload, refs):
    """Load verified cross-sheet values from the upload's normalized cache.

    Provider source: ``NormalizedValue`` rows from the same ``UploadVersion``,
    matched by ``source_sheet`` and ``source_cell``. Missing refs remain
    blocking and are reported as ``UNVERIFIED``.
    """
    refs = tuple(dict.fromkeys(refs or ()))
    if not refs:
        return {}
    try:
        from budgeting.models import NormalizedValue
    except Exception as exc:  # pragma: no cover - import guard for non-Django use
        raise ValueError("UNVERIFIED外部引用：无法读取归一化缓存") from exc

    requested = []
    for ref in refs:
        sheet, start, end = _split_external_ref(ref)
        requested.append((ref, sheet, _external_cell_sequence(start, end)))

    sheets = {sheet for _ref, sheet, _cells in requested}
    cells = {cell for _ref, _sheet, ref_cells in requested for cell in ref_cells}
    queryset = NormalizedValue.objects.filter(upload=upload, source_sheet__in=sheets, source_cell__in=cells)
    by_location = {}
    for value in queryset.order_by("source_sheet", "source_cell", "id"):
        by_location[(value.source_sheet, value.source_cell)] = _normalized_external_value(value)

    result = {}
    missing = []
    for ref, sheet, ref_cells in requested:
        values = []
        for cell in ref_cells:
            key = (sheet, cell)
            if key not in by_location:
                missing.append(ref if len(ref_cells) == 1 else f"{sheet}!{cell}")
                continue
            values.append(by_location[key])
        if len(values) == len(ref_cells):
            result[ref] = values[0] if len(values) == 1 else values
    if missing:
        raise ValueError("UNVERIFIED外部引用缺少归一化来源：" + ",".join(dict.fromkeys(missing)))
    return result


def recompute(rows, num_to_code, values, overrides=None, *, external_values=None, allow_year_allocation=False):
    """Recompute all rows.

    ``values`` / ``overrides``: ``{row_code: {period: numeric}}`` where periods
    are ``01..12`` and ``YEAR``.  A ``YEAR`` override is accepted only when
    ``allow_year_allocation`` is true; that path uses largest-remainder cent
    allocation and is reserved for explicit auxiliary allocation, not formal
    annual target dispatch.  Returns
    ``{row_code: {period: numeric}}``.
    """
    overrides = overrides or {}
    external_fn = _external_lookup(external_values)
    baseline = {rc: {p: _to_decimal(values.get(rc, {}).get(p, 0)) for p in MONTHS} for rc in rows}
    months = {rc: dict(baseline[rc]) for rc in rows}

    for rc, per in overrides.items():
        if rc not in rows:
            raise ValueError("未知行代码：" + rc)
        if "YEAR" in per:
            if not allow_year_allocation:
                raise ValueError("年度目标不会自动摊月；请显式调用辅助分摊后下发月度值")
            months[rc] = _allocate_year_to_months(rows[rc], months[rc], per["YEAR"])
        for p, v in per.items():
            if p != "YEAR":
                months[rc][p] = _to_decimal(v)

    topo = _topo_order(rows)
    affected = _affected_rows(rows, overrides) if overrides else set(rows)

    result = {rc: {} for rc in rows}

    # Monthly pass.
    for rc in topo:
        r = rows[rc]
        for month in MONTHS:
            if r["kind"] == "leaf":
                result[rc][month] = months[rc][month]
                continue
            if rc not in affected:
                result[rc][month] = baseline[rc][month]
                continue
            ast = parse_formula(r["monthly"])
            f_new = _eval(
                ast,
                lambda col, row, month=month: result.get(num_to_code.get(row), {}).get(month, Decimal(0)),
                lambda c1, r1, c2, r2, month=month: _range_values(result, num_to_code, c1, r1, c2, r2, month),
                external_fn,
            )
            value = _as_stored(r["unit"], f_new)
            if not overrides:
                _assert_equal_stored(r["unit"], value, baseline[rc][month], rc, month)
            result[rc][month] = value

    # Annual pass.
    for rc in topo:
        r = rows[rc]
        if rc not in affected and "YEAR" in values.get(rc, {}):
            result[rc]["YEAR"] = _to_decimal(values[rc]["YEAR"])
            continue
        if annual_formula_overrides_rollup(r):
            ast = parse_formula(r["annual"])
            result[rc]["YEAR"] = _as_stored(r["unit"], _eval(
                ast,
                lambda col, row: _annual_cell(rows, num_to_code, result, col, row),
                lambda c1, r1, c2, r2: _annual_range(rows, num_to_code, result, c1, r1, c2, r2),
                external_fn,
            ))
        elif r["kind"] == "leaf" and r["aggregation"] not in ("SUM", "AVERAGE"):
            result[rc]["YEAR"] = _to_decimal(values.get(rc, {}).get("YEAR", 0))
            continue
        elif r["aggregation"] in ("SUM", "AVERAGE"):
            vals = [result[rc][m] for m in MONTHS]
            if r["aggregation"] == "AVERAGE":
                result[rc]["YEAR"] = sum((_to_decimal(v) for v in vals), Decimal(0)) / Decimal(len(vals)) if vals else Decimal(0)
            else:
                result[rc]["YEAR"] = _as_stored(r["unit"], sum((_to_decimal(v) for v in vals), Decimal(0)))
        else:
            ast = parse_formula(r["annual"])
            result[rc]["YEAR"] = _as_stored(r["unit"], _eval(
                ast,
                lambda col, row: _annual_cell(rows, num_to_code, result, col, row),
                lambda c1, r1, c2, r2: _annual_range(rows, num_to_code, result, c1, r1, c2, r2),
                external_fn,
            ))
        if not overrides and "YEAR" in values.get(rc, {}):
            _assert_equal_stored(r["unit"], result[rc]["YEAR"], values[rc]["YEAR"], rc, "YEAR")

    return result


def _range_values(values, num_to_code, c1, r1, c2, r2, month):
    """Resolve a range for the *monthly* pass: vertical (same column) -> rows in
    one month; horizontal (same row) -> one row across months."""
    if c1 == c2:
        return [values.get(num_to_code.get(r), {}).get(month, 0) for r in range(r1, r2 + 1) if r in num_to_code]
    rc = num_to_code.get(r1)
    return [values.get(rc, {}).get(m, 0) for m in MONTHS] if rc else []


def _annual_cell(rows, num_to_code, result, col, row):
    rc = num_to_code.get(row)
    if not rc:
        raise ValueError(f"公式年度引用缺少行映射：{col}{row}")
    if col in {ANNUAL_COL, ZZ_ANNUAL_COL}:
        if "YEAR" not in result.get(rc, {}):
            raise ValueError(f"公式年度引用缺少年度数据：{col}{row}")
        return result[rc]["YEAR"]
    if col in MONTH_COLS or col in ZZ_MONTH_COLS:
        month_cols = ZZ_MONTH_COLS if col == ZZ_MONTH_COLS[0] else MONTH_COLS
        if col not in month_cols:
            raise ValueError(f"公式年度引用列无法判定期间：{col}{row}")
        month = MONTHS[month_cols.index(col)]
        if month not in result.get(rc, {}):
            raise ValueError(f"公式年度引用缺少月度数据：{col}{row}")
        return result[rc][month]
    if "YEAR" not in result.get(rc, {}):
        raise ValueError(f"公式年度引用缺少年度数据：{col}{row}")
    return result[rc]["YEAR"]


def _annual_range(rows, num_to_code, result, c1, r1, c2, r2):
    if r1 == r2:  # horizontal: one row across months
        rc = num_to_code.get(r1)
        if not rc:
            raise ValueError(f"公式年度引用缺少行映射：{c1}{r1}:{c2}{r2}")
        month_cols = ZZ_MONTH_COLS if c1 == ZZ_MONTH_COLS[0] and c2 == ZZ_MONTH_COLS[-1] else MONTH_COLS
        start = month_cols.index(c1) if c1 in month_cols else 0
        end = month_cols.index(c2) if c2 in month_cols else len(month_cols) - 1
        return [result[rc][MONTHS[index]] for index in range(start, end + 1)]
    values = []
    for row in range(r1, r2 + 1):
        rc = num_to_code.get(row)
        if not rc:
            raise ValueError(f"公式年度引用缺少行映射：{c1}{row}")
        if "YEAR" not in result.get(rc, {}):
            raise ValueError(f"公式年度引用缺少年度数据：{c1}{row}")
        values.append(result[rc]["YEAR"])
    return values


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
