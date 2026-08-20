"""LLM-based relevance scoring for arxiv papers using DeepSeek (OpenAI-compatible API)."""

import json
import math
import os
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from openai import OpenAI

from .profile import profile_to_prompt_text
from .preference_learning import (
    apply_adjustment,
    compute_adjustment,
    ensure_learned_profile,
    format_learned_weights_block,
)
from .progress import emit, log

from src import paths as _paths
FEEDBACK_FILE = str(_paths.data_dir() / "feedback.json")

# --- Ranking tunables -------------------------------------------------------
TARGET_BATCH_SIZE = 50   # papers per batch (balanced distribution, ~50 each)
VOTES = 3                # independent LLM calls per batch; per-paper median wins
MIN_VOTES = 2            # below this many valid votes a paper is marked No score
MAX_WORKERS = 5          # cap on parallel LLM calls (adaptive; 429 backoff keeps it polite)
RETRY_ATTEMPTS = 1       # extra attempt after the first failure (one retry)
RETRY_BACKOFF = 3.0      # seconds before a normal retry
RETRY_BACKOFF_429 = 30.0 # seconds before a retry after HTTP 429 (rate limited)
MAX_ATTEMPTS_429 = 3     # total tries for rate-limited calls (initial + 2 retries)


def split_papers_into_batches(papers: list, target: int = TARGET_BATCH_SIZE) -> list:
    """Split papers into a small number of balanced batches (~target each).

    The number of batches is the integer nearest to n/target, so every batch
    stays close to the target size and there is never a tiny trailing batch
    that would waste the per-request fixed prompt overhead.
    """
    n = len(papers)
    if n <= target:
        return [list(papers)]
    n_batches = max(1, round(n / target))
    per = math.ceil(n / n_batches)
    return [papers[i * per:(i + 1) * per] for i in range(n_batches)]


def get_client(base_url: str, api_key: str) -> OpenAI:
    """Create an OpenAI-compatible client."""
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=60.0,
        max_retries=1,
    )


def _load_feedback_text() -> str:
    """Load user feedback and format it for the LLM prompt."""
    if not os.path.exists(FEEDBACK_FILE):
        return ""
    
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            feedback = json.load(f)
    except (json.JSONDecodeError, IOError):
        return ""
    
    if not feedback:
        return ""
    
    overrated = [fb for fb in feedback if fb.get("action") == "overrated"]
    underrated = [fb for fb in feedback if fb.get("action") == "underrated"]
    
    lines = ["\n=== User Feedback (adjust scoring accordingly) ==="]
    
    if overrated:
        lines.append("Papers the user found OVERRATED (score was too high):")
        for fb in overrated[-5:]:  # Last 5
            lines.append(f'  - "{fb["title"][:80]}" was scored {fb.get("original_score", "?")} but user found it less relevant')
    
    if underrated:
        lines.append("Papers the user found UNDERRATED (score was too low):")
        for fb in underrated[-5:]:  # Last 5
            lines.append(f'  - "{fb["title"][:80]}" was scored {fb.get("original_score", "?")} but user found it highly relevant')
    
    if len(lines) > 1:
        return "\n".join(lines)
    return ""


def build_ranking_prompt(profile_text: str, papers: list[dict], learned_profile: dict = None) -> str:
    """Build the prompt for LLM ranking."""
    paper_list = []
    for i, p in enumerate(papers):
        authors_str = ", ".join(p["authors"][:5])
        if len(p["authors"]) > 5:
            authors_str += " et al."
        paper_list.append(
            f"[{i}] {p['title']}\n"
            f"    Authors: {authors_str}\n"
            f"    Categories: {', '.join(p['categories'])}\n"
            f"    Abstract: {p['abstract'][:500]}"
        )
    
    papers_text = "\n\n".join(paper_list)
    
    # Load user feedback for calibration
    feedback_text = _load_feedback_text()
    learned_text = format_learned_weights_block(learned_profile)
    
    prompt = f"""{profile_text}
{feedback_text}
{learned_text}

=== Candidate Papers ===
{papers_text}

=== Task ===
You are an astrophysics research assistant. For each candidate paper above, rate its relevance to the researcher's interests on a scale of 1-5 stars, where:
- 5 stars: Strongly recommended; directly matches core research interests (chemical enrichment, metal-poor stars, globular clusters, supernovae, Population III stars, Milky Way formation, stellar abundances)
- 4 stars: Highly relevant; closely related to the main interests
- 3 stars: Possibly relevant; meaningfully related or a useful adjacent topic
- 2 stars: Possibly relevant; weakly or tangentially related
- 1 star: Marginal; not meaningfully relevant

For each paper, provide:
1. The paper index [i]
2. A relevance rating (integer 1-5)
3. A brief reason of no more than 12 words explaining the score

Respond ONLY with a valid JSON array in this exact format, no other text:
[
  {{"index": 0, "score": 5, "reason": "Directly studies chemical enrichment in metal-poor globular clusters"}},
  {{"index": 1, "score": 2, "reason": "Tangentially related through galaxy formation topic"}},
  ...
]
"""
    return prompt


