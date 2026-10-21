from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .router import Providers, RouterError
from .limits import LimitManager


class RouteRequest(BaseModel):
    input: Any
    system_instruction: Any | None = None
    previous_interaction_id: str | None = None
    tools: list[dict[str, Any]] | None = None
    generation_config: dict[str, Any] = Field(default_factory=dict)
    hints: dict[str, Any] = Field(default_factory=dict)


class ResetLimitsRequest(BaseModel):
    model: str | None = None


providers = Providers()
limits = LimitManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await providers.close()


app = FastAPI(title="smart_AI Gemini Router", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    try:
        return {"models": await providers.models()}
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/limits")
async def get_limits() -> dict[str, Any]:
    try:
        return limits.status()
    except RouterError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/limits/reset")
async def reset_limits(payload: ResetLimitsRequest | None = None) -> dict[str, Any]:
    model = payload.model if payload else None
    limits.reset(model)
    return {"reset": True, "model": model, "scope": "smart_AI local counters only; provider quotas are unchanged"}


@app.post("/v1/route")
async def route(payload: RouteRequest) -> dict[str, Any]:
    try:
        data = payload.model_dump(exclude={"hints"}, exclude_none=True)
        return await providers.run(data, payload.hints)
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
