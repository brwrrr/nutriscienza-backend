"""
Generazione del piano alimentare via Claude.

L'LLM riceve:
  - profilo del cliente (intake)
  - target nutrizionali calcolati deterministicamente (NON ricalcola le kcal)
  - vincoli rigidi (allergie, dieta, protocollo, tempo cucina)

E ritorna un MealPlan strutturato come JSON.

Importante: i valori calorici e i macro sono già stati calcolati in nutrition.py.
Claude deve solo costruire i pasti che li rispettano. Non delegare i calcoli al modello.
"""
import json
import re
from typing import Any

import anthropic

from .config import settings
from .models import IntakeRequest, MealPlan, NutritionTargets


SYSTEM_PROMPT = """Sei un nutrizionista virtuale che lavora per NutriScienza, un servizio italiano \
di piani alimentari personalizzati basati sulle linee guida LARN/SINU. \
Costruisci piani settimanali di 7 giorni in cucina mediterranea italiana, realistici e sostenibili. \
Rispondi SEMPRE e SOLO con JSON valido (nessun testo prima o dopo, nessun blocco markdown). \
Le kcal di ogni pasto devono sommare entro ±5% al target giornaliero indicato. \
Rispetta tassativamente allergie, dieta e protocollo richiesti."""


def _build_user_prompt(intake: IntakeRequest, targets: NutritionTargets) -> str:
    allergies = ", ".join(intake.allergies) if intake.allergies else "nessuna"
    proto = intake.protocol or "alimentazione mediterranea bilanciata (default)"
    dislikes = intake.dislikes or "nessuno"
    target_low = int(targets.target_kcal * 0.95)
    target_high = int(targets.target_kcal * 1.05)

    return f"""Costruisci il piano alimentare settimanale per questo cliente.

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

ISTRUZIONI
1. 7 giorni: Lunedì → Domenica. Ogni giorno ha pasti coerenti con "pasti al giorno richiesti".
   - "3" → Colazione, Pranzo, Cena
   - "3+1" → + Spuntino pomeriggio
   - "3+2" → + Spuntino mattina e Spuntino pomeriggio
   - "5+" → 5 pasti distribuiti
2. Ogni pasto è descritto in italiano con grammature a crudo (es. "Pasta integrale 70 g con...").
3. Ricette mediterranee, ingredienti facilmente reperibili in supermercati italiani.
4. Domenica: includi un piatto della tradizione italiana (lasagna, pasta al forno, arrosto, ecc.) \
mantenendo il target calorico — la flessibilità è parte del metodo.
5. Lista della spesa raggruppata in categorie (Verdura/frutta, Carne/pesce/uova, Latticini, \
Cereali/legumi, Dispensa) con quantità per 7 giorni.
6. 5 consigli del nutrizionista pertinenti all'obiettivo del cliente (idratazione, timing nutrienti, \
sonno, misurazione progressi, sostenibilità).
7. Riepilogo settimanale in 2-3 frasi.

FORMATO DI OUTPUT (JSON, niente altro)
{{
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
  "weekly_summary": "Media settimanale ~X kcal/giorno...",
  "nutritionist_tips": [
    {{"title": "Idratazione", "text": "..."}},
    {{"title": "Timing dei carboidrati", "text": "..."}},
    {{"title": "...", "text": "..."}},
    {{"title": "...", "text": "..."}},
    {{"title": "...", "text": "..."}}
  ]
}}

I valori name dei pasti DEVONO essere uno tra: \
"Colazione", "Spuntino mattina", "Pranzo", "Spuntino pomeriggio", "Cena". Niente altro.
Output JSON ora:"""


def _strip_to_json(text: str) -> str:
    """Estrae il blocco JSON anche se l'LLM aggiunge testo o markdown attorno."""
    # Rimuovi blocchi markdown ```json ... ```
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    # Trova primo { e ultimo } accoppiati
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def generate_meal_plan(intake: IntakeRequest, targets: NutritionTargets) -> MealPlan:
    """
    Chiama Claude e ritorna un MealPlan validato.
    Solleva ValueError se l'output non è parseable o non rispetta lo schema.
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(intake, targets)}],
    )

    raw = response.content[0].text
    json_text = _strip_to_json(raw)

    try:
        data: dict[str, Any] = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM non ha restituito JSON valido: {e}\n--- raw ---\n{raw[:500]}")

    return MealPlan.model_validate(data)
