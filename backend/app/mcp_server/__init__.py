"""MCP server (WS2 outbound): exposes the platform as tools other agents
can call, mounted into the existing FastAPI app at /mcp.

Deliberately empty beyond this docstring — `server.py` imports `fastmcp`,
which is only guaranteed to be installed in the full Docker image
(`backend/requirements.txt`), not under plain `requirements-dev.txt` (see
that file's own docstring: real CI installs only requirements-dev.txt).
Importing anything from `fastmcp` here at package-import time would break
every test in the suite that imports `app.*` under that lighter
environment, the same reason `app/main.py`'s import of `app.memory.store`
(mem0, ~117MB of transitive imports) is deferred into the lifespan function
body instead of sitting at module top. `app/main.py` guards its import of
`mcp_server.server`/`mcp_server.auth` with try/except ImportError for the
same reason — see the comment there.
"""
