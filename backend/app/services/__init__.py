"""Service layer (WS0-C): shared business logic for listings, reviews,
availability, and planning.

`routers/*` (HTTP), `agents/*` (concierge), and (WS2) MCP tools all call
into this package instead of reaching into each other's private helpers.
Before this existed, `agents/orchestrator.py::_fallback_search` imported
`_build_query`/`_row_to_card` from `routers/search.py` inside the function
body to dodge a circular import, and `routers/agents.py` imported a sibling
router's endpoint function (`routers.search.search`) plus an agent's private
helper (`agents.retrieval._parse_constraints`). Both were layering
inversions — an HTTP/agent caller reaching past a module boundary into
implementation details that were never meant to be a public surface.

See `version2/V2_MASTER_PLAN.md` §WS0-C and `version2/WS2_MCP.md` Part 0.
"""
