"""Traveller memory endpoints (WS1) — backs the memory panel's forget button.

Only one route: DELETE /api/memory/{memory_id}. `app.memory.store` is
imported lazily, inside the handler rather than at module top, because it
pulls in mem0 (~117MB of imports) — and this router module is imported by
`app.main` at collection time by every test in the suite, most of which
never touch memory (see app/main.py's lifespan for the same reasoning).
"""
import logging

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api", tags=["memory"])

logger = logging.getLogger(__name__)


@router.delete("/memory/{memory_id}", status_code=204)
async def forget_memory(
    memory_id: str,
    user_id: str | None = Query(default=None),
) -> None:
    """Delete one memory. Matches frontend/lib/api.ts::forgetMemory exactly:
    `DELETE /api/memory/{memory_id}?user_id=<uuid>`.

    `user_id` is accepted only to match that request shape — it is NOT
    passed to `store.forget`, which deletes by mem0 memory id alone with no
    ownership check. This is deliberate, not an oversight: `user_id` is a
    localStorage UUID minted by the browser, not an authenticated session.
    There is no auth on this app, so this endpoint is NOT authorization-
    bearing — any caller that knows a memory_id can delete it regardless of
    which user_id (if any) they pass. Do not read a security guarantee into
    this parameter.

    The memory panel's forget button is optimistic: the row is hidden
    client-side immediately and only restored if this call fails. So a
    failed delete MUST be a non-2xx response — `store.forget()` returns
    `bool` and swallows its own exceptions (mem0 outage, unknown id, etc.
    are indistinguishable from here), so `False` is mapped to a 502
    (upstream/memory-store failure) rather than ever returning 200 on a
    delete that didn't happen.
    """
    from ..memory import store  # see module docstring: lazy, mem0 is heavy

    ok = await store.forget(memory_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to delete memory")
