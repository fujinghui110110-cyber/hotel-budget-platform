from decimal import Decimal, ROUND_HALF_UP

from budgeting.models import NormalizedValue, ProjectCycle, ValidationIssue
from budgeting.excel.extract import _load_manifest


def terms(positive=(), negative=()):
    return [(row, 1) for row in positive] + [(row, -1) for row in negative]


TOTAL_RULES = {
    32: terms((41, 49, 57, 62)), 44: terms((41,), (42, 43)),
    49: terms(range(45, 49)), 53: terms(range(50, 53)),
    56: terms((49,), (53, 54, 55)), 61: terms((57,), (58, 59, 60)),
    65: terms((62,), (63, 64)), 66: terms((44, 56, 61, 65)),
    71: terms(range(67, 71)), 78: terms(range(72, 78)),
    79: terms((78, 71)), 80: terms((66,), (79,)), 82: terms((80,), (81,)),
    89: terms(range(83, 89)), 90: terms((82,), (89,)),
    99: terms((90, 97), (*range(91, 97), 98)), 101: terms((99,), (100,)),
    104: terms((101, 96)), 105: terms((99, 96, 93)),
}
ZZ_RULES = {
    15: terms((21, 34, 48, 59)), 22: terms(range(23, 27)),
    28: terms((21,), range(23, 28)), 34: terms(range(30, 34)),
    38: terms(range(35, 38)), 39: terms(range(40, 44)),
    45: terms((34,), (38, *range(40, 45))), 50: terms(range(51, 55)),
    56: terms((48,), (49, *range(51, 56))), 62: terms((59,), (60, 61)),
    64: terms((28, 45, 56, 62)), 82: terms(range(72, 82)),
    91: terms(range(85, 91)), 95: terms((93, 94)),
    98: terms((84, 92, 93, 94, 96, 97)), 99: terms((82, 98)),
    101: terms((64,), (99,)), 103: terms((101,), (102,)),
    111: terms(range(105, 111)), 113: terms((103,), (111,)),
    124: terms((113, 121, 123), (*range(115, 121), 122)),
    127: terms((124,), (126,)), 129: terms((124, 120, 117), (123,)),
}


def validate_management_values(upload, run):
    validate_variances(upload, run)
    groups = {}
    for value in NormalizedValue.objects.filter(upload=upload, report_code__startswith='PL_'):
        groups.setdefault((value.report_code, value.period), {})[value.row_code] = value
    for (report, period), rows in groups.items():
        split = report.startswith('PL_ZZ')
        rules = ZZ_RULES if split else TOTAL_RULES
        def get(number):
            return rows.get(f'R{number:04d}')
        def issue(code, actual, expected, message):
            ValidationIssue.objects.get_or_create(
                run=run, code=code, location=f'{actual.source_sheet}!{actual.source_cell} ({actual.row_code}, {period})',
                defaults={'severity': 'P0', 'message': message, 'actual_value': str(actual.value_int), 'expected_value': str(expected)},
            )
        for target, dependencies in rules.items():
            actual = get(target)
            if not actual or actual.unit != 'MONEY' or any(get(row) is None for row, _ in dependencies):
                continue
            expected = sum(get(row).value_int * sign for row, sign in dependencies)
            if actual.value_int != expected:
                issue('PROFIT_RECONCILIATION', actual, expected, '利润/收入分级勾稽不一致（单位：分）；不得使用平衡项。')
        npi_row, reverse_terms = (129, terms((127, 126, 120, 117), (123,))) if split else (105, terms((101, 100, 96, 93)))
        npi = get(npi_row)
        if npi and all(get(row) is not None for row, _ in reverse_terms):
            expected = sum(get(row).value_int * sign for row, sign in reverse_terms)
            if npi.value_int != expected:
                issue('NPI_REVERSE', npi, expected, 'NPI 从最终净利润反算不一致；NPI 不等同经营净利润。')
        revenue, sold, available = (get(21), get(11), get(10)) if split else (get(41), get(28), get(24))
        if sold and available and (sold.value_int < 0 or available.value_int < 0 or sold.value_int > available.value_int):
            issue('ROOM_NIGHTS_CAPACITY', sold, f'0..{available.value_int}', '已售房晚必须在总可售房晚范围内。')
        for price, denominator, label in ((get(13 if split else 30), sold, 'ADR'), (get(14 if split else 31), available, 'RevPAR')):
            if not price or not revenue or not denominator:
                continue
            expected = int((Decimal(revenue.value_int) / denominator.value_int).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if denominator.value_int else 0
            if not denominator.value_int and revenue.value_int:
                issue('PRICE_ZERO_DENOMINATOR', price, '收入为0或分母大于0', f'{label} 分母为零但存在收入。')
            elif price.value_int != expected:
                issue('ROOM_PRICE_RECONCILIATION', price, expected, f'{label} 与收入/房晚不一致（单位：分）。')


def validate_variances(upload, run):
    threshold = upload.cycle.p1_threshold_cents
    if threshold is None:
        return
    baseline = ProjectCycle.objects.filter(
        project=upload.project, cycle=upload.cycle, current_upload__status='APPROVED',
    ).exclude(current_upload=upload).first()
    source = NormalizedValue.objects.filter(upload=baseline.current_upload) if baseline else NormalizedValue.objects.filter(
        upload=upload, data_year=upload.cycle.budget_year - 1, data_kind='FORECAST',
    )
    previous = {(v.report_code, v.row_code, v.month or v.period): v.value_int for v in source}
    amounts = {
        (report, item['row_code'])
        for report, spec in _load_manifest(upload).get('reports', {}).items()
        for item in spec.get('mapping', [])
        if item.get('unit') == 'MONEY' and item.get('aggregation', 'SUM') == 'SUM'
    }
    for value in NormalizedValue.objects.filter(upload=upload, data_kind='BUDGET', month__isnull=False):
        if (value.report_code, value.row_code) not in amounts:
            continue
        prior = previous.get((value.report_code, value.row_code, value.month))
        if prior is None:
            prior = previous.get((value.report_code, value.row_code, value.period))
        if prior is not None and abs(value.value_int - prior) > threshold:
            ValidationIssue.objects.get_or_create(
                run=run, code='AMOUNT_VARIANCE', location=f'{value.source_sheet}!{value.source_cell} ({value.row_code}, {value.period})',
                defaults={'severity': 'P1', 'message': '金额变动超过周期阈值，请说明原因；这不代表预算错误。',
                          'actual_value': str(value.value_int), 'expected_value': str(prior)},
            )
