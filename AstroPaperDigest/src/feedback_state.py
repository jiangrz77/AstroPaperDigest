"""Persist per-digest live score adjustments from user feedback."""

import json
import os

from src import paths as _paths
from src.scoring import TIER_NAMES, apply_source_adjustment, score_to_stars, tier_key

_ADJUSTMENTS_FILE = os.path.join(str(_paths.data_dir()), "feedback_adjustments.json")


def load_adjustments(path: str = _ADJUSTMENTS_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, IOError):
        return {}


def save_adjustments(data: dict, path: str = _ADJUSTMENTS_FILE) -> None:
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)


def record_adjustment(date_str: str, paper_id: str, amount: int,
                      path: str = _ADJUSTMENTS_FILE) -> int:
    data = load_adjustments(path)
    dates = data.setdefault(date_str, {})
    current = int(dates.get(paper_id, 0))
    step = int(amount)
    if step == 0:
        return current
    current += step
    # The effective score is clamped later against the paper's base score;
    # retaining the signed count means future feedback remains cumulative.
    dates[paper_id] = current
    save_adjustments(data, path)
    return current


def get_adjustment(date_str: str, paper_id: str,
                   path: str = _ADJUSTMENTS_FILE) -> int:
    try:
        return int(load_adjustments(path).get(date_str, {}).get(paper_id, 0))
    except (TypeError, ValueError):
        return 0


def clear_date(date_str: str, path: str = _ADJUSTMENTS_FILE) -> None:
    data = load_adjustments(path)
    if date_str in data:
        data.pop(date_str, None)
        save_adjustments(data, path)


def apply_to_digest(digest: dict, date_str: str,
                   path: str = _ADJUSTMENTS_FILE) -> dict:
    """Apply saved adjustments and rebuild the four star-rating tiers in place."""
    adjustments = load_adjustments(path).get(date_str, {})
    papers = []
    for tier in digest.get("tiers", []):
        for paper in tier.get("papers", []):
            displayed_score = int(paper.get("score", 0) or 0)
            scale = int(paper.get("score_scale", 10 if displayed_score > 5 else 5) or 5)
            source_score = int(paper.get("score_source", displayed_score) or 0)
            base_score = 0 if paper.get("scoring_failed") else score_to_stars(source_score, scale)
            paper["base_score"] = base_score
            paper["score_scale"] = scale
            paper["score_source"] = source_score
            delta = int(adjustments.get(paper.get("paper_id", ""), 0) or 0)
            paper["score"] = 0 if paper.get("scoring_failed") else apply_source_adjustment(source_score, delta, scale)
            papers.append(paper)

    papers.sort(key=lambda item: item.get("score", 0), reverse=True)
    grouped = {key: [] for key in ("strong", "high", "medium", "low")}
    for paper in papers:
        key = "low" if paper.get("scoring_failed") else tier_key(paper.get("score", 1))
        grouped[key].append(paper)
    digest["tiers"] = [
        {"name": TIER_NAMES[key], "papers": grouped[key]}
        for key in ("strong", "high", "medium", "low")
    ]
    digest["highly_relevant_count"] = len(grouped["strong"]) + len(grouped["high"])
    return digest
