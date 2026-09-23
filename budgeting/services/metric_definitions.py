"""Versioned definitions from the selected template, never the active template."""
from dataclasses import dataclass
import hashlib
import json
from .template_paths import resolve_template_path


@dataclass(frozen=True)
class MetricDefinition:
    stable_metric_id: str
    row_code: str
    label: str
    unit: str
    aggregation: str
    precision: int
    numerator_row: str | None = None
    denominator_row: str | None = None
    temporal_aggregation: str = "UNKNOWN"


def load_definitions(template, report_code):
    raw = resolve_template_path(template.manifest_path).read_bytes()
    manifest = json.loads(raw)
    if manifest.get("template_version") != template.version:
        raise ValueError("模板清单版本与轮次模板不一致。")
    mapping = manifest.get("reports", {}).get(report_code, {}).get("mapping", [])
    cell_rows = {item.get("cell") or item.get("source", {}).get("cell"): item["row_code"] for item in mapping}
    definitions = {}
    for item in mapping:
        code = item["row_code"]
        aggregation = item.get("aggregation", "UNKNOWN")
        unit = item.get("unit", "UNKNOWN")
        numerator = item.get("numerator_cell") or item.get("ratio_num_cell")
        denominator = item.get("denominator_cell") or item.get("ratio_den_cell")
        # Annual AVERAGE is temporal. Room inventory across hotels is additive.
        company_aggregation = "WEIGHTED" if aggregation in {"RATIO", "DERIVED"} else "SUM" if aggregation in {"SUM", "AVERAGE"} else "UNKNOWN"
        definition = MetricDefinition(f"metric:{report_code}:{code}", code,
            item.get("row_label", code), unit, company_aggregation,
            2 if unit == "MONEY" else 6 if unit == "RATIO" else 0,
            cell_rows.get(numerator), cell_rows.get(denominator), aggregation)
        if code in definitions:
            old = definitions[code]
            if (old.unit, old.aggregation) != (definition.unit, definition.aggregation):
                raise ValueError(f"指标 {code} 的单位或聚合规则冲突。")
        definitions[code] = definition
    return definitions, hashlib.sha256(raw).hexdigest()


def verified_legacy_mapping(upload, cycle, report_code, definitions):
    """Accept only source-bound rehearsal artifacts and explicit standard-name mappings."""
    from django.conf import settings
    import unicodedata
    import re
    from pathlib import Path

    if not cycle.source_budget_year or upload.template.rule_version != 'LEGACY-CACHE-1':
        return None
    try:
        raw = resolve_template_path(upload.template.manifest_path).read_bytes()
        manifest = json.loads(raw)
        valid = (manifest.get('legacy_rehearsal') is True
                 and manifest.get('source_sha256') == upload.sha256
                 and len(upload.sha256) == 64
                 and manifest.get('source_budget_year') == cycle.source_budget_year
                 and manifest.get('budget_year') == cycle.budget_year
                 and manifest.get('year_offset') == cycle.budget_year - cycle.source_budget_year
                 and upload.template.budget_year == cycle.budget_year
                 and upload.template.formula_manifest_hash == upload.sha256)
        if not valid:
            return None
        root = Path(settings.BUDGET_STORAGE_ROOT).resolve()
        source = (root / upload.original_path).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            return None
        digest = hashlib.sha256()
        with source.open('rb') as file:
            for chunk in iter(lambda:file.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != upload.sha256:
            return None
        report = manifest.get('reports', {}).get(report_code, {})
        normalize = lambda value: re.sub(r'[\s%％()（）\-—、:：]+','',unicodedata.normalize('NFKC',str(value)).upper())
        accepted = {}
        for item in report.get('mapping', []):
            definition = definitions.get(item.get('row_code'))
            if not definition or item.get('unit') != definition.unit or normalize(item.get('row_label','')) != normalize(definition.label):
                continue
            if not re.fullmatch(r'[A-Z]+[1-9][0-9]*',item.get('cell','')):
                continue
            period = 'YEAR' if item.get('period') in {'FY','YEAR'} else item.get('period')
            key = (definition.row_code, period)
            entry = (report.get('sheet'),item['cell'],item['row_label'])
            if key in accepted and accepted[key] != entry:
                return None
            accepted[key] = entry
        return {'cells':accepted,'manifest_hash':hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError, AttributeError):
        return None
