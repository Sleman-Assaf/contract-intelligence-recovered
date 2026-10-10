"""Contract intake form (§4).

Field order follows the approved v2 design. Two fields are added because the
system cannot function without them, and both are flagged rather than slipped
in silently:

* **Country** — standards resolution is keyed on jurisdiction. Without it no
  standard can be selected at all.
* **Analysis perspective** — which side AJi is on. If AJi is the Consultant an
  uncapped delay-damages clause is dangerous; if AJi is the Client the same
  clause is protective. Guessing would invert every finding.
"""
from __future__ import annotations

from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Country, Entity
from apps.documents.storage import validate_upload

from .models import (
    ContractLanguage,
    ContractType,
    CounterpartyType,
    Currency,
    PartyRole,
)


class BilingualModelChoiceField(forms.ModelChoiceField):
    """Shows the option in the reader's language.

    `ModelChoiceField` labels options with `str(obj)`, which is the English
    name — so an Arabic upload form offered "Architectural Design Consultancy"
    beside fully translated labels.
    """

    def label_from_instance(self, obj):
        return getattr(obj, "display_name", None) or str(obj)


class ContractUploadForm(forms.Form):
    # ── بيانات العقد — as in the v2 design ────────────────────────────────
    project_name = forms.CharField(
        label=_("اسم المشروع"), max_length=255,
        widget=forms.TextInput(attrs={
            "class": "input", "placeholder": _("مثال: خدمات فنية واستشارية…")}),
    )
    client_name = forms.CharField(
        label=_("اسم العميل"), max_length=255,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": _("الجهة المتعاقدة")}),
    )
    contract_title = forms.CharField(
        label=_("عنوان العقد"), max_length=255,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": _("كما ورد في الوثيقة")}),
    )
    contract_type = BilingualModelChoiceField(
        label=_("نوع العقد"),
        # Django's own placeholder is "---------", in either language.
        empty_label=_("اختر نوع العقد"),
        queryset=ContractType.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "select"}),
    )
    reference = forms.CharField(
        label=_("رقم العقد"), max_length=64, required=False,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": _("PO / رقم المرجع")}),
    )
    contract_value = forms.DecimalField(
        label=_("قيمة العقد"), required=False, min_value=0,
        max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "input", "placeholder": "0.00", "step": "0.01"}),
    )
    currency = forms.ChoiceField(
        label=_("العملة"), choices=Currency.choices, initial=Currency.SAR,
        widget=forms.Select(attrs={"class": "select"}),
    )
    language = forms.ChoiceField(
        label=_("لغة العقد"), choices=ContractLanguage.choices,
        initial=ContractLanguage.ARABIC,
        widget=forms.Select(attrs={"class": "select"}),
    )
    effective_date = forms.DateField(
        label=_("تاريخ السريان"), required=False,
        widget=forms.DateInput(attrs={"class": "input", "type": "date"}),
    )
    expiry_date = forms.DateField(
        label=_("تاريخ الانتهاء"), required=False,
        widget=forms.DateInput(attrs={"class": "input", "type": "date"}),
    )
    department = forms.CharField(
        label=_("الإدارة"), max_length=120, required=False,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    reference_template = forms.ChoiceField(
        label=_("النموذج المرجعي لـ AJi"),
        # FIDIC only for now: AJi has no approved standards of its own; the
        # AJi profile comes back here once Legal approves them.
        choices=[("fidic", "FIDIC")],
        initial="fidic",
        widget=forms.Select(attrs={"class": "select"}),
    )
    analysis_instructions = forms.CharField(
        label=_("تعليمات مراجعة إضافية"), required=False,
        widget=forms.Textarea(attrs={
            "class": "textarea", "rows": 3,
            "placeholder": _("نقاط تريد أن يركّز عليها التحليل…")}),
    )

    # ── added because the system cannot work without them ────────────────
    country = forms.ChoiceField(
        label=_("الدولة / الاختصاص القضائي"),
        choices=Country.choices,
        widget=forms.Select(attrs={"class": "select"}),
        help_text=_("يحدد معايير AJi المطبقة. مطلوب لتشغيل التحليل."),
    )
    entity = BilingualModelChoiceField(
        label=_("كيان AJi المتعاقد"),
        empty_label=_("اختر الكيان"),
        queryset=Entity.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "select"}),
    )
    # Worded as the situations AJi is actually in, so a sub-consultancy where
    # AJi hires, a JV agreement or an association is not left on the
    # "consultant" default -- the side decides the direction of every risk
    # and which standards apply (Standard.perspective).
    analysis_perspective = forms.ChoiceField(
        label=_("صفة AJi في العقد"),
        choices=[
            (PartyRole.CONSULTANT, _("AJi الاستشاري — عقد مع العميل")),
            (PartyRole.CLIENT, _("AJi الطرف المتعاقِد — يعيّن استشارياً فرعياً أو مورّداً")),
            (PartyRole.SUB_CONSULTANT, _("AJi استشاري فرعي لدى استشاري آخر")),
            (PartyRole.JV_PARTNER, _("AJi شريك في ائتلاف / اتفاقية شراكة")),
            (PartyRole.OTHER, _("أخرى")),
        ],
        initial=PartyRole.CONSULTANT,
        widget=forms.Select(attrs={"class": "select"}),
        help_text=_("يحدد اتجاه تقييم المخاطر والمعايير التي تُطبَّق."),
    )
    counterparty_type = forms.ChoiceField(
        label=_("نوع الطرف الآخر"),
        choices=CounterpartyType.choices, initial=CounterpartyType.PRIVATE,
        widget=forms.Select(attrs={"class": "select"}),
    )

    file = forms.FileField(
        label=_("ملف العقد"),
        # Visually hidden but still focusable and still the real input: the
        # dropzone is a <label> around it, so clicking anywhere opens the
        # picker with no script involved.
        widget=forms.ClearableFileInput(attrs={
            "class": "dropzone__input", "accept": ".pdf,.doc,.docx"}),
        help_text=_("PDF, DOC, DOCX"),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        # Same validation the storage layer applies, surfaced early so the user
        # sees a useful message instead of a 500.
        validate_upload(uploaded)
        return uploaded

    def clean(self):
        cleaned = super().clean()
        effective = cleaned.get("effective_date")
        expiry = cleaned.get("expiry_date")
        if effective and expiry and expiry < effective:
            self.add_error("expiry_date", _("تاريخ الانتهاء يجب أن يكون بعد تاريخ السريان."))

        entity = cleaned.get("entity")
        country = cleaned.get("country")
        if entity and country and entity.country != country:
            # Not fatal — an entity can contract abroad — but worth surfacing,
            # because it changes which standards apply.
            self.add_error(
                "country",
                _("الدولة المختارة تختلف عن دولة الكيان (%(entity)s). "
                  "تأكد من صحة الاختصاص القضائي.") % {"entity": entity.get_country_display()},
            )
        return cleaned

    @property
    def max_upload_mb(self) -> int:
        return settings.MAX_UPLOAD_MB