def _parse_score_response(content: str) -> tuple[list[dict], bool]:
    """Parse a score array, recovering complete objects from a truncated reply.

    Some providers return a valid prefix of the requested JSON array before
    hitting an output limit or producing an unterminated final reason string.
    Those complete scores are still useful votes; retrying the whole batch
    would discard them and can reproduce the same failure.

    Returns (normalized_items, recovered_partial_response).
    """
    text = (content or "").strip()
    if "```" in text:
        blocks = text.split("```")
        if len(blocks) >= 2:
            text = blocks[1].strip()
            if text.lower().startswith("json"):
                text = text[4:].lstrip()

    partial = False
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON array")
    except (json.JSONDecodeError, ValueError):
        array_start = text.find("[")
        if array_start < 0:
            raise ValueError("Expected JSON array")
        decoder = json.JSONDecoder()
        cursor = array_start + 1
        parsed = []
        while cursor < len(text):
            while cursor < len(text) and text[cursor] in " \t\r\n,":
                cursor += 1
            if cursor >= len(text) or text[cursor] == "]":
                break
            if text[cursor] != "{":
                break
            try:
                item, end = decoder.raw_decode(text, cursor)
            except json.JSONDecodeError:
                break
            parsed.append(item)
            cursor = end
        if not parsed:
            raise ValueError("No complete score objects in LLM response")
        partial = True

    items = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            score = max(1, min(5, int(item.get("score", 3))))
            items.append({
                "index": int(item.get("index", -1)),
                "score": score,
                "reason": str(item.get("reason", "")),
            })
        except (TypeError, ValueError):
            continue
    if not items:
        raise ValueError("No valid score objects in LLM response")
    return items, partial


