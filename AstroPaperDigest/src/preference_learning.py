"""Learn user preferences from taste feedback (overrated / underrated).

This module turns the feedback.json history into a structured "learned profile"
that the ranking stage can apply deterministically:

  * keyword/topic weights      (multiplicative, clamped, time-decayed)
  * arXiv category weights     (same math)
  * global score calibration   (overall upward/downward bias)

The profile is consumed by ranker.py to (a) inject explicit numeric weights
into the LLM prompt and (b) apply a bounded deterministic score adjustment.
"""

import json
import math
import os
import re
from datetime import datetime, timezone

from src import paths as _paths
_PROJECT_DIR = _paths.data_dir()

FEEDBACK_FILE = os.path.join(str(_PROJECT_DIR), "feedback.json")
LEARNED_PROFILE_FILE = os.path.join(_PROJECT_DIR, "learned_profile.json")

# --- Tuning constants -------------------------------------------------------
WEIGHT_MIN = 0.5
WEIGHT_MAX = 2.0
BOOST = 1.25           # factor applied for an "underrated" hit
PENALTY = 0.8          # factor applied for an "overrated" hit
HALF_LIFE_DAYS = 60.0  # feedback influence decays back to 1.0 over this half-life
CALIBRATION_PER = 0.15   # global offset per (underrated - overrated) count
CALIBRATION_MAX = 0.5
ADJUSTMENT_MAX = 2.0     # deterministic score adjustment clamp (points)
KEYWORD_ADJUST_COEF = 0.5   # score points per (weight - 1.0) per matched keyword
CATEGORY_ADJUST_COEF = 1.0  # score points per (weight - 1.0) per matched category
MIN_FEEDBACK_FOR_TERM = 2   # a discovered term must appear in N distinct papers
MAX_LEARNED_TERMS = 100

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "are", "was", "were", "have", "has", "had", "not", "but", "its", "their",
    "into", "over", "than", "then", "them", "they", "will", "would", "can",
    "could", "should", "may", "might", "about", "after", "before", "between",
    "through", "during", "using", "based", "which", "while", "where", "when",
    "paper", "papers", "study", "studies", "result", "results", "method",
    "methods", "model", "models", "data", "analysis", "show", "shows", "found",
    "find", "present", "report", "new", "two", "one", "also", "well", "use",
    "used", "via", "per", "within", "without", "under", "above", "below",
    "however", "thus", "therefore", "herein", "et", "al",
}


# --- Persistence ------------------------------------------------------------

def load_feedback(path: str = FEEDBACK_FILE) -> list:
    """Read the feedback history (list of dicts)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_feedback(feedback: list, path: str = FEEDBACK_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feedback, f, indent=2, ensure_ascii=False)


def load_learned_profile(path: str = LEARNED_PROFILE_FILE):
    """Return the persisted learned profile, or None if missing/corrupt."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def save_learned_profile(profile: dict, path: str = LEARNED_PROFILE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)


def reset_learned_profile() -> None:
    """Clear feedback history and the learned profile (full reset)."""
    save_feedback([])
    if os.path.exists(LEARNED_PROFILE_FILE):
        os.remove(LEARNED_PROFILE_FILE)


# --- Text helpers -----------------------------------------------------------

def matches_term(text: str, term: str) -> bool:
    """Case-insensitive whole-token/subphrase match."""
    term = str(term).strip().lower()
    if not term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return re.search(pattern, str(text).lower()) is not None


def extract_keyword_matches(text: str, keywords) -> list:
    """Return the subset of keywords present in text (normalized)."""
    return [str(k).strip().lower() for k in keywords
            if str(k).strip() and matches_term(text, str(k))]


def extract_ngrams(text: str, stopwords=None) -> list:
    """Extract lowercase unigrams and bigrams from text."""
    stop = stopwords if stopwords is not None else _STOPWORDS
    toks = [t for t in _TOKEN_RE.findall(str(text).lower()) if t not in stop]
    terms = list(toks)
    terms.extend(toks[i] + " " + toks[i + 1] for i in range(len(toks) - 1))
    return terms


