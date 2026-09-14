from decimal import Decimal

from django import template

from budgeting.excel.money import cents_to_yuan
from budgeting.models import REPORTS
from budgeting.services.drivers import driver_value_label, format_cascade_value

register = template.Library()

AUDIT_ACTIONS = {
    "HISTORY_PREVIEW": "历史损益上传预览",
    "HISTORY_CONFIRMED": "确认历史损益数据",
    "ADMIN_BATCH_UPLOAD": "管理员批量上传预算",
    "BATCH_UPLOAD_PREFLIGHT": "批量预算文件预检",
    "BATCH_UPLOAD_ENQUEUED": "批量预算文件已接收",
    "BATCH_UPLOAD_FAILED": "批量预算文件未接收",
    "UPLOAD_RECEIVED": "上传接收",
    "UPLOAD_SUBMITTED": "提交复核",
    "UPLOAD_APPROVED": "批准版本",
    "UPLOAD_REJECTED": "打回版本",
    "ADJUSTMENT_DRAFTED": "创建调整",
    "ADJUSTMENT_LINE_OVERRIDDEN": "覆盖分摊",
    "ADJUSTMENT_ISSUED": "下发调整",
    "ADJUSTMENT_CANCELLED": "取消调整",
    "CYCLE_FROZEN": "周期冻结",
    "CYCLE_REOPENED": "开放修订版",
}


@register.filter
def yuan(cents):
    return f"{cents_to_yuan(cents):,.2f}"


@register.filter
def audit_action(action):
    return AUDIT_ACTIONS.get(action, action)


@register.filter
def report_name(code):
    return REPORTS.get(code, code)


@register.simple_tag
def report_value(values, row, period, units=None):
    value = values.get((row, period))
    if value is None:
        return "—"
    unit = (units or {}).get((row, period))
    if unit == "RATIO":
        return f"{Decimal(value) / Decimal(10_000):.2%}"
    if unit == "COUNT":
        return f"{int(value or 0):,d}"
    return yuan(value)


@register.filter
def cascade(value, unit):
    return format_cascade_value(unit, value)


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return ""
    if isinstance(mapping, dict):
        return mapping.get(key, "")
    try:
        return mapping[int(key)]
    except (IndexError, ValueError, TypeError):
        return ""


@register.filter
def driver_value(value, driver):
    return driver_value_label(driver, value)
