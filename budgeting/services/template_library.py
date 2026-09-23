"""Version-scoped administrator workbooks; independent of calculation rules."""
import hashlib
from pathlib import Path
import uuid
import zipfile
import tempfile
from types import SimpleNamespace
from openpyxl.utils.exceptions import InvalidFileException
from django.core import signing

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import load_workbook

from budgeting.models import AuditEvent, BudgetCycle, BudgetTemplateFile


def get_distribution_template(cycle):
    if not cycle:
        return None
    return BudgetTemplateFile.objects.filter(cycle=cycle).order_by('-created_at', '-pk').first()


def distribution_template_path(item):
    return Path(settings.BUDGET_STORAGE_ROOT) / item.file_path


def publish_template(cycle, uploaded, actor, name=''):
    if actor.role != 'ADMIN':
        raise ValidationError('只有管理员可以发布填报模板。')
    if len(name.strip()) > 120:
        raise ValidationError('模板名称不能超过 120 个字。')
    suffix = Path(uploaded.name).suffix.lower()
    if suffix not in ('.xlsx', '.xlsm'):
        raise ValidationError('请选择 Excel 工作簿（.xlsx 或 .xlsm）。')
    if uploaded.size > 30 * 1024 * 1024:
        raise ValidationError('模板不能超过 30 MB。')
    original_name = Path(uploaded.name.replace('\\', '/')).name
    relative = Path('budget_templates') / str(cycle.pk) / f'{uuid.uuid4().hex}{suffix}'
    path = Path(settings.BUDGET_STORAGE_ROOT) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        digest = hashlib.sha256()
        with path.open('wb') as destination:
            for chunk in uploaded.chunks():
                digest.update(chunk)
                destination.write(chunk)
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 150 * 1024 * 1024:
                raise ValidationError('工作簿内容过大，请精简后上传。')
        validate_distribution_workbook(cycle, path)
        workbook = load_workbook(path, read_only=True, data_only=False)
        workbook.close()
        with transaction.atomic():
            locked = BudgetCycle.objects.select_for_update().get(pk=cycle.pk)
            if locked.status == BudgetCycle.FROZEN:
                raise ValidationError('该预算版本已归档，不能更换模板。')
            if locked.template_id != cycle.template_id:
                raise ValidationError('该版本的汇总规则已更新，请刷新页面后重新发布。')
            item = BudgetTemplateFile.objects.create(
                cycle=locked, name=name.strip() or original_name,
                original_name=original_name, file_path=relative.as_posix(),
                sha256=digest.hexdigest(), uploaded_by=actor,
            )
            AuditEvent.objects.create(actor=actor, cycle=locked, action='TEMPLATE_PUBLISHED',
                                      payload={'template_file_id': item.pk, 'name': item.name, 'sha256': item.sha256})
        return item
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, ValidationError):
            raise
        if isinstance(exc, (zipfile.BadZipFile, KeyError, ValueError, OSError, SyntaxError, InvalidFileException)):
            raise ValidationError('无法读取此工作簿，请用 Excel 重新保存后上传。') from exc
        raise


def validate_distribution_workbook(cycle, path):
    """Ensure a published workbook can return through the normal upload checks."""
    from budgeting.excel.ooxml import validate_upload_contract, validate_xlsx_zip
    from budgeting.services.template_delivery import _inject_sys_meta
    if not cycle.template_id:
        raise ValidationError('请先为该预算版本配置汇总科目和计算规则，再发布填报模板。')
    unsafe = [item for item in validate_xlsx_zip(path) if item[0] == 'P0']
    if unsafe:
        raise ValidationError('工作簿无法用于填报：' + '；'.join(item[2] for item in unsafe[:5]))
    template = cycle.template
    identity = {'project_code': 'TEMPLATE_REVIEW', 'budget_year': cycle.budget_year,
                'cycle_id': cycle.pk, 'template_version': template.version}
    meta = {'template_version': template.version, 'budget_year': str(cycle.budget_year),
            'project_code': identity['project_code'], 'rule_version': template.rule_version,
            'formula_manifest_hash': template.formula_manifest_hash,
            'project_signature': signing.dumps(identity, salt='budget-template-v1')}
    upload = SimpleNamespace(template=template, cycle=cycle, cycle_id=cycle.pk,
                             project=SimpleNamespace(code=identity['project_code']))
    with tempfile.TemporaryDirectory(prefix='budget-template-review-') as directory:
        checked = Path(directory) / path.name
        _inject_sys_meta(path, checked, meta, preserve_inputs=True)
        blocking = [item for item in validate_upload_contract(upload, checked) if item[0] == 'P0']
    if blocking:
        reasons = list(dict.fromkeys(item[2] for item in blocking if item[1] != 'TEMPLATE_SIGNATURE'))
        raise ValidationError('模板尚不能用于填报：' + '；'.join(reasons[:5]) +
                              '。请保留汇总表科目和计算公式，可按需要增加附表。')


def download_distribution_template(item, project, cycle):
    from budgeting.services.template_delivery import signed_template_copy
    if item.cycle_id != cycle.pk:
        raise ValidationError('此模板不属于当前预算版本，请重新选择。')
    path = distribution_template_path(item)
    if not path.is_file():
        raise FileNotFoundError('填报模板文件暂不可用，请联系管理员重新发布。')
    return signed_template_copy(cycle.template, project, cycle, source_path=path, preserve_inputs=True)
