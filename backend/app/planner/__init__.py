"""LangGraph trip planner (WS3).

A SECOND orchestration approach, deliberately — not a replacement for the
4-agent concierge in `app/agents/orchestrator.py`.

The concierge is a short, mostly-linear route (intent -> one route runner ->
answer) where what mattered was first-class SSE step streaming and exact
per-step token accounting. A hand-rolled async generator gives both directly;
a graph framework would have been overhead around a straight line.

This flow is genuinely graph-shaped and the concierge's DAG cannot express it:

  * a **cycle** — plan exceeds budget, relax and replan
  * **conditional routing** — a preference conflict routes to clarification
  * a **human-in-the-loop interrupt** — present the plan, wait for approve or
    adjust before committing
  * a **checkpointer** — state survives across turns, and across the Render
    free tier spinning the instance down between them

Both properties LangGraph does not give for free are preserved here: per-node
SSE step events, and a per-node error guard that degrades the stream instead of
crashing it — the same contract every agent in the concierge already honours.

Nothing here is imported at module load: `langgraph` is a heavy dependency and
the rest of the app must keep working without it.
"""
