"""Persist per-digest live score adjustments from user feedback."""

import json
import os

from src import paths as _paths

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
    current += 1 if amount > 0 else -1
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
    """Apply saved adjustments and rebuild the three score tiers in place."""
    adjustments = load_adjustments(path).get(date_str, {})
    papers = []
    for tier in digest.get("tiers", []):
        for paper in tier.get("papers", []):
            base_score = int(paper.get("score", 0) or 0)
            paper["base_score"] = base_score
            delta = int(adjustments.get(paper.get("paper_id", ""), 0) or 0)
            paper["score"] = max(0, min(10, base_score + delta))
            papers.append(paper)

    papers.sort(key=lambda item: item.get("score", 0), reverse=True)
    high = [p for p in papers if p.get("score", 0) >= 7]
    medium = [p for p in papers if 5 <= p.get("score", 0) < 7]
    low = [p for p in papers if p.get("score", 0) < 5]
    digest["tiers"] = [
        {"name": "Highly Relevant", "papers": high},
        {"name": "Possibly Relevant", "papers": medium},
        {"name": "Marginal", "papers": low},
    ]
    digest["highly_relevant_count"] = len(high)
    return digest
