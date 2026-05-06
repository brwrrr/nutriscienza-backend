"""
Generazione del piano alimentare via Claude — multi-settimana per tier.

  - Base     → 1 settimana   (1 chiamata)
  - Completo → 4 settimane   (4 chiamate, ognuna evita le ricette delle precedenti)
  - Coach    → 12 settimane  (12 chiamate periodizzate in 3 fasi da 4 settimane)

L'LLM riceve sempre:
  - profilo del cliente (intake)
  - target nutrizionali calcolati deterministicamente (NON ricalcola le kcal)
  - vincoli rigidi (allergie, dieta, protocollo, tempo cucina)
  - contesto della settimana (numero, fase, ricette già usate da evitare)

I valori calorici e i macro sono già stati calcolati in nutrition.py.
Claude deve solo costruire i pasti che li rispettano.
"""
import json
import re
from typing import Any

import anthropic

from .config import settings
from .models import (
    IntakeRequest,
    MealPlan,
    NutritionTargets,
    WeekPlan,
    WorkoutPlan,
)


WEEKS_BY_PLAN = {"base": 1, "completo": 4, "coach": 12}


# Periodizzazione coach: 12 settimane suddivise in 3 fasi
def _coach_phase(week_number: int) -> tuple[str, str]:
    """Ritorna (nome_fase, focus_calorico_per_la_settimana) per piano Coach."""
    if 1 <= week_number <= 4:
        return ("Adattamento", "ricostruzione metabolica e abitudini")
    if 5 <= week_number <= 8:
        return ("Sviluppo", "carico nutrizionale progressivo per la composizione corporea")
    return ("Picco", "intensificazione e affinamento — settimana di scarico ogni 4")


SYSTEM_PROMPT = """Sei l'engine di generazione di NutriScienza, un servizio italiano automatico \
di piani alimentari personalizzati che applica le linee guida LARN/SINU e l'equazione Mifflin-St Jeor. \
Non sei un professionista sanitario abilitato e non firmi il documento — il piano è un output educativo, \
non sostituisce il parere di un medico, di un dietologo o di un biologo nutrizionista. \
Costruisci settimane di 7 giorni in cucina mediterranea italiana, realistiche e sostenibili. \
Rispondi SEMPRE e SOLO con JSON valido (nessun testo prima o dopo, nessun blocco markdown). \
Le kcal di ogni pasto devono sommare entro ±5% al target giornaliero indicato. \
Rispetta tassativamente allergie, dieta e protocollo richiesti."""


