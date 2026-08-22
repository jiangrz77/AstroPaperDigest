#!/usr/bin/env python3
"""Score-stability test for AstroPaperDigest ranking.

Runs the SAME prompt (same profile + same papers) through the LLM multiple
times and reports how stable the scores are:

  * exact-match rate   - how many papers got the identical score on every run
  * score spread       - per-paper min..max across runs
  * average std        - mean per-paper standard deviation
  * tier agreement     - % of papers staying in the same tier on every run
  * ranking correlation- average pairwise Spearman rho between runs

Usage (from the project root):
    python3 tests/test_score_stability.py --runs 10 --papers 10
    python3 tests/test_score_stability.py --runs 10 --papers 10 --temperature 0

Needs DEEPSEEK_API_KEY (loaded from .env). Cost is small: N runs x a few k
tokens (roughly 0.01-0.05 CNY for 10 runs of 10 papers).
"""

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import yaml

from src.digest_parser import get_latest_digest_path, parse_digest
from src.profile import build_profile_from_config, profile_to_prompt_text
from src.ranker import build_ranking_prompt, get_client
from src.preference_learning import ensure_learned_profile

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config() -> dict:
    with open(os.path.join(_PROJECT_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_sample_papers(count: int) -> list:
    """Sample real papers from the latest digest (title/authors/categories/abstract)."""
    path = get_latest_digest_path(os.path.join(_PROJECT_DIR, "output", "digests"))
    if not path:
        raise SystemExit("No digest found; cannot sample papers.")
    d = parse_digest(path)
    papers = []
    for tier in d.get("tiers", []):
        for p in tier.get("papers", []):
            papers.append({
                "title": p.get("title", ""),
                "authors": [a.strip() for a in (p.get("authors") or "").split(",") if a.strip()][:5],
                "categories": [c.strip() for c in (p.get("categories") or "").split(",") if c.strip()],
                "abstract": (p.get("abstract") or "")[:500],
            })
            if len(papers) >= count:
                return papers
    if len(papers) < count:
        raise SystemExit(f"Only {len(papers)} papers available in the latest digest.")
    return papers


def run_once(client, model, messages, temperature: float) -> list:
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=8192,
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array")
    scores = [None] * 1000
    for item in data:
        idx = int(item.get("index", -1))
        if 0 <= idx < len(scores):
            scores[idx] = int(item.get("score", 5))
    return scores


def spearman(a: list, b: list):
    """Spearman rho over indices where BOTH runs have a score; None if too few."""
    idxs = [i for i in range(len(a)) if a[i] is not None and b[i] is not None]
    if len(idxs) < 2:
        return None
    aa = [a[i] for i in idxs]
    bb = [b[i] for i in idxs]
    n = len(aa)

    def ranks(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0] * n
        for pos, idx in enumerate(order):
            r[idx] = pos
        return r

    ra, rb = ranks(aa), ranks(bb)
    ma = mb = (n - 1) / 2.0
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    den = (sum((x - ma) ** 2 for x in ra) * sum((x - mb) ** 2 for x in rb)) ** 0.5
    return num / den if den else 0.0


def tier(score: int) -> str:
    if score == 5:
        return "strong"
    if score == 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def main():
    parser = argparse.ArgumentParser(description="Score stability test")
    parser.add_argument("--runs", type=int, default=10, help="LLM calls per paper set")
    parser.add_argument("--papers", type=int, default=10, help="papers per run")
    parser.add_argument("--temperature", type=float, default=0.1, help="sampling temperature")
    parser.add_argument("--votes", type=int, default=1, help="median of N calls per run (voting); 1 = single call")
    args = parser.parse_args()

    config = load_config()
    papers = load_sample_papers(args.papers)
    print(f"Papers: {len(papers)} | Runs: {args.runs} | Temperature: {args.temperature}")

    profile = build_profile_from_config(config)
    learned = ensure_learned_profile(config_keywords=list(profile.get("keywords", {}).keys()))
    prompt = build_ranking_prompt(
        profile_to_prompt_text(profile),
        papers,
        learned_profile=learned,
    )

    llm_cfg = config.get("llm", {})
    api_key = os.environ.get(llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY not found. Check .env")
    client = get_client(
        llm_cfg.get("base_url", "https://api.deepseek.com"),
        api_key,
    )
    model = llm_cfg.get("model", "deepseek-v4-flash-vision-exp")
    messages = [
        {"role": "system", "content": "You are an astrophysics research assistant. Respond only with valid JSON."},
        {"role": "user", "content": prompt},
    ]

    n = len(papers)
    runs = []
    for i in range(1, args.runs + 1):
        print(f"  run {i}/{args.runs} (median of {args.votes} call(s)) ...", flush=True)
        if args.votes <= 1:
            result = run_once(client, model, messages, args.temperature)
            runs.append([result[j] if j < len(result) else None for j in range(n)])
        else:
            per_paper = []
            for _ in range(args.votes):
                result = run_once(client, model, messages, args.temperature)
                per_paper.append([result[j] if j < len(result) else None for j in range(n)])
            med = []
            for j in range(n):
                vals = [pp[j] for pp in per_paper if pp[j] is not None]
                med.append(statistics.median(vals) if vals else None)
            runs.append(med)

    print("\n=== Per-paper scores across runs ===")
    print(f"{'#':>2}  {'title':<50} {'mean':>4} {'min':>2} {'max':>2} {'std':>4}")
    exact = 0
    tier_stable = 0
    for j in range(n):
        vals = [r[j] for r in runs if r[j] is not None]
        if not vals:
            print(f"{j:>2}  {papers[j]['title'][:50]:<50}  (missing in some runs)")
            continue
        mean = statistics.mean(vals)
        std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        if len(vals) == len(runs) and max(vals) == min(vals):
            exact += 1
        if len({tier(v) for v in vals}) == 1:
            tier_stable += 1
        title = papers[j]["title"][:50]
        print(f"{j:>2}  {title:<50} {mean:>4.1f} {min(vals):>2} {max(vals):>2} {std:>4.2f}")

    rho_sum = 0.0
    pairs = 0
    for i in range(len(runs)):
        for k in range(i + 1, len(runs)):
            r = spearman(runs[i], runs[k])
            if r is not None:
                rho_sum += r
                pairs += 1
    avg_rho = rho_sum / pairs if pairs else 1.0

    print("\n=== Summary ===")
    print(f"Exact-score match across all runs : {exact}/{n} ({100.0 * exact / n:.0f}%)")
    print(f"Tier-stable across all runs       : {tier_stable}/{n} ({100.0 * tier_stable / n:.0f}%)")
    all_stds = []
    for j in range(n):
        vals = [r[j] for r in runs if r[j] is not None]
        if len(vals) > 1:
            all_stds.append(statistics.stdev(vals))
    print(f"Average per-paper std             : {statistics.mean(all_stds) if all_stds else 0.0:.2f}")
    print(f"Average pairwise Spearman rho     : {avg_rho:.3f}")
    print("\nInterpretation: rho ~ 1.0 = same ranking; std ~ 0 = same score.")


if __name__ == "__main__":
    main()
