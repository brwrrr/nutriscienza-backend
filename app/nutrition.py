"""
Calcoli nutrizionali deterministici, conformi alle linee guida LARN/SINU.

Formule:
- BMR: Mifflin-St Jeor (1990) — standard più validato per adulti sani
- TDEE: BMR × PAL (Physical Activity Level)
- Deficit/surplus: -18% per dimagrimento, +12% per massa, 0% mantenimento/salute
- Macronutrienti: proteine modulate sull'obiettivo (1.6-2.0 g/kg), poi carbs/fat split
"""
from .models import IntakeRequest, NutritionTargets


# ---------- Coefficienti ----------

PAL_MAP: dict[str, float] = {
    "sedentario": 1.20,
    "leggero": 1.375,
    "moderato": 1.55,
    "intenso": 1.725,
}

# Modulazione kcal sul TDEE in base all'obiettivo
GOAL_DELTA: dict[str, int] = {
    "dimagrire": -18,   # deficit moderato sostenibile
    "mantenere": 0,
    "massa": +12,       # surplus controllato
    "salute": 0,
}

# Apporto proteico (g/kg di peso corporeo) per obiettivo
PROTEIN_PER_KG: dict[str, float] = {
    "dimagrire": 1.8,   # protezione massa magra in deficit
    "mantenere": 1.6,
    "massa": 2.0,       # supporto sintesi proteica in surplus
    "salute": 1.4,
}


# ---------- Funzioni base ----------

def bmr_mifflin_st_jeor(sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    """
    Equazione di Mifflin-St Jeor (1990).
    Donna: BMR = 10·peso + 6.25·altezza - 5·età - 161
    Uomo:  BMR = 10·peso + 6.25·altezza - 5·età + 5
    """
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if sex == "M" else -161)


def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    h_m = height_cm / 100
    return round(weight_kg / (h_m * h_m), 1)


def bmi_label(bmi: float) -> str:
    if bmi < 18.5:
        return "sottopeso"
    if bmi < 25:
        return "normopeso"
    if bmi < 30:
        return "sovrappeso lieve" if bmi < 27 else "sovrappeso"
    return "obesità"


def macros_from_kcal(target_kcal: int, weight_kg: float, goal: str) -> tuple[int, int, int]:
    """
    Calcola grammi giornalieri di proteine, carboidrati, grassi.
    Strategia:
      1. Proteine = g/kg in base a obiettivo (kcal: 4)
      2. Grassi = ~30% delle kcal totali (kcal: 9), minimo per equilibrio ormonale
      3. Carboidrati = il resto (kcal: 4)
    """
    protein_g = round(PROTEIN_PER_KG[goal] * weight_kg)
    fat_pct = 0.30
    fat_kcal = target_kcal * fat_pct
    fat_g = round(fat_kcal / 9)
    protein_kcal = protein_g * 4
    carbs_kcal = target_kcal - protein_kcal - fat_kcal
    carbs_g = round(max(carbs_kcal, 0) / 4)
    return protein_g, carbs_g, fat_g


def percentages(target_kcal: int, p: int, c: int, f: int) -> tuple[int, int, int]:
    """Ritorna (% prot, % carb, % grassi) arrotondati a interi che sommano a 100."""
    pp = round(p * 4 / target_kcal * 100)
    cp = round(c * 4 / target_kcal * 100)
    fp = 100 - pp - cp
    return pp, cp, fp


# ---------- API ----------

def compute_targets(intake: IntakeRequest) -> NutritionTargets:
    """Pipeline completa di calcolo a partire dall'IntakeRequest."""
    bmr = bmr_mifflin_st_jeor(intake.sex, intake.weight, intake.height, intake.age)
    pal = PAL_MAP[intake.activity]
    tdee = bmr * pal

    delta = GOAL_DELTA[intake.goal]
    target_kcal = tdee * (1 + delta / 100)

    # Arrotondamento a multiplo di 10 più vicino per leggibilità
    target_kcal_int = int(round(target_kcal / 10) * 10)

    p, c, f = macros_from_kcal(target_kcal_int, intake.weight, intake.goal)
    pp, cp, fp = percentages(target_kcal_int, p, c, f)

    bmi = calculate_bmi(intake.weight, intake.height)

    return NutritionTargets(
        bmr=int(round(bmr)),
        tdee=int(round(tdee)),
        target_kcal=target_kcal_int,
        deficit_pct=delta,
        protein_g=p,
        carbs_g=c,
        fat_g=f,
        protein_pct=pp,
        carbs_pct=cp,
        fat_pct=fp,
        bmi=bmi,
        bmi_label=bmi_label(bmi),
        pal=pal,
    )


# ---------- Self-test rapido ----------

if __name__ == "__main__":
    from .models import IntakeRequest
    sample = IntakeRequest.model_validate({
        "goal": "dimagrire", "sex": "F", "age": 34, "height": 165, "weight": 68,
        "targetWeight": 63, "activity": "moderato", "workouts": "3-4", "meals": "3+2",
        "diet": "onnivora", "protocol": "", "allergies": [],
        "cookingTime": "medio", "budget": "medio", "mealsOut": "1-2",
        "plan": "base", "firstName": "Giulia", "email": "test@nutriscienza.org",
    })
    t = compute_targets(sample)
    print(t.model_dump_json(indent=2))
