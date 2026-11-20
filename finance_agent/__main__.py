"""python -m finance_agent  (the Docker image's entry point)"""

import uvicorn

from finance_agent.config import settings

if __name__ == "__main__":
    uvicorn.run("finance_agent.main:app", host=settings.host, port=settings.port)
