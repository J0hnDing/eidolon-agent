from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import create_db_and_tables
from app.routers import chat, memory_facts, permission_requests, skill_generation_requests, skills


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    yield


app = FastAPI(
    title="Personal Agent Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(memory_facts.router)
app.include_router(skills.router)
app.include_router(chat.router)
app.include_router(skill_generation_requests.router)
app.include_router(permission_requests.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
