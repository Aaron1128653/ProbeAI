"""The answer shapes the model must return (D8 "LLM contracts"). Each one is a Pydantic model that
goes to `client.messages.parse(output_format=...)`, so the API forces the answer into this shape.

Limits such as "3-5 missions" are not in the schema: the structured-output API cannot enforce
them, so the run loop checks them in code after parsing.
"""
from typing import Literal

from pydantic import BaseModel


class Mission(BaseModel):
    id: str                 # m1, m2, ...
    goal: str               # a user goal in plain words, never a selector or a script
    category: Literal["core_flow", "input_validation", "edge_case", "feedback", "state_change"]
    priority: Literal["critical", "high", "medium", "low"]
    why: str                # one sentence: what a real user loses if this is broken


class AppPlan(BaseModel):
    app_type: str
    capabilities: list[str]     # 3-6 things a user would want to do
    missions: list[Mission]     # 3-5


class StepDecision(BaseModel):
    action: Literal["click", "type", "check", "uncheck", "press", "done", "stuck"]
    ref: str | None             # a ref from the page state; null for done / stuck
    text: str | None            # what to type, or the key to press
    expect: str                 # what should visibly change if the app works correctly
    reasoning: str              # one sentence


class StepVerdict(BaseModel):
    step: int
    violated: bool              # would a real user consider the outcome wrong?
    reason: str


class JudgedFinding(BaseModel):
    step: int | None            # the step where it is visible
    kind: Literal["bug", "improvement"]
    title: str
    severity: Literal["low", "medium", "high"]
    impact: str
    expected: str
    observed: str
    suggestion: str


class Judgement(BaseModel):
    step_verdicts: list[StepVerdict]
    findings: list[JudgedFinding]


class Disproof(BaseModel):
    refuted: bool               # true: a harmless explanation fits the evidence better than a defect
    reason: str
