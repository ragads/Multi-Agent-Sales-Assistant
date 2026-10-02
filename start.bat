@echo off
rem Starts the whole system with the project's .venv: both MCP servers, then the API.
rem Each server opens in its own window; close a window (or press Ctrl+C in it) to stop that server.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo .venv not found. Create it first:  py -3.13 -m venv .venv  then  .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem self-hosted Langfuse tracing (only if it was set up with scripts\setup_langfuse.py and Docker is running)
if exist "observability\langfuse.env" (
    docker compose -f observability\docker-compose.langfuse.yml --env-file observability\langfuse.env up -d >nul 2>&1 ^
        && echo Langfuse: http://localhost:3000 ^
        || echo Langfuse not started - open Docker Desktop, then run start.bat again for tracing
)

start "CloseFuture - calendar MCP (8931)" cmd /k ".venv\Scripts\python -m app.mcp_servers.calendar_server"
start "CloseFuture - email MCP (8932)" cmd /k ".venv\Scripts\python -m app.mcp_servers.email_server"

rem give the MCP servers a few seconds so the API finds their tools at startup
timeout /t 5 /nobreak >nul

start "CloseFuture - API (8000)" cmd /k ".venv\Scripts\python -m uvicorn app.main:app --port 8000"

echo Started. Chat: http://localhost:8000/widget   Health: http://localhost:8000/health
