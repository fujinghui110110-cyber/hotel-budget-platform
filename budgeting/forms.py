from collections import Counter

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from budgeting.models import REPORTS, NormalizedValue, Project


class UploadForm(forms.Form):
    file = forms.FileField(label="预算套表", help_text="支持 .xlsx、.xlsm，最大 50 MB。")

    def __init__(self, *args, cycle=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_rehearsal = bool(cycle and cycle.source_budget_year)
        if self.is_rehearsal:
            self.fields["file"].help_text = "历史原表演练：支持 .xlsx、.xlsm，最大 50 MiB；不执行宏或刷新外链。"

    def clean_file(self):
        upload = self.cleaned_data["file"]
        suffixes = (".xlsx", ".xlsm")
        if not upload.name.lower().endswith(suffixes):
            raise forms.ValidationError("请选择 .xlsx 或 .xlsm 格式的 Excel 文件。")
        if upload.size > 50 * 1024 * 1024:
            raise forms.ValidationError("压缩文件超过 50 MiB。")
        return upload


class SubmitForm(forms.Form):
    note = forms.CharField(label="说明", required=False, widget=forms.Textarea(attrs={"rows": 3}))


class ApproveForm(forms.Form):
    pass


class RejectForm(forms.Form):
    reason = forms.CharField(label="打回原因", widget=forms.Textarea(attrs={"rows": 3}), help_text="项目端将看到这条说明。")


class AdjustmentForm(forms.Form):
    report_code = forms.ChoiceField(label="报表", choices=[(code, name) for code, name in REPORTS.items()])
    row_code = forms.ChoiceField(label="行项", choices=[])
    period = forms.ChoiceField(label="期间", choices=[])
    delta_yuan = forms.DecimalField(label="调整差额（元）", max_digits=18, decimal_places=2, help_text="正数为调增，负数为调减。")
    reason = forms.CharField(label="调整原因", widget=forms.Textarea(attrs={"rows": 3}))
    due_date = forms.DateField(label="期限", required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, cycle=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cycle = cycle
        self.report_choices_map = self._build_report_choices_map()
        self._apply_choices()

    def _rows_periods(self, qs):
        seen = {}
        for nv in qs.order_by("row_code").values("row_code", "row_label"):
            seen[nv["row_code"]] = nv["row_label"] or nv["row_code"]
        counts = Counter(seen.values())
        rows = [
            (code, f"{label}（{code}）" if counts[label] > 1 else label)
            for code, label in seen.items()
        ]
        periods = [(p, p) for p in qs.order_by("period").values_list("period", flat=True).distinct()]
        return rows, periods

    def _build_report_choices_map(self):
        base = NormalizedValue.objects.none()
        if self.cycle:
            base = NormalizedValue.objects.filter(upload__cycle=self.cycle)
        return {
            code: dict(zip(("rows", "periods"), self._rows_periods(base.filter(report_code=code))))
            for code in REPORTS
        }

    def _apply_choices(self):
        report = self.data.get(self.add_prefix("report_code")) if self.is_bound else None
        if report not in REPORTS:
            report = next(iter(REPORTS))
        entry = self.report_choices_map[report]
        self.fields["row_code"].choices = entry["rows"] or [("", "该报表暂无可用行项")]
        self.fields["period"].choices = entry["periods"] or [("", "该报表暂无可用期间")]


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["code", "name"]


class ProjectAccountForm(forms.Form):
    username = forms.CharField(label="账号", max_length=150)
    password = forms.CharField(label="初始密码", widget=forms.PasswordInput(render_value=True))
    is_admin = forms.BooleanField(label="管理账号", required=False, help_text="勾选则创建管理端账号，不绑定项目。")
    project = forms.ModelChoiceField(label="绑定项目", queryset=Project.objects.filter(is_active=True), required=False)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("is_admin") and not cleaned.get("project"):
            self.add_error("project", "项目账号必须绑定一个项目。")
        if cleaned.get("password"):
            try:
                validate_password(cleaned["password"], get_user_model()(username=cleaned.get("username", "")))
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if get_user_model().objects.filter(username=username).exists():
            raise forms.ValidationError("该账号已存在。")
        return username


class ResetPasswordForm(forms.Form):
    username = forms.CharField(label="账号", max_length=150)
    new_password = forms.CharField(label="新密码", widget=forms.PasswordInput(render_value=True))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("new_password"):
            user = get_user_model().objects.filter(username=cleaned.get("username", "")).first()
            try:
                validate_password(cleaned["new_password"], user)
            except forms.ValidationError as exc:
                self.add_error("new_password", exc)
        return cleaned
