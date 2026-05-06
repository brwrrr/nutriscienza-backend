"""
Generazione del PDF NutriScienza a partire da:
  - IntakeRequest (dati cliente)
  - NutritionTargets (calcoli deterministici)
  - MealPlan (output dell'LLM)

Stesso layout della versione hardcoded `build_pdf.py` ma parametrizzato.
"""
from datetime import datetime
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, NextPageTemplate, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

from .models import IntakeRequest, MealPlan, NutritionTargets, WorkoutPlan


# ---------- Brand palette ----------
GREEN_DEEP = HexColor("#2D5F3F")
GREEN_DARK = HexColor("#1A2E22")
GREEN_SOFT = HexColor("#4A8264")
GREEN_TINT = HexColor("#E8F0EB")
CREAM = HexColor("#F5F1E8")
CREAM_LIGHT = HexColor("#FBF9F4")
GOLD = HexColor("#C9A66B")
GOLD_DARK = HexColor("#A88349")
TEXT = HexColor("#2A2A2A")
TEXT_MUTED = HexColor("#6B6B6B")
BORDER = HexColor("#E5E0D3")


# ---------- Styles ----------
styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontName="Helvetica-Bold",
    fontSize=26, textColor=GREEN_DEEP, leading=32, spaceAfter=12)
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName="Helvetica-Bold",
    fontSize=15, textColor=GREEN_DEEP, leading=20, spaceAfter=8, spaceBefore=12)
H3 = ParagraphStyle("H3", parent=styles["Heading3"], fontName="Helvetica-Bold",
    fontSize=12, textColor=GREEN_DEEP, leading=16, spaceAfter=5, spaceBefore=10)
EYEBROW = ParagraphStyle("Eyebrow", parent=styles["Normal"], fontName="Helvetica-Bold",
    fontSize=9, textColor=GOLD_DARK, leading=11, spaceAfter=6)
BODY = ParagraphStyle("Body", parent=styles["Normal"], fontName="Helvetica",
    fontSize=10.5, textColor=TEXT, leading=15, spaceAfter=8, alignment=TA_LEFT)
BODY_JUST = ParagraphStyle("BodyJust", parent=BODY, alignment=TA_JUSTIFY)
LEAD = ParagraphStyle("Lead", parent=BODY, fontSize=11.5, leading=17,
    textColor=TEXT_MUTED, spaceAfter=12)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8.5, leading=12, textColor=TEXT_MUTED)


GOAL_LABELS = {
    "dimagrire": "dimagrimento sostenibile",
    "mantenere": "mantenimento e ricomposizione",
    "massa": "aumento massa muscolare",
    "salute": "salute generale e benessere",
}
PLAN_LABELS = {"base": "Piano Base", "completo": "Piano Completo", "coach": "Piano Coach"}
PLAN_DURATION = {"base": "7 giorni", "completo": "4 settimane (rinnovabile)", "coach": "12 settimane periodizzate"}
MONTHS_IT = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
             "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]


# ---------- Page decorations ----------

def _make_header_footer(plan_label: str, customer_name: str):
    def fn(canv, doc):
        canv.saveState()
        width, height = A4
        # Top bar
        canv.setFillColor(GREEN_DEEP)
        canv.rect(0, height - 0.45 * cm, width, 0.45 * cm, fill=1, stroke=0)
        # Brand
        canv.setFont("Helvetica-Bold", 10)
        canv.setFillColor(GREEN_DEEP)
        canv.drawString(2 * cm, height - 1.1 * cm, "NutriScienza")
        canv.setFont("Helvetica", 9)
        canv.setFillColor(TEXT_MUTED)
        canv.drawString(4.6 * cm, height - 1.1 * cm, f"· {plan_label} · {customer_name}")
        canv.setFont("Helvetica", 8.5)
        canv.drawRightString(width - 2 * cm, height - 1.1 * cm, "nutriscienza.org")
        # Footer
        canv.setStrokeColor(BORDER)
        canv.setLineWidth(0.4)
        canv.line(2 * cm, 1.6 * cm, width - 2 * cm, 1.6 * cm)
        canv.setFont("Helvetica", 8)
        canv.setFillColor(TEXT_MUTED)
        canv.drawString(2 * cm, 1 * cm, "© NutriScienza — Conforme alle linee guida LARN")
        canv.drawRightString(width - 2 * cm, 1 * cm, f"Pagina {doc.page}")
        canv.restoreState()
    return fn