def _discover_terms(feedback: list, stopwords=None) -> list:
    """Terms appearing in at least MIN_FEEDBACK_FOR_TERM distinct papers."""
    term_papers = {}
    for fb in feedback:
        pid = str(fb.get("paper_id", ""))
        if not pid:
            continue
        text = " ".join([str(fb.get("title", "")), str(fb.get("abstract_snippet", ""))])
        seen = set()
        for term in extract_ngrams(text, stopwords):
            if term not in seen:
                seen.add(term)
                term_papers.setdefault(term, set()).add(pid)
    return [t for t, pids in term_papers.items()
            if len(pids) >= MIN_FEEDBACK_FOR_TERM]


# --- Timestamp / decay helpers ----------------------------------------------

def _to_dt(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _days_since(dt: datetime, now: datetime) -> float:
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _decayed_factor(factor: float, age_days: float) -> float:
    return 1.0 + (factor - 1.0) * math.exp(-age_days / HALF_LIFE_DAYS)


def _clamp_weight(w: float) -> float:
    return max(WEIGHT_MIN, min(WEIGHT_MAX, float(w)))


# --- Derivation -------------------------------------------------------------

def derive_learned_profile(feedback: list, config_keywords=None, manual=None,
                           now=None) -> dict:
    """Derive a learned profile from the feedback history.

    config_keywords seeds the topic vocabulary (stable, interpretable).
    manual carries user overrides: keyword_weights maps term to float or
    None, category_weights maps category to float or None. A None value
    suppresses the term.
    """
    now = now or datetime.now(timezone.utc).astimezone()
    feedback = list(feedback or [])
    config_keywords = [str(k).strip().lower()
                       for k in (config_keywords or []) if str(k).strip()]

    # Vocabulary: config keywords + terms discovered from feedback.
    vocab = []
    seen = set()
    for k in config_keywords:
        if k not in seen:
            seen.add(k)
            vocab.append(k)
    for t in _discover_terms(feedback):
        if t not in seen and len(seen) < MAX_LEARNED_TERMS + len(config_keywords):
            seen.add(t)
            vocab.append(t)

    entries = sorted(feedback, key=lambda fb: _to_dt(fb.get("timestamp") or fb.get("date")))
    kw_weights = {}
    cat_weights = {}
    kw_sources = {}
    cat_sources = {}
    kw_last_action = {}
    cat_last_action = {}

    for fb in entries:
        action = fb.get("action")
        if action not in ("underrated", "overrated"):
            continue
        factor = BOOST if action == "underrated" else PENALTY
        age = _days_since(_to_dt(fb.get("timestamp") or fb.get("date")), now)
        eff = _decayed_factor(factor, age)
        source = f"{action}:{fb.get('paper_id', '')}"

        text = " ".join([str(fb.get("title", "")), str(fb.get("abstract_snippet", ""))])
        for term in extract_keyword_matches(text, vocab):
            if kw_last_action.get(term) is not None and kw_last_action[term] != action:
                kw_weights[term] = 1.0  # conflict: latest feedback wins
            kw_weights[term] = _clamp_weight(kw_weights.get(term, 1.0) * eff)
            kw_last_action[term] = action
            kw_sources[term] = source

        cats = fb.get("categories") or []
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",") if c.strip()]
        for c in cats:
            c = str(c).strip()
            if not c:
                continue
            if cat_last_action.get(c) is not None and cat_last_action[c] != action:
                cat_weights[c] = 1.0
            cat_weights[c] = _clamp_weight(cat_weights.get(c, 1.0) * eff)
            cat_last_action[c] = action
            cat_sources[c] = source

    cal = CALIBRATION_PER * (sum(1 for fb in feedback if fb.get("action") == "underrated")
                             - sum(1 for fb in feedback if fb.get("action") == "overrated"))
    cal = max(-CALIBRATION_MAX, min(CALIBRATION_MAX, cal))

    # Apply manual overrides (user priority). None suppresses an auto term.
    manual = manual or {}
    for term, w in (manual.get("keyword_weights", {}) or {}).items():
        term = str(term).strip().lower()
        if not term:
            continue
        if w is None:
            kw_weights.pop(term, None)
            kw_sources.pop(term, None)
            continue
        try:
            kw_weights[term] = _clamp_weight(float(w))
        except (TypeError, ValueError):
            continue
        kw_sources[term] = "manual"
    for cat, w in (manual.get("category_weights", {}) or {}).items():
        cat = str(cat).strip()
        if not cat:
            continue
        if w is None:
            cat_weights.pop(cat, None)
            cat_sources.pop(cat, None)
            continue
        try:
            cat_weights[cat] = _clamp_weight(float(w))
        except (TypeError, ValueError):
            continue
        cat_sources[cat] = "manual"

    return {
        "keyword_weights": {
            t: {"weight": round(w, 3), "source": kw_sources.get(t, ""),
                "origin": "manual" if kw_sources.get(t) == "manual" else "auto"}
            for t, w in kw_weights.items()
        },
        "category_weights": {
            c: {"weight": round(w, 3), "source": cat_sources.get(c, ""),
                "origin": "manual" if cat_sources.get(c) == "manual" else "auto"}
            for c, w in cat_weights.items()
        },
        "global_calibration": round(cal, 3),
        "manual": manual,
        "updated_at": now.isoformat(timespec="seconds"),
    }


