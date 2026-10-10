"""Write the Excel template Legal fills in with AJi's real standards.

    manage.py standards_template AJi_Standards_Template.xlsx

One row per clause category, from the live category list, with its Arabic and
English name, its default department and -- for reference only -- the FIDIC
baseline position Sanad uses today where AJi has none. The completed file
loads as is:

    manage.py import_standards AJi_Standards_Template.xlsx --dry-run

Sheets: "اقرأ أولاً · Read me" (how to fill it in), "Standards" (the rows),
"Lists" (the codes behind the drop-downs). In the brand's black, white and
yellow; set in Arial, the brand guidelines' system typeface for files shared
widely (p. 32), since the recipients may not have Draft B or IBM Plex.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.accounts.models import Country
from apps.contracts.models import ContractType
from apps.standards.models import ClauseCategory, StandardStatus, StandardVersion

YELLOW, BLACK, WHITE = "FCD700", "000000", "FFFFFF"
REFERENCE_FILL = "F1F1F2"      # the grey of read-only reference columns
KEY_GREY = "626262"
FONT = "Arial"

#: (key read by import_standards, English label, Arabic label, width, required, reference-only)
COLUMNS = [
    ("category", "Category code", "رمز الفئة", 16, True, False),
    ("category_name_en", "Category (English)", "الفئة (إنجليزي)", 26, False, True),
    ("category_name_ar", "Category (Arabic)", "الفئة (عربي)", 24, False, True),
    ("country", "Country — blank = all", "الدولة — فارغ = الكل", 18, False, False),
    ("contract_type", "Contract type — blank = all", "نوع العقد — فارغ = الكل", 22, False, False),
    ("aji_role", "AJi's role — blank = all (consultant / client / sub_consultant / jv_partner)",
     "صفة AJi — فارغ = الكل (consultant / client / sub_consultant / jv_partner)", 24, False, False),
    ("preferred_position", "AJi preferred position (English)", "موقف AJi المفضل (إنجليزي)", 44, True, False),
    ("preferred_position_ar", "AJi preferred position (Arabic)", "موقف AJi المفضل (عربي)", 44, False, False),
    ("acceptable_range", "Acceptable fallback / range (English)", "الحد المقبول / البديل المقبول (إنجليزي)", 36, False, False),
    ("acceptable_range_ar", "Acceptable fallback / range (Arabic)", "الحد المقبول / البديل المقبول (عربي)", 36, False, False),
    ("red_flags", "Walk-away / red flags (English)", "خطوط حمراء / غير مقبول (إنجليزي)", 36, False, False),
    ("red_flags_ar", "Walk-away / red flags (Arabic)", "خطوط حمراء / غير مقبول (عربي)", 36, False, False),
    ("approved_fallback_wording", "Approved replacement wording (English)", "الصياغة البديلة المعتمدة (إنجليزي)", 48, False, False),
    ("approved_fallback_wording_ar", "Approved replacement wording (Arabic)", "الصياغة البديلة المعتمدة (عربي)", 48, False, False),
    ("exceptions", "Exceptions", "الاستثناءات", 28, False, False),
    ("impact", "Impact 1–5", "الأثر 1–5", 11, True, False),
    ("approval_authority", "Who approves a deviation", "جهة اعتماد الانحراف", 24, False, False),
    ("responsible_department", "Responsible department", "الإدارة المسؤولة", 22, False, False),
    ("source_reference", "Legal basis / source", "المرجع / الأساس القانوني", 26, False, False),
    ("effective_date", "Effective date (YYYY-MM-DD)", "تاريخ السريان", 16, False, False),
    ("notes", "Notes", "ملاحظات", 26, False, False),
    ("fidic_baseline_reference", "Today's FIDIC baseline (reference only)", "خط الأساس FIDIC الحالي (للاطلاع فقط)", 44, False, True),
]
EXTRA_ROWS = 15          # blank rows for country- or contract-type-specific overrides

IMPACT_SCALE = [
    (1, "Negligible — no real cost to AJi", "ضئيل — لا تكلفة فعلية على AJi"),
    (2, "Minor — small, recoverable cost", "بسيط — تكلفة صغيرة يمكن استردادها"),
    (3, "Moderate — noticeable cost or delay", "متوسط — تكلفة أو تأخير ملحوظ"),
    (4, "Major — significant financial or legal exposure", "كبير — تعرّض مالي أو قانوني كبير"),
    (5, "Critical — could threaten the project or AJi", "حرج — قد يهدد المشروع أو AJi"),
]


class Command(BaseCommand):
    help = "Write the Excel template Legal fills in with AJi's standards."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default="AJi_Standards_Template.xlsx")

    def handle(self, *args, **options):
        from openpyxl import Workbook

        book = Workbook()
        readme = book.active
        readme.title = "اقرأ أولاً · Read me"
        sheet = book.create_sheet("Standards")
        lists = book.create_sheet("Lists")

        categories = list(ClauseCategory.objects.filter(is_active=True).order_by("sort_order", "pk"))
        types = list(ContractType.objects.filter(is_active=True).order_by("pk"))
        self._lists(lists, categories, types)
        self._standards(sheet, categories, types)
        self._readme(readme)
        book.active = 0
        book.save(options["path"])
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {options['path']}: {len(categories)} categories, {EXTRA_ROWS} rows for overrides."))

    # ── styles ───────────────────────────────────────────────────────────
    @staticmethod
    def _font(**kw):
        from openpyxl.styles import Font

        return Font(name=FONT, **kw)

    @staticmethod
    def _fill(colour):
        from openpyxl.styles import PatternFill

        return PatternFill("solid", start_color=colour, end_color=colour)

    @staticmethod
    def _border():
        from openpyxl.styles import Border, Side

        side = Side(style="thin", color="D8D8D8")
        return Border(left=side, right=side, top=side, bottom=side)

    # ── the rows ─────────────────────────────────────────────────────────
    def _standards(self, ws, categories, types):
        from openpyxl.styles import Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation

        last = get_column_letter(len(COLUMNS))
        ws.merge_cells(f"A1:{last}1")
        title = ws["A1"]
        title.value = "AJi Standards — template for Legal   ·   قالب معايير AJi للشؤون القانونية"
        title.font = self._font(bold=True, size=14, color=YELLOW)
        title.fill = self._fill(BLACK)
        title.alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[1].height = 30

        for i, (key, en, ar, width, required, reference) in enumerate(COLUMNS, 1):
            letter = get_column_letter(i)
            ws.column_dimensions[letter].width = width
            label = ws.cell(row=2, column=i, value=f"{en}{' *' if required else ''}\n{ar}{' *' if required else ''}")
            label.font = self._font(bold=True, size=10, color=BLACK)
            label.fill = self._fill(REFERENCE_FILL if reference else YELLOW)
            label.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
            label.border = self._border()
            # The row import_standards reads. Kept visible, small and grey:
            # renaming it would stop the column loading.
            k = ws.cell(row=3, column=i, value=key)
            k.font = self._font(size=8, italic=True, color=KEY_GREY)
            k.alignment = Alignment(horizontal="center")
        ws.row_dimensions[2].height = 44

        baseline = {}
        for v in StandardVersion.objects.filter(
                standard__reference_template="fidic", status=StandardStatus.APPROVED).select_related("standard"):
            baseline.setdefault(v.standard.category_id, v.preferred_position)

        rows = [(c, baseline.get(c.pk, "")) for c in categories] + [(None, "")] * EXTRA_ROWS
        for r, (category, fidic) in enumerate(rows, 4):
            values = {
                "category": category.code if category else "",
                "category_name_en": category.name if category else "",
                "category_name_ar": category.name_ar if category else "",
                "responsible_department": category.default_department if category else "",
                "fidic_baseline_reference": fidic,
            }
            for i, (key, _en, _ar, _w, _req, reference) in enumerate(COLUMNS, 1):
                cell = ws.cell(row=r, column=i, value=values.get(key) or None)
                cell.font = self._font(size=10, color=KEY_GREY if reference else BLACK)
                cell.alignment = Alignment(wrap_text=True, vertical="top",
                                           horizontal="right" if key.endswith("_ar") else "left",
                                           readingOrder=2 if key.endswith("_ar") else 1)
                cell.border = self._border()
                if reference:
                    cell.fill = self._fill(REFERENCE_FILL)
            ws.row_dimensions[r].height = 60 if category else 30
        end = 3 + len(rows)

        def listed(ref, *, prompt):
            dv = DataValidation(type="list", formula1=ref, allow_blank=True, showDropDown=False)
            dv.promptTitle, dv.prompt = "AJi", prompt
            dv.showInputMessage = True
            return dv

        n_cat, n_types = len(categories), len(types)
        validations = [
            (listed(f"=Lists!$A$2:$A${1 + n_cat}", prompt="Pick a category code / اختر رمز الفئة"), "A"),
            (listed(f"=Lists!$E$2:$E${1 + len(Country.choices)}", prompt="Blank = every country / فارغ = كل الدول"), "D"),
            (listed(f"=Lists!$H$2:$H${1 + n_types}", prompt="Blank = every contract type / فارغ = كل الأنواع"), "E"),
            (listed('"1,2,3,4,5"', prompt="1 negligible … 5 critical — see Read me / انظر اقرأ أولاً"), "M"),
        ]
        for dv, col in validations:
            ws.add_data_validation(dv)
            dv.add(f"{col}4:{col}{end}")
        dates = DataValidation(type="date", operator="greaterThan", formula1="DATE(2000,1,1)", allow_blank=True)
        dates.error, dates.errorTitle = "Use a date, YYYY-MM-DD / استخدم تاريخاً", "AJi"
        ws.add_data_validation(dates)
        dates.add(f"Q4:Q{end}")
        for r in range(4, end + 1):
            ws[f"Q{r}"].number_format = "yyyy-mm-dd"

        ws.freeze_panes = "D4"
        ws.auto_filter.ref = f"A3:{last}{end}"
        ws.sheet_view.zoomScale = 90

    # ── the codes behind the drop-downs ──────────────────────────────────
    def _lists(self, ws, categories, types):
        from openpyxl.styles import Alignment

        heads = {"A": "category", "B": "English", "C": "العربية", "E": "country", "F": "name",
                 "H": "contract_type", "I": "name", "K": "impact", "L": "English", "M": "العربية"}
        for col, text in heads.items():
            cell = ws[f"{col}1"]
            cell.value = text
            cell.font = self._font(bold=True, color=BLACK)
            cell.fill = self._fill(YELLOW)
        for r, c in enumerate(categories, 2):
            ws[f"A{r}"], ws[f"B{r}"], ws[f"C{r}"] = c.code, c.name, c.name_ar
            ws[f"C{r}"].alignment = Alignment(horizontal="right", readingOrder=2)
        for r, (code, name) in enumerate(Country.choices, 2):
            ws[f"E{r}"], ws[f"F{r}"] = code, str(name)
        for r, t in enumerate(types, 2):
            ws[f"H{r}"], ws[f"I{r}"] = t.code, t.name
        for r, (n, en, ar) in enumerate(IMPACT_SCALE, 2):
            ws[f"K{r}"], ws[f"L{r}"], ws[f"M{r}"] = n, en, ar
        for col, width in {"A": 16, "B": 40, "C": 34, "E": 10, "F": 26, "H": 14, "I": 32, "K": 8, "L": 44, "M": 38}.items():
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows():
            for cell in row:
                if cell.row > 1:
                    cell.font = self._font(size=10)

    # ── how to fill it in ────────────────────────────────────────────────
    def _readme(self, ws):
        from openpyxl.styles import Alignment

        ws.column_dimensions["A"].width = 4
        ws.column_dimensions["B"].width = 62
        ws.column_dimensions["C"].width = 62
        ws.merge_cells("B1:C1")
        ws["B1"] = "AJi Standards — how to fill in this template   ·   معايير AJi — طريقة تعبئة القالب"
        ws["B1"].font = self._font(bold=True, size=14, color=YELLOW)
        ws["B1"].fill = self._fill(BLACK)
        ws["B1"].alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[1].height = 32

        blocks = [
            ("What this is for", "الغرض من القالب",
             "Sanad compares every uploaded contract with AJi's standards. Today it has none of AJi's own, so it "
             "uses a FIDIC baseline. Each row you fill in becomes AJi's position for one clause category.",
             "يقارن سند كل عقد مرفوع بمعايير AJi. لا توجد حالياً معايير خاصة بـ AJi، لذلك يستخدم خط أساس FIDIC. "
             "كل صف تعبّئه يصبح موقف AJi لفئة بنود واحدة."),
            ("How to fill it in", "طريقة التعبئة",
             "Go to the sheet \"Standards\". There is one row per category. Fill in the yellow columns; the grey "
             "columns are for reference and are not imported. Columns marked * are required: the preferred "
             "position (in English or Arabic) and the impact. Leave a row blank to keep the FIDIC baseline for "
             "that category.",
             "انتقل إلى ورقة \"Standards\". يوجد صف لكل فئة. املأ الأعمدة الصفراء؛ الأعمدة الرمادية للاطلاع فقط ولا "
             "تُستورد. الأعمدة المعلّمة بـ * إلزامية: الموقف المفضل (بالإنجليزية أو العربية) والأثر. اترك الصف "
             "فارغاً للإبقاء على خط أساس FIDIC لتلك الفئة."),
            ("Country and contract type", "الدولة ونوع العقد",
             "Leave both blank for a position that applies everywhere. To set a different position for one country "
             "(SA, JO, AE, OM) or one contract type, use one of the blank rows at the bottom: pick the category "
             "code, the country and/or the contract type, and fill it in. The most specific position wins.",
             "اتركهما فارغين للموقف الذي ينطبق في كل مكان. لتحديد موقف مختلف لدولة واحدة أو نوع "
             "عقد واحد، استخدم أحد الصفوف الفارغة في الأسفل: اختر رمز الفئة والدولة و/أو نوع العقد ثم املأ الصف. "
             "الموقف الأكثر تحديداً هو الذي يُطبق.\nرموز الدول: SA السعودية، JO الأردن، AE الإمارات، OM عُمان."),
            ("The columns", "الأعمدة",
             "Preferred position — what AJi asks for. Acceptable fallback — what AJi can live with. Walk-away / red "
             "flags — what AJi never accepts. Approved replacement wording — the clause text AJi proposes in a "
             "redline; it goes to the counterparty as AJi-approved, so write it exactly as it should appear, in "
             "each language you have. Exceptions — cases where the position does not apply. Who approves a "
             "deviation — the role that can accept something outside the range.",
             "الموقف المفضل — ما تطلبه AJi. الحد المقبول — ما يمكن لـ AJi قبوله. الخطوط الحمراء — ما لا تقبله AJi "
             "أبداً. الصياغة البديلة المعتمدة — نص البند الذي تقترحه AJi في التعديلات المتتبَّعة؛ تُرسل إلى الطرف "
             "الآخر على أنها معتمدة من AJi، فاكتبها كما يجب أن تظهر تماماً وبكل لغة متاحة. الاستثناءات — الحالات التي "
             "لا ينطبق فيها الموقف. جهة اعتماد الانحراف — الجهة التي يمكنها قبول ما هو خارج الحد."),
            ("Impact (1–5)", "الأثر (1–5)",
             "\n".join(f"{n} — {en}" for n, en, _ in IMPACT_SCALE)
             + "\nThe impact is Legal's judgement of how much a breach of this clause can cost AJi; Sanad multiplies "
             "it by how strongly the contract deviates to score each risk.",
             "\n".join(f"{n} — {ar}" for n, _, ar in IMPACT_SCALE)
             + "\nالأثر هو تقدير الشؤون القانونية لحجم ما قد يكلّفه الإخلال بهذا البند على AJi؛ يضربه سند في درجة "
             "انحراف العقد لحساب كل خطر."),
            ("What happens next", "الخطوات التالية",
             "Send the completed file back. It is imported into Sanad as drafts — nothing changes until a Standard "
             "Manager reviews and approves each position in Settings → Standards. After approval, re-analysing a "
             "contract applies the new standards; earlier analyses keep the version they used.",
             "أعد إرسال الملف بعد تعبئته. يُستورد إلى سند كمسودات — لا يتغير شيء حتى يراجع مسؤول المعايير كل موقف "
             "ويعتمده من الإعدادات ← المعايير. بعد الاعتماد، تُطبّق إعادة تحليل العقد المعايير الجديدة؛ وتحتفظ "
             "التحليلات السابقة بالإصدار الذي استخدمته."),
            ("Please do not", "يرجى عدم",
             "Rename, reorder or delete the small grey key row (row 3) of \"Standards\" — Sanad reads the columns by "
             "those keys. Change the category codes. Merge cells in the rows you fill in.",
             "تغيير أسماء صف المفاتيح الرمادي الصغير (الصف 3) في ورقة \"Standards\" أو ترتيبه أو حذفه — يقرأ سند "
             "الأعمدة بتلك المفاتيح. تغيير رموز الفئات. دمج الخلايا في الصفوف التي تعبّئها."),
        ]
        r = 3
        for en_head, ar_head, en, ar in blocks:
            for col, text, rtl in (("B", en_head, False), ("C", ar_head, True)):
                cell = ws[f"{col}{r}"]
                cell.value = text
                cell.font = self._font(bold=True, size=11, color=BLACK)
                cell.fill = self._fill(YELLOW)
                cell.alignment = Alignment(horizontal="right" if rtl else "left", readingOrder=2 if rtl else 1, indent=1)
            for col, text, rtl in (("B", en, False), ("C", ar, True)):
                cell = ws[f"{col}{r + 1}"]
                cell.value = text
                cell.font = self._font(size=10.5)
                cell.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right" if rtl else "left",
                                           readingOrder=2 if rtl else 1, indent=1)
            lines = max(len(en) // 70 + en.count("\n"), len(ar) // 70 + ar.count("\n")) + 1
            ws.row_dimensions[r + 1].height = max(30, 15 * lines)
            r += 3
        ws.sheet_view.showGridLines = False
