"""Traveller + trip memory (WS1).

`store` is the only supported entry point: mem0's API is synchronous and
CPU/network-bound, and `store` is what re-establishes the `asyncio.to_thread`
boundary. Importing mem0 directly from a request path would block every other
SSE stream on the instance.

Nothing here is imported at module load — `mem0` is a heavy optional dependency
and the rest of the app must keep working when memory is unavailable, so
importing this package must not pull mem0 in as a side effect.
"""
