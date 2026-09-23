REPORT_CODES = (
    "PL_TOTAL_WINE",
    "PL_TOTAL_NOWINE",
    "PL_ZZ_WINE",
    "PL_ZZ_NOWINE",
)


def _rows(total_wine, total_nowine=None, zz_wine=None, zz_nowine=None):
    return {
        "PL_TOTAL_WINE": total_wine,
        "PL_TOTAL_NOWINE": total_wine if total_nowine is None else total_nowine,
        "PL_ZZ_WINE": zz_wine,
        "PL_ZZ_NOWINE": zz_nowine,
    }


_NO_ROWS = {report: None for report in REPORT_CODES}


METRICS = {
    "revenue_total": {
        "label": "酒店总收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0032", zz_wine="R0015", zz_nowine="R0015"),
    },
    "revenue_room": {
        "label": "客房收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0041", zz_wine="R0021", zz_nowine="R0021"),
    },
    "revenue_fb": {
        "label": "餐饮部收入合计",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0049", zz_wine="R0034", zz_nowine="R0034"),
    },
    "revenue_restaurant": {
        "label": "餐厅收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R9001", "R9001", "R9001", "R9001"),
        "source": {"available": True, "section": "B1餐厅汇总", "cell_range": "K24:V24", "note": "餐饮收入的营业点拆分；已包含季节性产品，不与餐饮总额重复相加"},
    },
    "revenue_banquet": {
        "label": "宴会收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R9002", "R9002", "R9002", "R9002"),
        "source": {"available": True, "section": "B2宴会收入", "cell_range": "K24:V24", "note": "B3宴会厅引用B2，取B2一次以避免重复"},
    },
    "revenue_other": {
        "label": "其他经营收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0057", zz_wine="R0048", zz_nowine="R0048"),
    },
    "revenue_rent": {
        "label": "租赁收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0062", zz_wine="R0059", zz_nowine="R0059"),
    },
    "revenue_wine": {
        "label": "名酒收入",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R9003", "R9003", "R9003", "R9003"),
        "source": {"available": True, "section": "名酒明细", "cell_range": "按科目及月度表头识别", "note": "优先读取独立名酒表或明确明细行，补充指标作为后备；已包含在其他收入中，不重复加总或用其他收入全额代替"},
    },
    "cost_wine": {
        "label": "名酒成本",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R9006", "R9006", "R9006", "R9006"),
        "source": {"available": True, "section": "名酒明细", "cell_range": "按科目及月度表头识别", "note": "只读取独立名酒表或明确名酒成本行；已包含在其他销售成本中，缺失时不按收入比例推算"},
    },
    "revenue_seasonal": {
        "label": "季节性产品收入（月饼亭）",
        "group": "revenue",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R9005", "R9005", "R9005", "R9005"),
        "source": {"available": True, "section": "B16月饼亭", "cell_range": "K24:V24", "note": "已包含在餐厅及餐饮收入内；其他季节性产品未单列时需补充确认"},
    },
    "cost_room_operating_per_room": {
        "label": "客房人工及其他费用/已售房晚",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "DERIVED",
        "rows": dict(_NO_ROWS),
        "numerator_rows": _rows("R9004", "R9004", "R9004", "R9004"),
        "denominator_rows": _rows("R0028", zz_wine="R0011", zz_nowine="R0011"),
        "source": {
            "available": True,
            "sheet": "酒店损益总表（含名酒）",
            "formula": "(R0042 + R0043) / R0028",
            "numerator_components": _rows(
                "R0042+R0043",
                zz_wine="R0023+R0024+R0025+R0026+R0027",
                zz_nowine="R0023+R0024+R0025+R0026+R0027",
            ),
            "denominator_rows": _rows("R0028", zz_wine="R0011", zz_nowine="R0011"),
            "provenance": "固定报表无独立每间客房成本行，按客房人工及其他费用合计除以已售房晚计算",
        },
    },
    "cost_room_labor": {
        "label": "客房人工成本",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0042", zz_wine="R0022", zz_nowine="R0022"),
    },
    "cost_food_ratio": {
        "label": "食品成本率",
        "group": "cost",
        "unit": "RATIO",
        "aggregation": "RATIO",
        "rows": _rows("R0050", zz_wine="R0035", zz_nowine="R0035"),
        "numerator_rows": _rows("R0050", zz_wine="R0035", zz_nowine="R0035"),
        "denominator_rows": _rows("R0045", zz_wine="R0030", zz_nowine="R0030"),
    },
    "cost_fb_labor": {
        "label": "餐饮人工成本",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0054", zz_wine="R0039", zz_nowine="R0039"),
    },
    "cost_fb_opex": {
        "label": "餐饮其他费用",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0055", zz_wine="R0044", zz_nowine="R0044"),
    },
    "cost_other_labor": {
        "label": "其他经营部门人工成本",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0059", zz_wine="R0050", zz_nowine="R0050"),
    },
    "cost_other_opex": {
        "label": "其他经营部门其他费用",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0060", zz_wine="R0055", zz_nowine="R0055"),
    },
    "cost_backoffice_labor": {
        "label": "管理职能部门人工成本",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0071", zz_wine="R0067", zz_nowine="R0067"),
    },
    "cost_admin": {
        "label": "行政管理",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0072", zz_wine="R0084", zz_nowine="R0084"),
        "source": {"section": "管理职能部门其他费用"},
    },
    "cost_it": {
        "label": "信息与通信部门",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0073", zz_wine="R0092", zz_nowine="R0092"),
    },
    "cost_sales": {
        "label": "市场销售",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0069", zz_wine="R0095", zz_nowine="R0095"),
    },
    "cost_engineering": {
        "label": "工程维修及保养",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0076", zz_wine="R0096", zz_nowine="R0096"),
        "source": {"section": "管理职能部门其他费用"},
    },
    "cost_energy": {
        "label": "能源",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0077", zz_wine="R0097", zz_nowine="R0097"),
    },
    "cost_energy_ratio": {
        "label": "能源成本率",
        "group": "cost",
        "unit": "RATIO",
        "aggregation": "RATIO",
        "rows": _rows("R0077", zz_wine="R0097", zz_nowine="R0097"),
        "numerator_rows": _rows("R0077", zz_wine="R0097", zz_nowine="R0097"),
        "denominator_rows": _rows("R0032", zz_wine="R0015", zz_nowine="R0015"),
    },
    "cost_owner_expense": {
        "label": "业主费用",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0088", zz_wine="R0110", zz_nowine="R0110"),
    },
    "cost_amortization": {
        "label": "折旧及摊销",
        "group": "cost",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0095", zz_wine="R0119", zz_nowine="R0119"),
        "source": {
            "additional_rows": _rows("R0094", zz_wine="R0118", zz_nowine="R0118"),
            "land_rows": _rows("R0096", zz_wine="R0120", zz_nowine="R0120"),
        },
    },
    "occ": {
        "label": "出租率 OCC",
        "group": "operating",
        "unit": "RATIO",
        "aggregation": "RATIO",
        "rows": _rows("R0029", zz_wine="R0012", zz_nowine="R0012"),
        "numerator_rows": _rows("R0028", zz_wine="R0011", zz_nowine="R0011"),
        "denominator_rows": _rows("R0024", zz_wine="R0010", zz_nowine="R0010"),
    },
    "adr": {
        "label": "平均房价 ADR",
        "group": "operating",
        "unit": "MONEY",
        "aggregation": "DERIVED",
        "rows": _rows("R0030", zz_wine="R0013", zz_nowine="R0013"),
        "numerator_rows": _rows("R0041", zz_wine="R0021", zz_nowine="R0021"),
        "denominator_rows": _rows("R0028", zz_wine="R0011", zz_nowine="R0011"),
    },
    "revpar": {
        "label": "平均每可卖房收入 RevPAR",
        "group": "operating",
        "unit": "MONEY",
        "aggregation": "DERIVED",
        "rows": _rows("R0031", zz_wine="R0014", zz_nowine="R0014"),
        "numerator_rows": _rows("R0041", zz_wine="R0021", zz_nowine="R0021"),
        "denominator_rows": _rows("R0024", zz_wine="R0010", zz_nowine="R0010"),
    },
    "channel_rate": {
        "label": "渠道占比",
        "group": "operating",
        "unit": "RATIO",
        "aggregation": "RATIO",
        "rows": dict(_NO_ROWS),
        "source": {
            "available": False,
            "channel_codes": ("MW", "MT", "MF", "MC", "MD", "MG", "ML", "OTH"),
            "reason": "四张固定管理报表不含渠道率行",
        },
    },
    "profit_gop": {
        "label": "经营毛利润 GOP",
        "group": "profit",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0033", zz_wine="R0016", zz_nowine="R0016"),
    },
    "profit_operating": {
        "label": "酒店经营净利润",
        "group": "profit",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0090", zz_wine="R0113", zz_nowine="R0113"),
    },
    "profit_npi": {
        "label": "NPI（净经营收益）",
        "group": "profit",
        "unit": "MONEY",
        "aggregation": "SUM",
        "rows": _rows("R0105", zz_wine="R0129", zz_nowine="R0129"),
    },
}


def get_metric(code):
    return METRICS.get(code)


def metric_for_row(report, row):
    for metric in METRICS.values():
        if metric["rows"].get(report) == row:
            return metric
    return None


def rolling_years(budget_year):
    year = int(budget_year)
    return [
        (year - 3, "ACTUAL"),
        (year - 2, "ACTUAL"),
        (year - 1, "FORECAST"),
        (year, "BUDGET"),
    ]
