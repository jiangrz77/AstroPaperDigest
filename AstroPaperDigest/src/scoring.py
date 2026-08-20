"""Shared helpers for the native 1–5 star relevance scale."""

import math


MIN_STARS = 1
MAX_STARS = 5


def clamp_stars(score) -> int:
    """Clamp a scored paper to the supported 1–5 star range."""
    return max(MIN_STARS, min(MAX_STARS, int(round(float(score)))))


def legacy_score_to_stars(score) -> int:
    """Convert a legacy 0–10 score while preserving the former tiers."""
    value = max(0, min(10, int(round(float(score)))))
    return max(MIN_STARS, min(MAX_STARS, int(math.ceil(value / 2.0))))


def normalize_threshold(threshold) -> int:
    """Accept native star thresholds and legacy 0–10 configuration values."""
    value = int(threshold)
    return legacy_score_to_stars(value) if value > MAX_STARS else clamp_stars(value)


def score_to_stars(score, scale: int = 5) -> int:
    """Normalize a score stored on either the /5 or legacy /10 scale."""
    return legacy_score_to_stars(score) if int(scale) == 10 else clamp_stars(score)


def apply_source_adjustment(source_score, delta: int, scale: int = 5) -> int:
    """Apply a live adjustment in the source score's units, then return stars."""
    scale = 10 if int(scale) == 10 else 5
    minimum = 0 if scale == 10 else MIN_STARS
    adjusted = max(minimum, min(scale, int(source_score) + int(delta)))
    return score_to_stars(adjusted, scale)


def feedback_step(scale: int = 5) -> int:
    """One visible star equals two legacy points or one native star."""
    return 2 if int(scale) == 10 else 1


def tier_key(score: int) -> str:
    """Return the four-tier key for a normalized star rating."""
    score = clamp_stars(score)
    if score == 5:
        return "strong"
    if score == 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


TIER_NAMES = {
    "strong": "Strongly Recommended",
    "high": "Highly Relevant",
    "medium": "Possibly Relevant",
    "low": "Marginal",
}