def rebuild_learned_profile(config_keywords=None, manual=None) -> dict:
    """Rebuild + persist the learned profile from feedback.json."""
    profile = derive_learned_profile(load_feedback(), config_keywords=config_keywords,
                                     manual=manual)
    save_learned_profile(profile)
    return profile


def ensure_learned_profile(config_keywords=None) -> dict:
    """Return the persisted profile, deriving a fresh one if missing."""
    profile = load_learned_profile()
    if profile is None:
        profile = rebuild_learned_profile(config_keywords=config_keywords)
    return profile


# --- Prompt formatting ------------------------------------------------------

def format_learned_weights_block(profile: dict) -> str:
    """Render the learned profile as explicit instructions for the LLM prompt."""
    if not profile:
        return ""
    lines = ["=== Learned Preference Weights (apply these explicitly when scoring) ==="]
    kw = profile.get("keyword_weights", {}) or {}
    boosts = sorted(((t, m.get("weight", 1.0)) for t, m in kw.items()
                     if m.get("weight", 1.0) > 1.0), key=lambda x: -x[1])
    penal = sorted(((t, m.get("weight", 1.0)) for t, m in kw.items()
                    if m.get("weight", 1.0) < 1.0), key=lambda x: x[1])
    if boosts:
        lines.append("Boost topics (these increase relevance):")
        for t, w in boosts[:20]:
            lines.append(f'  - "{t}" x{round(w, 2)}')
    if penal:
        lines.append("Penalize topics (these decrease relevance):")
        for t, w in penal[:20]:
            lines.append(f'  - "{t}" x{round(w, 2)}')
    cw = profile.get("category_weights", {}) or {}
    if cw:
        shown = [f"  - {c} x{round(m.get('weight', 1.0), 2)}"
                 for c, m in sorted(cw.items()) if m.get("weight", 1.0) != 1.0]
        if shown:
            lines.append("Category weights:")
            lines.extend(shown)
    cal = profile.get("global_calibration", 0.0) or 0.0
    if cal:
        sign = "+" if cal > 0 else ""
        lines.append(f"Global calibration: {sign}{round(cal, 2)} "
                     "(shift every score by this amount)")
    return "\n".join(lines)


# --- Deterministic scoring --------------------------------------------------

def compute_adjustment(paper: dict, profile: dict) -> float:
    """Deterministic score adjustment for one paper, clamped to ±ADJUSTMENT_MAX."""
    if not profile:
        return 0.0
    adj = 0.0
    text = " ".join([str(paper.get("title", "")), str(paper.get("abstract", ""))])
    for term, meta in (profile.get("keyword_weights", {}) or {}).items():
        w = meta.get("weight", 1.0) if isinstance(meta, dict) else 1.0
        if matches_term(text, term):
            adj += (w - 1.0) * KEYWORD_ADJUST_COEF

    cats = paper.get("categories") or []
    if isinstance(cats, str):
        cats = [c.strip() for c in cats.split(",") if c.strip()]
    cw = profile.get("category_weights", {}) or {}
    for c in cats:
        meta = cw.get(str(c).strip())
        if isinstance(meta, dict):
            adj += (meta.get("weight", 1.0) - 1.0) * CATEGORY_ADJUST_COEF

    adj += profile.get("global_calibration", 0.0) or 0.0
    return max(-ADJUSTMENT_MAX, min(ADJUSTMENT_MAX, adj))


def apply_adjustment(raw_score, adjustment: float) -> int:
    """Combine an LLM raw score with an adjustment and clamp to 0-10."""
    return max(0, min(10, int(round(float(raw_score) + adjustment))))