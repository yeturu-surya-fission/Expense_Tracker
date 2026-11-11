import uvicorn

from mcp_server.config import settings

if __name__ == "__main__":
    uvicorn.run("mcp_server.main:app", host=settings.host, port=settings.port)