def _build_week_prompt(
    intake: IntakeRequest,
    targets: NutritionTargets,
    week_number: int,
    total_weeks: int,
    phase: str | None,
    phase_focus: str | None,
    used_dishes: list[str],
) -> str:
    allergies = ", ".join(intake.allergies) if intake.allergies else "nessuna"
    proto = intake.protocol or "alimentazione mediterranea bilanciata (default)"
    dislikes = intake.dislikes or "nessuno"
    target_low = int(targets.target_kcal * 0.95)
    target_high = int(targets.target_kcal * 1.05)

    # Sezione "settimana N di M" — fa capire al modello dove siamo nel programma
    week_context = f"Stai costruendo la SETTIMANA {week_number} DI {total_weeks}"
    if phase:
        week_context += f" — fase «{phase}»"
        if phase_focus:
            week_context += f" ({phase_focus})"
    week_context += "."

    # Lista ricette già usate nelle settimane precedenti — devono essere evitate
    avoid_block = ""
    if used_dishes:
        # Limita per non esplodere i token, ma con 12 settimane × 7 giorni × 3 pasti = 252
        # serve un budget generoso
        avoid_list = ", ".join(used_dishes[-200:])
        avoid_block = f"""
RICETTE GIÀ UTILIZZATE NELLE SETTIMANE PRECEDENTI (NON RIPETERLE):
{avoid_list}

Devi proporre piatti DIVERSI da questi, mantenendo lo stesso target calorico.
"""

    return f"""{week_context}

PROFILO
- Nome: {intake.first_name}
- Età: {intake.age}, Sesso: {'Donna' if intake.sex == 'F' else 'Uomo'}
- Altezza: {intake.height} cm, Peso: {intake.weight} kg
- Obiettivo: {intake.goal}

TARGET CALCOLATI (NON RICALCOLARE — usa questi)
- Target giornaliero: {targets.target_kcal} kcal (banda accettabile: {target_low}-{target_high})
- Proteine: {targets.protein_g} g/giorno
- Carboidrati: {targets.carbs_g} g/giorno
- Grassi: {targets.fat_g} g/giorno

VINCOLI RIGIDI
- Tipo di alimentazione: {intake.diet}
- Protocollo: {proto}
- Allergie / intolleranze (DA EVITARE): {allergies}
- Cibi non graditi: {dislikes}
- Pasti al giorno richiesti: {intake.meals}
- Tempo per cucinare: {intake.cooking_time}
- Budget settimanale: {intake.budget}
- Pasti fuori casa a settimana: {intake.meals_out}
{avoid_block}
ISTRUZIONI
1. 7 giorni: Lunedì → Domenica. Ogni giorno ha pasti coerenti con "pasti al giorno richiesti".
   - "3" → Colazione, Pranzo, Cena
   - "3+1" → + Spuntino pomeriggio
   - "3+2" → + Spuntino mattina e Spuntino pomeriggio
   - "5+" → 5 pasti distribuiti
2. Ogni pasto è descritto in italiano con grammature a crudo (es. "Pasta integrale 70 g con...").
3. Ricette mediterranee, ingredienti facilmente reperibili in supermercati italiani.
4. Domenica: includi un piatto della tradizione italiana mantenendo il target calorico.
5. Lista della spesa raggruppata in categorie (Verdura/frutta, Carne/pesce/uova, Latticini, \
Cereali/legumi, Dispensa) con quantità per i 7 giorni di QUESTA settimana.
6. Riepilogo settimanale in 2-3 frasi, riferito a questa specifica settimana.

FORMATO DI OUTPUT (JSON, niente altro)
{{
  "label": "Settimana {week_number}{' — ' + phase if phase else ''}",
  "days": [
    {{
      "label": "Lunedì",
      "total_kcal": 1690,
      "meals": [
        {{"name": "Colazione", "description": "...", "kcal": 350}},
        {{"name": "Pranzo", "description": "...", "kcal": 540}},
        {{"name": "Cena", "description": "...", "kcal": 470}}
      ]
    }}
    // ... altri 6 giorni
  ],
  "shopping_list": [
    {{"name": "Verdura e frutta fresca", "items": ["Pomodorini · 500 g", "Mele · 4", ...]}},
    {{"name": "Carne, pesce e uova", "items": [...]}},
    {{"name": "Latticini", "items": [...]}},
    {{"name": "Cereali, legumi e pane", "items": [...]}},
    {{"name": "Dispensa", "items": [...]}}
  ],
  "weekly_summary": "Media settimanale ~X kcal/giorno..."
}}

I valori name dei pasti DEVONO essere uno tra: \
"Colazione", "Spuntino mattina", "Pranzo", "Spuntino pomeriggio", "Cena". Niente altro.
Output JSON ora:"""


def _build_tips_prompt(intake: IntakeRequest, targets: NutritionTargets, total_weeks: int) -> str:
    """Prompt per generare i 5 consigli metodologici del piano — globali, non per settimana."""
    return f"""Genera 5 consigli metodologici pertinenti per questo cliente che sta seguendo \
un piano alimentare di {total_weeks} settimana/e. I consigli sono indicazioni generali educative, \
non parere clinico individualizzato.

CLIENTE: {intake.first_name}, {intake.age} anni, obiettivo {intake.goal}, \
target {targets.target_kcal} kcal/giorno, {intake.workouts} allenamenti/sett.

I 5 consigli devono coprire: idratazione, timing nutrienti, sonno, misurazione progressi, sostenibilità.
Rispondi SOLO con JSON, niente altro.

FORMATO:
{{
  "nutritionist_tips": [
    {{"title": "Idratazione", "text": "..."}},
    {{"title": "Timing dei carboidrati", "text": "..."}},
    {{"title": "Sonno e recupero", "text": "..."}},
    {{"title": "Misura i progressi", "text": "..."}},
    {{"title": "Sostenibilità nel tempo", "text": "..."}}
  ]
}}

Output JSON ora:"""


