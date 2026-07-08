"""Static code catalog (PT / OT / SLP) + modifier semantics.

Each entry: short description, the discipline(s) that bill it, and whether it
is a timed (15-minute-unit) code. Shared codes carry several disciplines —
attribution for those comes from the GP/GO/GN modifier, never the code
(§3.5/§7A.9): an unmodified shared code is "unspecified".
"""

from __future__ import annotations

PT, OT, SLP = "PT", "OT", "SLP"
UNSPECIFIED = "unspecified"

# discipline modifiers (plan-of-care attribution)
DISCIPLINE_MODIFIERS = {"GP": PT, "GO": OT, "GN": SLP}
# assistant-provided modifiers (statutory 85% payment)
ASSISTANT_MODIFIERS = {"CQ", "CO"}
# other recognized flags
CAP_EXCEPTION_MODIFIER = "KX"
DISTINCT_SERVICE_MODIFIERS = {"59", "XE", "XS", "XP", "XU"}

# code: (description, disciplines, timed)
CODE_CATALOG: dict[str, tuple[str, tuple[str, ...], bool]] = {
    # ---- PT evals (untimed) ----
    "97161": ("PT eval, low complexity", (PT,), False),
    "97162": ("PT eval, moderate complexity", (PT,), False),
    "97163": ("PT eval, high complexity", (PT,), False),
    "97164": ("PT re-evaluation", (PT,), False),
    # ---- OT evals (untimed) ----
    "97165": ("OT eval, low complexity", (OT,), False),
    "97166": ("OT eval, moderate complexity", (OT,), False),
    "97167": ("OT eval, high complexity", (OT,), False),
    "97168": ("OT re-evaluation", (OT,), False),
    # ---- shared PT/OT treatment (timed unless noted) ----
    "97110": ("Therapeutic exercises", (PT, OT), True),
    "97112": ("Neuromuscular re-education", (PT, OT), True),
    "97140": ("Manual therapy", (PT, OT), True),
    "97150": ("Group therapeutic procedures", (PT, OT), False),
    "97530": ("Therapeutic activities", (PT, OT), True),
    "97535": ("Self-care/home management training", (PT, OT), True),
    "97542": ("Wheelchair management training", (PT, OT), True),
    "97760": ("Orthotic management & training, initial", (PT, OT), True),
    "97761": ("Prosthetic training, initial", (PT, OT), True),
    "97763": ("Orthotic/prosthetic management, established", (PT, OT), True),
    # ---- PT-leaning treatment ----
    "97113": ("Aquatic therapy w/ exercises", (PT,), True),
    "97116": ("Gait training", (PT,), True),
    "97124": ("Massage therapy", (PT,), True),
    "97750": ("Physical performance test", (PT, OT), True),
    # ---- OT-specific ----
    "97129": ("Cognitive function intervention, first 15 min", (OT, SLP), True),
    "97130": ("Cognitive function intervention, each addl 15 min", (OT, SLP), True),
    "97533": ("Sensory integrative techniques", (OT,), True),
    "97537": ("Community/work reintegration training", (OT,), True),
    # ---- PT modalities ----
    "97010": ("Hot/cold packs", (PT, OT), False),
    "97012": ("Mechanical traction", (PT,), False),
    "97014": ("Electrical stimulation (unattended)", (PT,), False),
    "97016": ("Vasopneumatic devices", (PT,), False),
    "97018": ("Paraffin bath", (PT, OT), False),
    "97022": ("Whirlpool", (PT,), False),
    "97026": ("Infrared therapy", (PT,), False),
    "97032": ("Electrical stimulation (manual)", (PT,), True),
    "97033": ("Iontophoresis", (PT, OT), True),
    "97035": ("Ultrasound therapy", (PT,), True),
    "97036": ("Hubbard tank", (PT,), True),
    "G0283": ("Electrical stimulation, non-wound (HCPCS)", (PT,), False),
    # ---- SLP evals (untimed) ----
    "92521": ("Evaluation of speech fluency", (SLP,), False),
    "92522": ("Evaluation of speech sound production", (SLP,), False),
    "92523": ("Speech sound production w/ language eval", (SLP,), False),
    "92524": ("Behavioral analysis of voice and resonance", (SLP,), False),
    # ---- SLP treatment ----
    "92507": ("Speech/hearing therapy, individual", (SLP,), False),
    "92508": ("Speech/hearing therapy, group", (SLP,), False),
    "92526": ("Swallowing dysfunction treatment", (SLP,), False),
    # ---- swallowing studies ----
    "92610": ("Swallowing function evaluation", (SLP,), False),
    "92611": ("Motion fluoroscopic swallow study", (SLP,), False),
    "92612": ("Flexible endoscopic swallow eval (FEES)", (SLP,), False),
    "92613": ("FEES interpretation and report", (SLP,), False),
    "92614": ("Laryngeal sensory testing", (SLP,), False),
    "92615": ("Laryngeal sensory testing interpretation", (SLP,), False),
    "92616": ("FEES w/ laryngeal sensory testing", (SLP,), False),
    "92617": ("FEES w/ sensory testing interpretation", (SLP,), False),
    # ---- auditory rehab ----
    "92626": ("Auditory function evaluation, first hour", (SLP,), False),
    "92627": ("Auditory function evaluation, each addl 15 min", (SLP,), True),
    # ---- AAC / voice ----
    "92597": ("Voice prosthetic evaluation", (SLP,), False),
    "92605": ("Non-speech-generating AAC device eval, first hour", (SLP,), False),
    "92606": ("Non-speech-generating AAC device services", (SLP,), False),
    "92607": ("Speech-generating AAC device eval, first hour", (SLP,), False),
    "92608": ("Speech-generating AAC device eval, each addl 30 min", (SLP,), True),
    "92609": ("Speech-generating AAC device programming", (SLP,), False),
}

DEFAULT_CODE_SET = list(CODE_CATALOG.keys())


def code_info(code: str) -> tuple[str, tuple[str, ...], bool]:
    return CODE_CATALOG.get(code, ("", (), False))


def resolve_discipline(code: str, modifiers: list[str]) -> str:
    """Modifier-first discipline attribution (§3.5).

    GP/GO/GN wins. Without one, a single-discipline code attributes by code;
    a shared code is honestly 'unspecified' — never guessed.
    """
    for m in modifiers:
        if m in DISCIPLINE_MODIFIERS:
            return DISCIPLINE_MODIFIERS[m]
    _, disciplines, _ = code_info(code)
    if len(disciplines) == 1:
        return disciplines[0]
    return UNSPECIFIED


def is_timed(code: str) -> bool:
    return code_info(code)[2]


def catalog_json() -> dict:
    """code -> {description, disciplines, timed} for the dashboard."""
    return {
        c: {"description": d, "disciplines": list(ds), "timed": t}
        for c, (d, ds, t) in CODE_CATALOG.items()
    }