def _make_cover_page(intake: IntakeRequest, plan_label: str, plan_duration: str):
    def fn(canv, doc):
        canv.saveState()
        width, height = A4
        canv.setFillColor(CREAM)
        canv.rect(0, height / 2, width, height / 2, fill=1, stroke=0)
        canv.setFillColor(GREEN_DEEP)
        canv.rect(0, height - 0.6 * cm, width, 0.6 * cm, fill=1, stroke=0)

        # Logo
        canv.setFont("Helvetica-Bold", 16)
        canv.setFillColor(GREEN_DEEP)
        canv.drawString(2 * cm, height - 2 * cm, "Nutri")
        tw = canv.stringWidth("Nutri", "Helvetica-Bold", 16)
        canv.setFillColor(GOLD)
        canv.drawString(2 * cm + tw, height - 2 * cm, "Scienza")

        # Eyebrow + Title
        canv.setFont("Helvetica-Bold", 9)
        canv.setFillColor(GOLD_DARK)
        canv.drawString(2 * cm, height - 5 * cm, f"{plan_label.upper()}  ·  {plan_duration.upper()}")
        canv.setFont("Helvetica-Bold", 36)
        canv.setFillColor(GREEN_DEEP)
        canv.drawString(2 * cm, height - 7 * cm, "Il tuo piano")
        canv.drawString(2 * cm, height - 8.4 * cm, "alimentare")
        canv.setFillColor(GREEN_SOFT)
        canv.drawString(2 * cm, height - 9.8 * cm, "personalizzato.")
        canv.setStrokeColor(GOLD)
        canv.setLineWidth(2)
        canv.line(2 * cm, height - 11 * cm, 4.5 * cm, height - 11 * cm)

        # Customer card
        canv.setFillColor(white)
        canv.setStrokeColor(BORDER)
        canv.setLineWidth(0.6)
        canv.roundRect(2 * cm, height / 2 - 6 * cm, width - 4 * cm, 4.8 * cm, 0.3 * cm,
                       fill=1, stroke=1)
        canv.setFont("Helvetica-Bold", 8)
        canv.setFillColor(TEXT_MUTED)
        canv.drawString(2.6 * cm, height / 2 - 1 * cm, "PREPARATO PER")
        canv.setFont("Helvetica-Bold", 22)
        canv.setFillColor(GREEN_DEEP)
        canv.drawString(2.6 * cm, height / 2 - 1.8 * cm, intake.first_name)
        canv.setFont("Helvetica", 11)
        canv.setFillColor(TEXT_MUTED)
        meta = f"{intake.age} anni · Obiettivo: {GOAL_LABELS[intake.goal]}"
        canv.drawString(2.6 * cm, height / 2 - 2.5 * cm, meta)

        canv.setStrokeColor(BORDER)
        canv.line(2.6 * cm, height / 2 - 3.2 * cm, width - 2.6 * cm, height / 2 - 3.2 * cm)

        today = datetime.now()
        date_str = f"{today.day} {MONTHS_IT[today.month]} {today.year}".capitalize()

        canv.setFont("Helvetica-Bold", 8)
        canv.setFillColor(TEXT_MUTED)
        canv.drawString(2.6 * cm, height / 2 - 4 * cm, "DATA DI EMISSIONE")
        canv.setFont("Helvetica", 11)
        canv.setFillColor(TEXT)
        canv.drawString(2.6 * cm, height / 2 - 4.7 * cm, date_str)

        canv.setFont("Helvetica-Bold", 8)
        canv.setFillColor(TEXT_MUTED)
        canv.drawString(11 * cm, height / 2 - 4 * cm, "VALIDITÀ DEL PIANO")
        canv.setFont("Helvetica", 11)
        canv.setFillColor(TEXT)
        canv.drawString(11 * cm, height / 2 - 4.7 * cm, plan_duration)

        # Authority
        canv.setFont("Helvetica", 9)
        canv.setFillColor(TEXT_MUTED)
        canv.drawCentredString(width / 2, 3.2 * cm,
            "Calcoli basati sull'equazione Mifflin-St Jeor — validata in letteratura scientifica")
        canv.drawCentredString(width / 2, 2.7 * cm,
            "Conforme alle linee guida LARN — Società Italiana di Nutrizione Umana")

        canv.setFillColor(GREEN_DEEP)
        canv.rect(0, 0, width, 0.6 * cm, fill=1, stroke=0)
        canv.setFillColor(GOLD)
        canv.rect(0, 0.6 * cm, width, 0.15 * cm, fill=1, stroke=0)
        canv.restoreState()
    return fn