def _strip_to_json(text: str) -> str:
    """Estrae il blocco JSON anche se l'LLM aggiunge testo o markdown attorno."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _call_claude(client: anthropic.Anthropic, prompt: str, max_tokens: int = 8000) -> dict[str, Any]:
    """Chiama Claude e ritorna il dict JSON parsed. Solleva ValueError su parsing failure."""
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    json_text = _strip_to_json(raw)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM non ha restituito JSON valido: {e}\n--- raw ---\n{raw[:500]}")


def _extract_dish_signatures(week_data: dict[str, Any]) -> list[str]:
    """
    Estrae i nomi dei piatti principali dalla settimana per evitarne la ripetizione.
    Prendiamo le prime 6-8 parole della description di Pranzo e Cena (i pasti principali).
    """
    signatures: list[str] = []
    for day in week_data.get("days", []):
        for meal in day.get("meals", []):
            if meal.get("name") in ("Pranzo", "Cena"):
                desc = (meal.get("description") or "").strip()
                # Prima frase / prime ~10 parole — abbastanza per identificare il piatto
                first_chunk = re.split(r"[.,;]", desc)[0]
                words = first_chunk.split()
                if words:
                    signatures.append(" ".join(words[:8]).strip())
    return signatures


def generate_meal_plan(intake: IntakeRequest, targets: NutritionTargets) -> MealPlan:
    """
    Genera il piano completo per il tier scelto.

    - Base: 1 chiamata (1 settimana)
    - Completo: 4 chiamate (4 settimane, evita ripetizioni)
    - Coach: 12 chiamate (12 settimane periodizzate, evita ripetizioni)

    Una chiamata aggiuntiva genera i 5 consigli globali.
    Solleva ValueError se l'output non è parseable o non rispetta lo schema.
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    total_weeks = WEEKS_BY_PLAN.get(intake.plan, 1)

    weeks: list[WeekPlan] = []
    used_dishes: list[str] = []

    for week_num in range(1, total_weeks + 1):
        # Per Coach: aggiungi fase + focus
        phase, phase_focus = (None, None)
        if intake.plan == "coach":
            phase, phase_focus = _coach_phase(week_num)

        prompt = _build_week_prompt(
            intake=intake,
            targets=targets,
            week_number=week_num,
            total_weeks=total_weeks,
            phase=phase,
            phase_focus=phase_focus,
            used_dishes=used_dishes,
        )

        week_data = _call_claude(client, prompt)
        # Inserisci campi mancanti che la validazione si aspetta
        week_data["week_number"] = week_num
        if phase:
            week_data["phase"] = phase
        if "label" not in week_data or not week_data["label"]:
            week_data["label"] = f"Settimana {week_num}" + (f" — {phase}" if phase else "")

        week = WeekPlan.model_validate(week_data)
        weeks.append(week)

        # Aggiorna lista anti-ripetizione per le settimane successive
        used_dishes.extend(_extract_dish_signatures(week_data))

    # Ultimo step: 5 consigli globali al piano
    tips_data = _call_claude(client, _build_tips_prompt(intake, targets, total_weeks), max_tokens=2000)
    nutritionist_tips = tips_data.get("nutritionist_tips", [])

    return MealPlan(weeks=weeks, nutritionist_tips=nutritionist_tips)


# ---------- Programma di allenamento (Completo + Coach) ----------

WORKOUT_SYSTEM_PROMPT = """Sei un preparatore atletico per NutriScienza. \
Costruisci programmi di allenamento personalizzati, sicuri ed efficaci. \
Rispondi SEMPRE e SOLO con JSON valido (nessun testo prima o dopo, nessun blocco markdown)."""


def _sessions_per_week(workouts_field: str) -> int:
    """Mappa il campo 'workouts' del questionario al numero di sessioni a settimana."""
    return {
        "0": 2,        # se non si allena, partiamo con 2 sessioni soft
        "1-2": 2,
        "3-4": 3,
        "5-6": 4,
        "7+": 5,
    }.get(workouts_field, 3)


