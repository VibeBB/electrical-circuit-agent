"""Advisory Konnect observations kept separate from deterministic gates."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AdvisoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    stage: Literal["intake", "schematic", "layout", "review", "manufacturing"]
    status: Literal["ok", "error", "not_applicable"]
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    detail: dict[str, object] | None = None


def merge_detail(result: AdvisoryResult, extra: dict[str, object]) -> AdvisoryResult:
    result.detail = {**(result.detail or {}), **extra}
    return result