# ---------- Helpers ----------

def _metric(label: str, value: str, sub: str) -> Table:
    t = Table([
        [Paragraph(label.upper(), ParagraphStyle("ml", parent=BODY, fontName="Helvetica-Bold",
                                                  fontSize=8, textColor=TEXT_MUTED, leading=10))],
        [Paragraph(value, ParagraphStyle("mv", parent=BODY, fontName="Helvetica-Bold",
                                          fontSize=18, textColor=GREEN_DEEP, leading=22))],
        [Paragraph(sub, SMALL)],
    ], colWidths=[5.4 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CREAM_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _meal_table(day_label: str, total_kcal: int, meals: list) -> Table:
    header = [Paragraph(f'<font color="white"><b>{day_label}</b></font>', BODY),
              "",
              Paragraph(f'<font color="white">~ {total_kcal} kcal</font>', BODY)]
    rows = [header]
    for m in meals:
        rows.append([
            Paragraph(f"<b>{m.name}</b>", BODY),
            Paragraph(m.description, BODY),
            Paragraph(f"<b>{m.kcal}</b> kcal", SMALL),
        ])
    t = Table(rows, colWidths=[3.2 * cm, 11 * cm, 2.3 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GREEN_DEEP),
        ("LEFTPADDING", (0, 0), (-1, 0), 14),
        ("RIGHTPADDING", (0, 0), (-1, 0), 14),
        ("TOPPADDING", (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
        ("BACKGROUND", (0, 1), (-1, -1), white),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, BORDER),
        ("LEFTPADDING", (0, 1), (-1, -1), 12),
        ("RIGHTPADDING", (0, 1), (-1, -1), 12),
        ("TOPPADDING", (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
    ]))
    return t


def _info_box(title: str, body: str, accent=GREEN_DEEP, bg=GREEN_TINT) -> Table:
    t = Table([
        [Paragraph(f"<b>{title}</b>",
                   ParagraphStyle("ibt", parent=BODY, fontSize=11, textColor=accent, spaceAfter=4))],
        [Paragraph(body, BODY)],
    ], colWidths=[16.5 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    return t


# ---------- Doc template ----------

class _Doc(BaseDocTemplate):
    def __init__(self, filename: str, intake: IntakeRequest):
        super().__init__(filename, pagesize=A4,
            leftMargin=2 * cm, rightMargin=2 * cm,
            topMargin=2 * cm, bottomMargin=2.2 * cm,
            title=f"NutriScienza — {PLAN_LABELS[intake.plan]}",
            author="NutriScienza")
        cover_frame = Frame(0, 0, A4[0], A4[1], id="cover",
                            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        normal_frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="normal")
        plan_label = PLAN_LABELS[intake.plan]
        plan_duration = PLAN_DURATION[intake.plan]
        self.addPageTemplates([
            PageTemplate(id="Cover", frames=[cover_frame],
                         onPage=_make_cover_page(intake, plan_label, plan_duration)),
            PageTemplate(id="Normal", frames=[normal_frame],
                         onPage=_make_header_footer(plan_label, intake.first_name)),
        ])


# ---------- API pubblica ----------

def build_pdf(intake: IntakeRequest, targets: NutritionTargets, plan: MealPlan,
              output_path: str, workout: WorkoutPlan | None = None) -> str:
    """Genera il PDF e lo salva su `output_path`. Ritorna il path.

    `workout` è opzionale: se fornito (Completo/Coach), viene aggiunta la sezione
    «Programma di allenamento» dopo i menù e prima delle note metodologiche del piano.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    story = []
    story.append(NextPageTemplate("Normal"))
    story.append(PageBreak())

    # ============ Pagina Profilo ============
    story.append(Paragraph("IL TUO PROFILO", EYEBROW))
    story.append(Paragraph("I numeri che contano per te.", H1))
    story.append(Paragraph(
        f"Abbiamo calcolato il tuo fabbisogno energetico utilizzando la formula di Mifflin-St Jeor — "
        f"lo standard scientifico più validato — applicando un fattore di attività "
        f"<b>{intake.activity}</b> (PAL {targets.pal}) "
        f"{'e un deficit calorico controllato del ' + str(abs(targets.deficit_pct)) + '%' if targets.deficit_pct < 0 else ('e un surplus controllato del ' + str(targets.deficit_pct) + '%') if targets.deficit_pct > 0 else 'per il mantenimento'}, "
        f"in linea con le raccomandazioni LARN.",
        BODY_JUST))
    story.append(Spacer(1, 10))

    sex_label = "Femminile" if intake.sex == "F" else "Maschile"
    target_w_label = (
        f"{intake.target_weight} kg ({intake.target_weight - intake.weight:+.1f} kg)"
        if intake.target_weight else "—"
    )
    profile = Table([
        [Paragraph("<b>Età</b>", BODY), Paragraph(f"{intake.age} anni", BODY),
         Paragraph("<b>Sesso</b>", BODY), Paragraph(sex_label, BODY)],
        [Paragraph("<b>Altezza</b>", BODY), Paragraph(f"{intake.height} cm", BODY),
         Paragraph("<b>Peso attuale</b>", BODY), Paragraph(f"{intake.weight} kg", BODY)],
        [Paragraph("<b>BMI</b>", BODY), Paragraph(f"{targets.bmi} ({targets.bmi_label})", BODY),
         Paragraph("<b>Peso obiettivo</b>", BODY), Paragraph(target_w_label, BODY)],
        [Paragraph("<b>Attività</b>", BODY), Paragraph(f"{intake.activity.capitalize()} (PAL {targets.pal})", BODY),
         Paragraph("<b>Allenamenti</b>", BODY), Paragraph(f"{intake.workouts} volte/sett.", BODY)],
    ], colWidths=[2.5 * cm, 4.8 * cm, 3 * cm, 6.2 * cm])
    profile.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CREAM_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(profile)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Il tuo fabbisogno calcolato", H3))
    story.append(Spacer(1, 6))
    metrics = Table([[
        _metric("Metabolismo basale", f"{targets.bmr:,}".replace(",", "."),
                "kcal — BMR (Mifflin-St Jeor)"),
        _metric("Fabbisogno totale", f"{targets.tdee:,}".replace(",", "."),
                "kcal — TDEE con attività"),
        _metric("Target giornaliero", f"{targets.target_kcal:,}".replace(",", "."),
                f"kcal — {'deficit' if targets.deficit_pct < 0 else 'surplus' if targets.deficit_pct > 0 else 'mantenimento'} {targets.deficit_pct:+d}%"),
    ]], colWidths=[5.6 * cm] * 3)
    metrics.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(metrics)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Ripartizione macronutrienti", H3))
    macro = Table([
        [Paragraph("<b>Macronutriente</b>", BODY), Paragraph("<b>% kcal</b>", BODY),
         Paragraph("<b>Grammi/giorno</b>", BODY), Paragraph("<b>Funzione</b>", BODY)],
        [Paragraph("Proteine", BODY), Paragraph(f"{targets.protein_pct}%", BODY),
         Paragraph(f"{targets.protein_g} g ({targets.protein_g / intake.weight:.1f} g/kg)", BODY),
         Paragraph("Preserva la massa magra e aumenta la sazietà", BODY)],
        [Paragraph("Carboidrati", BODY), Paragraph(f"{targets.carbs_pct}%", BODY),
         Paragraph(f"{targets.carbs_g} g", BODY),
         Paragraph("Energia per allenamenti e cervello", BODY)],
        [Paragraph("Grassi", BODY), Paragraph(f"{targets.fat_pct}%", BODY),
         Paragraph(f"{targets.fat_g} g", BODY),
         Paragraph("Equilibrio ormonale e sazietà", BODY)],
    ], colWidths=[3.5 * cm, 2 * cm, 3.8 * cm, 7.2 * cm])
    macro.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GREEN_DEEP),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("BACKGROUND", (0, 1), (-1, -1), white),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(macro)
    story.append(PageBreak())

    # ============ Pagine Menù — una sezione per settimana ============
    total_weeks = len(plan.weeks)
    is_multiweek = total_weeks > 1

    # Eyebrow + intro globale (solo prima settimana, poi le settimane si distinguono da sé)
    if is_multiweek:
        story.append(Paragraph(f"IL TUO MENÙ — {total_weeks} SETTIMANE", EYEBROW))
        story.append(Paragraph("Cosa mangerai durante il programma.", H1))
        story.append(Paragraph(
            "Ogni settimana ha menù diversi mantenendo gli stessi target nutrizionali. "
            "Le grammature sono indicative del peso a crudo salvo diversa indicazione.", LEAD))
    else:
        story.append(Paragraph("IL TUO MENÙ — 7 GIORNI", EYEBROW))
        story.append(Paragraph("Cosa mangerai questa settimana.", H1))
        story.append(Paragraph(
            "Pasti pensati sull'alimentazione mediterranea, ingredienti facilmente reperibili. "
            "Le grammature sono indicative del peso a crudo salvo diversa indicazione.", LEAD))

    for week_idx, week in enumerate(plan.weeks):
        # Per piani multi-settimana, intestazione di settimana
        if is_multiweek:
            if week_idx > 0:
                story.append(PageBreak())
            story.append(Spacer(1, 6))
            story.append(Paragraph(week.label.upper(), EYEBROW))
            story.append(Paragraph(week.label, H2))
            if week.phase:
                story.append(Paragraph(f"Fase: <b>{week.phase}</b>", BODY))
                story.append(Spacer(1, 8))

        # Giorni — KeepTogether garantisce che un giorno non si spezzi tra pagine
        # ed evita la pagina vuota che si otteneva con PageBreak espliciti
        for day in week.days:
            story.append(KeepTogether([
                _meal_table(day.label, day.total_kcal, day.meals),
                Spacer(1, 12),
            ]))

        if week.weekly_summary:
            story.append(Spacer(1, 6))
            story.append(_info_box("Riepilogo settimanale", week.weekly_summary))

        # Lista della spesa di questa settimana
        story.append(Spacer(1, 14))
        shopping_title = "LISTA DELLA SPESA" if not is_multiweek else f"LISTA DELLA SPESA — {week.label}"
        story.append(Paragraph(shopping_title, EYEBROW))
        story.append(Paragraph(
            "Una settimana, una sola spesa." if not is_multiweek else f"Spesa per la {week.label.lower()}.",
            H3))
        story.append(Paragraph(
            "Quantità calcolate per i 7 giorni. Suggeriamo una spesa unica a inizio settimana "
            "e una piccola integrazione di freschi a metà settimana.", BODY))
        story.append(Spacer(1, 6))

        for cat in week.shopping_list:
            story.append(Paragraph(cat.name, H3))
            rows = []
            row = []
            for it in cat.items:
                row.append(Paragraph(f"• {it}", BODY))
                if len(row) == 3:
                    rows.append(row); row = []
            while row and len(row) < 3:
                row.append(Paragraph("", BODY))
            if row:
                rows.append(row)
            t = Table(rows, colWidths=[5.7 * cm] * 3)
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))

    story.append(PageBreak())

    # ============ Programma di allenamento (Completo + Coach) ============
    if workout is not None:
        story.append(Paragraph("PROGRAMMA DI ALLENAMENTO", EYEBROW))
        story.append(Paragraph("Il tuo piano di lavoro in palestra.", H1))
        story.append(Paragraph(workout.methodology, BODY_JUST))
        story.append(Spacer(1, 10))
        story.append(_info_box("Come progredire nel tempo", workout.progression_notes,
                                accent=GOLD_DARK, bg=CREAM))
        story.append(Spacer(1, 14))

        for w_idx, w_week in enumerate(workout.weeks):
            if w_idx > 0:
                story.append(PageBreak())
            header_label = f"Settimana {w_week.week_number} — {w_week.phase}"
            story.append(Paragraph(header_label.upper(), EYEBROW))
            story.append(Paragraph(header_label, H2))
            if w_week.week_focus:
                story.append(Paragraph(f"<i>{w_week.week_focus}</i>", BODY))
            story.append(Spacer(1, 10))

            for sess in w_week.sessions:
                # Costruisci una tabella esercizi per la sessione
                ex_rows = [[
                    Paragraph("<b>Esercizio</b>", BODY),
                    Paragraph("<b>Serie x Reps</b>", BODY),
                    Paragraph("<b>Recupero</b>", BODY),
                    Paragraph("<b>Note</b>", BODY),
                ]]
                for ex in sess.exercises:
                    ex_rows.append([
                        Paragraph(ex.name, BODY),
                        Paragraph(ex.sets_reps, BODY),
                        Paragraph(ex.rest, BODY),
                        Paragraph(ex.notes or "—", SMALL),
                    ])

                ex_table = Table(ex_rows, colWidths=[6 * cm, 3 * cm, 2.2 * cm, 5.3 * cm])
                ex_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), GREEN_DEEP),
                    ("TEXTCOLOR", (0, 0), (-1, 0), white),
                    ("BACKGROUND", (0, 1), (-1, -1), white),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]))

                # Header sessione + tabella tenuti insieme
                session_block = [
                    Paragraph(sess.label, H3),
                    Paragraph(
                        f"Durata stimata: <b>{sess.duration_min} min</b> · Focus: {sess.focus}",
                        SMALL),
                    Spacer(1, 6),
                    ex_table,
                    Spacer(1, 14),
                ]
                story.append(KeepTogether(session_block))

        story.append(PageBreak())

    # ============ Note + disclaimer ============
    story.append(Paragraph("NOTE METODOLOGICHE DEL PIANO", EYEBROW))
    story.append(Paragraph("Cinque consigli che fanno la differenza.", H1))

    for i, tip in enumerate(plan.nutritionist_tips, 1):
        story.append(Paragraph(f"{i}. {tip['title']}", H3))
        story.append(Paragraph(tip["text"], BODY_JUST))
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 12))

    disclaimer = Table([
        [Paragraph("<b>AVVERTENZA IMPORTANTE</b>",
                   ParagraphStyle("dt", parent=BODY, fontSize=10, textColor=GOLD_DARK,
                                  fontName="Helvetica-Bold", spaceAfter=4))],
        [Paragraph(
            "Questo piano alimentare ha finalità educative ed è basato sulle linee guida LARN "
            "della Società Italiana di Nutrizione Umana. <b>Non sostituisce il parere di un medico, "
            "di un dietologo o di un biologo nutrizionista in presenza di condizioni patologiche</b> "
            "(diabete, patologie tiroidee, renali, cardiovascolari, disturbi del comportamento alimentare, "
            "gravidanza e allattamento). In caso di dubbi consulta sempre il tuo medico curante prima "
            "di iniziare un nuovo regime alimentare.",
            SMALL)],
    ], colWidths=[16.5 * cm])
    disclaimer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CREAM),
        ("LINEBEFORE", (0, 0), (0, -1), 3, GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(disclaimer)
    story.append(Spacer(1, 14))

    sig = Table([[
        Paragraph(
            "<b>Documento generato con intelligenza artificiale</b><br/>"
            "Questo piano è stato elaborato con il supporto di modelli di intelligenza "
            "artificiale a partire da riferimenti nutrizionali pubblici e tracciabili: "
            "linee guida LARN/SINU (IV revisione, 2014), riferimenti EFSA e WHO/FAO. "
            "I calcoli di fabbisogno energetico e dei macronutrienti sono deterministici e "
            "basati sull'equazione di Mifflin-St Jeor (1990), ampiamente validata in letteratura. "
            "Il piano non costituisce una diagnosi medica né sostituisce la consulenza "
            "di un medico, di un dietologo o di un biologo nutrizionista.",
            SMALL),
        Paragraph("<b>Hai domande sul tuo piano?</b><br/>"
                  "Scrivici a supporto@nutriscienza.org<br/>"
                  "Risposta entro 48 ore lavorative", BODY),
    ]], colWidths=[10.5 * cm, 6 * cm])
    sig.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig)

    _Doc(output_path, intake).build(story)
    return output_path