def _build_workout_prompt(
    intake: IntakeRequest, targets: NutritionTargets, total_weeks: int, periodized: bool
) -> str:
    sessions = _sessions_per_week(intake.workouts)

    if periodized:
        structure_block = (
            "PERIODIZZAZIONE (12 settimane, 3 blocchi da 4):\n"
            "- Settimane 1-4: «Adattamento» — volume moderato, RPE 6-7, focus sulla tecnica e ricostruzione.\n"
            "- Settimane 5-8: «Sviluppo» — volume e carico crescente, RPE 7-8, progressione doppia.\n"
            "- Settimane 9-12: «Picco» — intensità alta, RPE 8-9, l'ultima settimana è di scarico (deload).\n"
        )
    else:
        structure_block = (
            "STRUTTURA (4 settimane):\n"
            "Stessa struttura di sessioni in tutte e 4 le settimane, con leggera progressione "
            "di volume o carico settimana dopo settimana (es. +1 ripetizione, o +2.5 kg sui multiarticolari).\n"
        )

    return f"""Costruisci un programma di allenamento di {total_weeks} settimane per questo cliente.

{structure_block}

PROFILO
- {intake.first_name}, {intake.age} anni, {'Donna' if intake.sex == 'F' else 'Uomo'}, {intake.weight} kg, {intake.height} cm
- Obiettivo: {intake.goal}
- Allenamenti già abituali a settimana: {intake.workouts}
- Sessioni a settimana per questo programma: {sessions} (rispetta esattamente)
- Livello di attività dichiarato: {intake.activity}

LINEE GUIDA
- Esercizi multiarticolari prima degli isolamenti.
- Sicuri ed eseguibili in palestra commerciale (no attrezzi rari).
- Indica sempre serie x ripetizioni, recupero in secondi, e note brevi su tecnica/RPE.
- Per principianti (workouts «0» o «1-2»): 8-12 reps, RPE 6-7, esercizi base, progressione lineare.
- Per intermedi/avanzati: schemi a obiettivo (forza 4-6 reps, ipertrofia 8-12, condizionamento 12-20).
- Adatta agli obiettivi: dimagrire → maggior volume e accenno HIIT; massa → forza + ipertrofia; mantenere/salute → mix bilanciato.

FORMATO DI OUTPUT (JSON, niente altro)
{{
  "methodology": "Paragrafo (3-5 frasi) sul metodo: split scelto, principi di progressione, perché funziona per l'obiettivo.",
  "progression_notes": "Paragrafo (3-5 frasi) su come gestire la progressione dei carichi nel tempo: aggiunta peso, double-progression, gestione plateau, deload.",
  "weeks": [
    {{
      "week_number": 1,
      "phase": "{'Adattamento' if periodized else 'Progressione'}",
      "week_focus": "Frase breve sull'obiettivo specifico di questa settimana",
      "sessions": [
        {{
          "label": "Sessione A — Forza Upper",
          "duration_min": 60,
          "focus": "Pettorali, spalle, tricipiti",
          "exercises": [
            {{"name": "Panca piana con bilanciere", "sets_reps": "4 x 6-8", "rest": "120s", "notes": "RPE 7, controlla la fase eccentrica"}}
          ]
        }}
      ]
    }}
  ]
}}

Output JSON ora:"""


def generate_workout_plan(
    intake: IntakeRequest, targets: NutritionTargets
) -> WorkoutPlan | None:
    """
    Genera il programma di allenamento per i tier che lo includono.
    Ritorna None per il piano base (no workout incluso).
    """
    if intake.plan == "base":
        return None

    total_weeks = WEEKS_BY_PLAN[intake.plan]
    periodized = intake.plan == "coach"

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8000,
        system=WORKOUT_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": _build_workout_prompt(intake, targets, total_weeks, periodized),
        }],
    )
    raw = response.content[0].text
    json_text = _strip_to_json(raw)
    try:
        data: dict[str, Any] = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Workout: JSON non valido: {e}\n--- raw ---\n{raw[:500]}")

    return WorkoutPlan.model_validate(data)
