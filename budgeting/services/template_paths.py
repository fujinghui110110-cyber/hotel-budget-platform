import os
from pathlib import Path

from django.conf import settings


def resolve_template_path(value):
    path = Path(value)
    if path.is_absolute():
        return path
    roots = [Path(root) for root in (
        os.getenv("BUDGET_TEMPLATE_ROOT"), os.getenv("BUDGET_INSTALL_ROOT"), str(settings.BASE_DIR)
    ) if root]
    for root in roots:
        candidate = root / path
        if candidate.is_file():
            return candidate
    return roots[0] / path
