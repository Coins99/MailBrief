"""Structured OpenAI analysis adapter."""

from datetime import UTC, datetime
from enum import StrEnum
import json
from typing import Self
import hashlib
import secrets

from pydantic import BaseModel, Field

from mailbrief.domain.analysis import AnalysisCategory, AnalysisRequest, MessageAnalysis


PROMPT_VERSION = "2026-09-12"
SCHEMA_VERSION = "v1"
RESOLVER_VERSION = "v1"


class LLMAnalysisResponse(BaseModel):
    """Wire DTO for OpenAI strict mode structured output."""
    message_key: str
    category: AnalysisCategory
    summary: str
    action_required: bool
    action_text: str | None = None
    deadline_text: str | None = None
    deadline_precision: str | None = None
    confidence: float
    evidence: str

