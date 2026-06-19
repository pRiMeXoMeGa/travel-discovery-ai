"""Intent agent: natural language -> structured query.

Powers both the NL search bar (parse -> apply filters, update chips) and the
first step of the concierge. Uses a structured-output LLM call (responseMimeType
= application/json) — no brittle free-text parsing.

Date coercion: the model is asked to resolve vague phrases ("late June",
"next weekend") to an explicit ISO check_in/check_out range relative to a
provided reference date, so downstream availability costing is deterministic.
"""
import logging
from datetime import date

from pydantic import ValidationError

from .. import llm
from ..observability import AgentStep
from ..schemas import StructuredQuery

logger = logging.getLogger(__name__)

# Documents the desired shape for the model. Fields are intentionally optional —
# the model omits what it cannot determine rather than fabricating.
_INTENT_SCHEMA = {
    "city": "string | null  (the destination city, e.g. 'Lisbon', 'Amsterdam', 'Los Angeles')",
    "check_in": "string | null  (ISO date YYYY-MM-DD, start of stay)",
    "check_out": "string | null  (ISO date YYYY-MM-DD, end of stay, exclusive)",
    "party_size": "integer | null  (number of guests)",
    "budget_per_night": "number | null  (max nightly budget, in local currency)",
    "budget_total": "number | null  (max total trip budget)",
    "hard_constraints": "array of short strings (MUST-haves: amenities like "
    "'balcony', 'pool'; areas to include/avoid like 'avoid Deira', 'near metro'; "
    "property type like 'apartment')",
    "soft_preferences": "array of short strings (nice-to-haves)",
    "vibe": "string | null  (overall mood, e.g. 'quiet', 'luxury', 'family-friendly')",
}

_SYSTEM = (
    "You parse a traveler's natural-language request into a structured booking "
    "query. The catalog covers three cities — Amsterdam, Lisbon, Los Angeles — so "
    "normalize any city mention to one of those exact names (e.g. 'LA'/'L.A.' -> "
    "'Los Angeles'); leave city null if none is implied.\n"
    "Rules:\n"
    "- Dates: resolve relative/vague dates to explicit ISO (YYYY-MM-DD) from the "
    "reference date ('late June' -> ~Jun 22-30 of the reference year; a bare month "
    "-> a sensible week in it; 'this weekend' -> the upcoming Sat-Sun). check_out "
    "is exclusive.\n"
    "- Budget: use budget_per_night for '... a night'; budget_total for whole-trip "
    "or 'total' budgets. Numbers only (strip currency symbols). The currency is the "
    "city's local one (USD for Los Angeles, EUR for Amsterdam/Lisbon) — never convert.\n"
    "- hard_constraints = concrete must-haves: amenities ('balcony','pool','wifi'), "
    "property type ('entire place','private room','hotel'), and areas to include or "
    "avoid ('near the centre','avoid the airport'). soft_preferences = nice-to-haves.\n"
    "- vibe = overall mood ('quiet','luxury','family-friendly').\n"
    "- Never invent a city, date, or budget the user did not imply; omit any field "
    "you cannot determine rather than guessing."
)


def _build_prompt(query: str, today: date) -> str:
    return (
        f"Reference date (today): {today.isoformat()}.\n"
        f"Traveler request: \"{query}\"\n\n"
        "Extract the structured booking intent."
    )


async def parse_intent(
    query: str, today: date | None = None, step: AgentStep | None = None
) -> StructuredQuery:
    """Turn NL into a validated StructuredQuery.

    On any LLM/validation failure, returns an empty StructuredQuery (graceful
    degradation — the caller can still fall back to keyword retrieval) and
    records the error on `step` if provided.
    """
    today = today or date.today()
    prompt = _build_prompt(query, today)

    try:
        raw, usage = await llm.complete_json_with_usage(prompt, _INTENT_SCHEMA, _SYSTEM)
        if step is not None:
            step.input_tokens += usage.input_tokens
            step.output_tokens += usage.output_tokens
    except llm.LLMError as exc:
        logger.warning("intent: LLM failed (%s) — returning empty query", exc)
        if step is not None:
            step.detail = f"llm_error: {exc}"
        return StructuredQuery()

    # Normalize before validation: drop nulls, coerce stray scalars to lists.
    cleaned = {k: v for k, v in raw.items() if v is not None}
    for list_field in ("hard_constraints", "soft_preferences"):
        val = cleaned.get(list_field)
        if isinstance(val, str):
            cleaned[list_field] = [val]
        elif val is None:
            cleaned.pop(list_field, None)

    try:
        return StructuredQuery(**cleaned)
    except ValidationError as exc:
        logger.warning("intent: validation failed (%s) — partial parse", exc)
        # Best-effort: keep only fields that validate individually.
        safe: dict = {}
        for key in StructuredQuery.model_fields:
            if key in cleaned:
                try:
                    StructuredQuery(**{key: cleaned[key]})
                    safe[key] = cleaned[key]
                except ValidationError:
                    continue
        return StructuredQuery(**safe)
