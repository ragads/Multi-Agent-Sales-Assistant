#!/bin/sh
# Single-container launcher (used on Render / Fly / any one-container host).
# Both MCP servers run beside the API on localhost; the API listens on $PORT.
python -m app.mcp_servers.calendar_server &
python -m app.mcp_servers.email_server &
sleep 4
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
