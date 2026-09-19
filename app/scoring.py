"""Deterministic lead scoring (FR-6.5). Dependency-free so it is unit-testable in isolation."""
from __future__ import annotations

RUBRIC = [
    ("email", 20, "email captured"),
    ("__booked__", 25, "meeting booked"),
    ("budget_hint", 15, "budget or project size mentioned"),
    ("timeline", 10, "timeline mentioned"),
    ("project_type", 15, "named a concrete project or problem"),
    ("__questions__", 10, "asked 3 or more substantive questions"),
    ("company", 5, "company name given"),
]


def score_lead(qualification: dict, booked: bool, questions: list) -> tuple[int, str]:
    score = 0
    for field, points, _ in RUBRIC:
        if field == "__booked__":
            score += points if booked else 0
        elif field == "__questions__":
            score += points if len(questions) >= 3 else 0
        elif qualification.get(field):
            score += points
    score = min(score, 100)
    tier = "hot" if score >= 70 else "warm" if score >= 40 else "cold"
    return score, tier
