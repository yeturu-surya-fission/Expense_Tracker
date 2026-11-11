"""Local launcher:  python run.py

Why not plain `uvicorn app.main:app`? On Windows uvicorn uses the Proactor event loop, which
psycopg (LangGraph's Postgres checkpointer) cannot use. A selector loop works everywhere.
"""
import asyncio

import uvicorn

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config("backend.main:app", host="0.0.0.0", port=8000))
    asyncio.run(server.serve())
