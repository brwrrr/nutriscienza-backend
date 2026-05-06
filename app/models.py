"""Pydantic models — input questionario, risultati nutrizionali, piano generato."""
from typing import Literal, Optional
from pydantic import BaseModel, EmailStr, Field, field_validator


# ---------- Input dal questionario ----------

Goal = Literal["dimagrire", "mantenere", "massa", "salute"]
Sex = Literal["F", "M"]
Activity = Literal["sedentario", "leggero", "moderato", "intenso"]
Diet = Literal["onnivora", "pescetariana", "vegetariana", "vegana"]
Plan = Literal["base", "completo", "coach"]


class IntakeRequest(BaseModel):
    """Payload completo dal questionario front-end."""

    # Step 1
    goal: Goal

    # Step 2
    sex: Sex
    age: int = Field(ge=16, le=90)
    height: int = Field(ge=120, le=220, description="cm")
    weight: float = Field(ge=35, le=250, description="kg")
    target_weight: Optional[float] = Field(default=None, ge=35, le=250, alias="targetWeight")

    # Step 3
    activity: Activity
    workouts: str  # "0", "1-2", "3-4", "5-6", "7+"
    meals: str     # "3", "3+1", "3+2", "5+"

    # Step 4
    diet: Diet
    protocol: Optional[str] = None  # "", "mediterranea", "cheto", "lowcarb", "if16", "if18"
    allergies: list[str] = Field(default_factory=list)
    dislikes: Optional[str] = None

    # Step 5
    cooking_time: Literal["poco", "medio", "molto"] = Field(alias="cookingTime")
    budget: Literal["basso", "medio", "alto"]
    meals_out: str = Field(alias="mealsOut")  # "0", "1-2", "3-5", "6+"

    # Step 6
    plan: Plan
    first_name: str = Field(alias="firstName", min_length=1, max_length=80)
    email: EmailStr

    # Programma affiliati — opzionale. Catturato dal frontend dal param ?ref=
    # in landing page e propagato al checkout. Nessun impatto se assente.
    affiliate_ref: Optional[str] = Field(default=None, alias="affiliateRef", max_length=40)

    model_config = {"populate_by_name": True}

    @field_validator("dislikes", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


# ---------- Calcoli nutrizionali ----------

class NutritionTargets(BaseModel):
    """Output del modulo nutrition.py — calcoli deterministici."""
    bmr: int                # kcal — metabolismo basale
    tdee: int               # kcal — fabbisogno con attività
    target_kcal: int        # kcal/giorno target
    deficit_pct: int        # 0 = mantenimento, -18 = cut, +12 = bulk
    protein_g: int
    carbs_g: int
    fat_g: int
    protein_pct: int
    carbs_pct: int
    fat_pct: int
    bmi: float
    bmi_label: str          # "sottopeso" | "normopeso" | "sovrappeso lieve" | "obesità"
    pal: float              # 1.2 - 1.725


# ---------- Piano generato dall'LLM ----------

class Meal(BaseModel):
    name: Literal["Colazione", "Spuntino mattina", "Pranzo", "Spuntino pomeriggio", "Cena"]
    description: str
    kcal: int


class Day(BaseModel):
    label: str        # "Lunedì", "Martedì", ...
    meals: list[Meal]
    total_kcal: int


class ShoppingCategory(BaseModel):
    name: str
    items: list[str]


class WeekPlan(BaseModel):
    """Una singola settimana del piano (7 giorni + lista spesa di settimana)."""
    week_number: int                          # 1, 2, 3, ...
    label: str                                # "Settimana 1" o "Settimana 1 — Adattamento"
    phase: Optional[str] = None               # solo per coach: "Adattamento", "Ipertrofia", "Picco"
    days: list[Day]
    shopping_list: list[ShoppingCategory]
    weekly_summary: str


class MealPlan(BaseModel):
    """Container del piano alimentare. 1 settimana per Base, 4 per Completo, 12 per Coach."""
    weeks: list[WeekPlan]
    nutritionist_tips: list[dict]             # [{"title": "...", "text": "..."}] — globali


# ---------- Programma di allenamento (completo + coach) ----------

class Exercise(BaseModel):
    name: str                                 # "Squat con bilanciere"
    sets_reps: str                            # "4 x 6-8" o "3 x 12"
    rest: str                                 # "120s" / "90s"
    notes: Optional[str] = None               # tecnica, RPE, sostituzioni


class WorkoutSession(BaseModel):
    label: str                                # "Sessione A — Forza Upper"
    duration_min: int                         # durata stimata
    focus: str                                # "Pettorali, spalle, tricipiti"
    exercises: list[Exercise]


class WorkoutWeek(BaseModel):
    week_number: int
    phase: str                                # "Adattamento", "Ipertrofia", "Forza", "Picco"
    week_focus: str                           # frase breve sull'obiettivo della settimana
    sessions: list[WorkoutSession]


class WorkoutPlan(BaseModel):
    """Programma di allenamento. 4 settimane per Completo, 12 per Coach."""
    weeks: list[WorkoutWeek]
    methodology: str                          # paragrafo introduttivo sul metodo
    progression_notes: str                    # come gestire la progressione dei carichi


# ---------- Order record ----------

class OrderStatus(BaseModel):
    id: str
    intake: IntakeRequest
    targets: NutritionTargets
    plan_chosen: Plan
    email: str
    status: Literal["pending_payment", "paid", "generating", "sent", "failed"] = "pending_payment"
    stripe_session_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    pdf_path: Optional[str] = None
    error: Optional[str] = None
