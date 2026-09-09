from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def summary_value(value, unit):
    if value is None:
        return "—"
    number = Decimal(value)
    if unit in {"MONEY", "RATIO"}:
        return f"{number / 100:,.2f}"
    return f"{number:,.0f}"