def rank_papers(
    papers: list[dict],
    profile: dict,
    llm_config: dict,
) -> list[dict]:
    """Use LLM to rank papers by relevance to the user's interests.

    Each batch of papers is scored by VOTES independent LLM calls with the
    same prompt; the per-paper median wins (median voting over VOTES calls
    reduces score noise). A paper with fewer than MIN_VOTES valid votes is marked with
    scoring_failed and score 0 so the UI can show "No score" instead of a
    fabricated number. Progress events report completed batches.

    Args:
        papers: list of candidate paper dicts
        profile: interest profile dict from profile.build_profile()
        llm_config: dict with base_url, api_key_env, model

    Returns:
        list of paper dicts with added 'score', 'score_raw', 'score_votes',
        'score_adjustment' and 'reason' fields, sorted by score descending
    """
    if not papers:
        return []

    # Get API key from environment
    api_key_env = llm_config.get("api_key_env", "DEEPSEEK_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(
            f"API key not found. Set the {api_key_env} environment variable."
        )

    base_url = llm_config.get("base_url", "https://api.deepseek.com")
    model = llm_config.get("model", "deepseek-v4-flash")

    learned_profile = ensure_learned_profile(
        config_keywords=list((profile.get("keywords") or {}).keys())
    )

    client = get_client(base_url, api_key)
    profile_text = profile_to_prompt_text(profile)

    batches = split_papers_into_batches(papers, TARGET_BATCH_SIZE)
    total_batches = len(batches)
    batch_starts = []
    paper_batch_of = [0] * len(papers)
    _offset = 0
    for _bi, _b in enumerate(batches):
        batch_starts.append(_offset)
        for _k in range(len(_b)):
            paper_batch_of[_offset + _k] = _bi
        _offset += len(_b)
    emit("rank", 0, total_batches,
         f"Starting… ({total_batches} batch{'es' if total_batches != 1 else ''}, "
         f"~{TARGET_BATCH_SIZE} papers each, {VOTES} votes)")

    # votes per paper: {paper_idx: {"scores": [...], "reasons": [...]}}
    results = {i: {"scores": [], "reasons": []} for i in range(len(papers))}
    results_lock = Lock()
    batch_votes_done = [0] * total_batches

    def _call_batch(batch_idx: int, prompt: str):
        """One LLM call (one vote) with retries; returns (items, last_error)."""
        messages = [
            {"role": "system", "content": "You are an astrophysics research assistant. Respond only with valid JSON."},
            {"role": "user", "content": prompt},
        ]
        last_error = ""
        for attempt in range(max(RETRY_ATTEMPTS + 1, MAX_ATTEMPTS_429)):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=8192,
                )
                content = response.choices[0].message.content
                if content is None:
                    raise ValueError("Empty LLM response")
                content = content.strip()
                items, recovered_partial = _parse_score_response(content)
                if recovered_partial:
                    log(
                        f"  Recovered {len(items)} complete scores from a partial "
                        f"LLM response (batch {batch_idx + 1})"
                    )
                return items, ""
            except Exception as e:
                last_error = str(e) or e.__class__.__name__
                is_429 = getattr(e, "status_code", None) == 429
                max_attempts = MAX_ATTEMPTS_429 if is_429 else RETRY_ATTEMPTS + 1
                if attempt < max_attempts - 1:
                    backoff = RETRY_BACKOFF_429 if is_429 else RETRY_BACKOFF
                    log(f"  LLM call failed (batch {batch_idx + 1}); retrying in {backoff:.0f}s ({last_error})")
                    time.sleep(backoff)
                else:
                    log(f"  LLM call failed permanently (batch {batch_idx + 1}): {last_error}")
                    break
        return [], last_error

    batch_errors = [""] * total_batches

    def _vote(batch_idx: int, vote_idx: int):
        start = batch_starts[batch_idx]
        batch = batches[batch_idx]
        end = start + len(batch)
        log(f"  Ranking batch {batch_idx + 1}/{total_batches} (vote {vote_idx + 1}/{VOTES})...")
        prompt = build_ranking_prompt(profile_text, batch, learned_profile=learned_profile)
        items, err = _call_batch(batch_idx, prompt)
        with results_lock:
            if err and not batch_errors[batch_idx]:
                batch_errors[batch_idx] = err
            for item in items:
                idx = item["index"] + start
                if start <= idx < end:
                    results[idx]["scores"].append(item["score"])
                    if item["reason"]:
                        results[idx]["reasons"].append(item["reason"])

    workers = max(1, min(MAX_WORKERS, total_batches * VOTES))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_vote, b, v): (b, v)
            for b in range(total_batches)
            for v in range(VOTES)
        }
        for fut in as_completed(futures):
            b, _ = futures[fut]
            try:
                fut.result()  # surface unexpected worker errors
            except Exception:
                pass  # _vote degrades gracefully on its own
            batch_votes_done[b] += 1
            if batch_votes_done[b] == VOTES:
                done = sum(1 for n in batch_votes_done if n == VOTES)
                emit("rank", done, total_batches,
                     f"{done}/{total_batches} batches scored")

    emit("rank", total_batches, total_batches, "All batches scored")

    # Attach scores to papers
    scored_papers = []
    for idx, paper in enumerate(papers):
        p = paper.copy()
        votes = results[idx]["scores"]
        p["score_votes"] = len(votes)
        p["score_raw"] = votes
        if len(votes) >= MIN_VOTES:
            raw_score = int(round(statistics.median(votes)))
            adjustment = compute_adjustment(p, learned_profile)
            p["score_adjustment"] = round(adjustment, 2)
            p["score"] = apply_adjustment(raw_score, adjustment)
            reasons = results[idx]["reasons"]
            p["reason"] = Counter(reasons).most_common(1)[0][0] if reasons else "No reason provided"
        else:
            err = batch_errors[paper_batch_of[idx]] if total_batches else ""
            p["scoring_failed"] = True
            p["score_adjustment"] = 0.0
            p["score"] = 0
            p["reason"] = f"Scoring failed — no score assigned ({err or 'fewer than 2 valid votes'})"
        scored_papers.append(p)

    # Sort by score descending
    scored_papers.sort(key=lambda x: x["score"], reverse=True)

    return scored_papers
