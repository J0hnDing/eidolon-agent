from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.db import create_db_and_tables
from app.routers import memory_facts, skills


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    yield


app = FastAPI(
    title="Personal Agent Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(memory_facts.router)
app.include_router(skills.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
