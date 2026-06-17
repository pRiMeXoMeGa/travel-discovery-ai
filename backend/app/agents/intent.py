"""Intent agent: natural language -> structured query.

Powers both the NL search bar (parse -> apply filters, update chips) and the
first step of the concierge. Uses structured-output LLM calls (no free-text
parsing).
"""
from ..schemas import StructuredQuery


async def parse_intent(query: str) -> StructuredQuery:
    """Turn NL into a StructuredQuery.

    TODO: call llm.complete_json with the StructuredQuery schema and a prompt
    that extracts city, dates, budget, party size, vibe, hard constraints, and
    soft preferences. Validate + coerce dates ("late June" -> a date range).
    """
    raise NotImplementedError("TODO: intent parsing")
