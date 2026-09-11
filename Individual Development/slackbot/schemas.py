from typing import Any, Literal

from pydantic import BaseModel, Field

Prediction = Literal["keep", "skip"]
RunMode = Literal["first_run", "partial_replay", "full_replay"]
AnswerSource = Literal["memory", "reused", "generated", "unknown"]


class JobCard(BaseModel):
    job_id: str
    title: str
    company: str
    location: str | None = None
    salary: str | None = None
    url: str | None = None
    why: str | None = Field(None, description="Rendered explanation; the digest's most important field")
    prediction: Prediction | None = None
    confidence: float | None = None
    coverage: str | None = Field(None, description="e.g. '4 of 5 requirements have verified claims'")
    off_slate: bool = Field(False, description="Drawn from outside the top ranking to hold slate difficulty fixed")


class DigestIn(BaseModel):
    run_id: str
    day: str
    jobs: list[JobCard]
    mode: RunMode | None = None
    tokens: int | None = None
    wall_ms: int | None = None
    questions_asked: int = 0
    decided_by_agent: bool = False
    channel: str | None = None


class ClaimUsed(BaseModel):
    claim_id: str
    text: str


class GapOut(BaseModel):
    skill: str
    note: str | None = None


class QuestionOut(BaseModel):
    question_id: str
    text: str
    draft: str | None = None
    source: AnswerSource = "unknown"


class PackIn(BaseModel):
    run_id: str
    job_id: str
    title: str
    company: str
    summary: str
    claims: list[ClaimUsed] = []
    gaps: list[GapOut] = []
    questions: list[QuestionOut] = []
    answers_from_memory: int = 0
    doc_url: str | None = None
    sheet_url: str | None = None
    play: str | None = Field(None, description="e.g. 'apply-pack v3 (full replay)'")
    tokens: int | None = None
    channel: str | None = None


class PreferenceIn(BaseModel):
    run_id: str
    preference_id: str
    rule_text: str
    rule: dict[str, Any] = {}
    evidence: list[str] = Field([], description="Job titles or ids that produced the hypothesis")
    confidence: float | None = None
    channel: str | None = None


class AutonomyIn(BaseModel):
    run_id: str
    domain: str
    headline: str
    agreed: int | None = None
    total: int | None = None
    accuracy: float | None = None
    channel: str | None = None


class UnverifiedClaim(BaseModel):
    claim_id: str
    text: str
    source_doc: str | None = None


class ClaimsIn(BaseModel):
    run_id: str
    claims: list[UnverifiedClaim]
    channel: str | None = None


class QuestionIn(BaseModel):
    run_id: str
    question_id: str
    text: str
    job_id: str | None = None
    context: str | None = None
    draft: str | None = None
    channel: str | None = None


class PostedOut(BaseModel):
    ok: bool
    channel: str
    ts: str
