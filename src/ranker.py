"""LLM-based relevance scoring for arxiv papers using DeepSeek (OpenAI-compatible API)."""

import json
import os

from openai import OpenAI

from .profile import profile_to_prompt_text

FEEDBACK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "feedback.json")


def get_client(base_url: str, api_key: str) -> OpenAI:
    """Create an OpenAI-compatible client."""
    return OpenAI(base_url=base_url, api_key=api_key)


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
        for fb in overrated[-10:]:  # Last 10
            lines.append(f'  - "{fb["title"][:80]}" was scored {fb.get("original_score", "?")} but user found it less relevant')
    
    if underrated:
        lines.append("Papers the user found UNDERRATED (score was too low):")
        for fb in underrated[-10:]:  # Last 10
            lines.append(f'  - "{fb["title"][:80]}" was scored {fb.get("original_score", "?")} but user found it highly relevant')
    
    if len(lines) > 1:
        return "\n".join(lines)
    return ""


def build_ranking_prompt(profile_text: str, papers: list[dict]) -> str:
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
    
    prompt = f"""{profile_text}
{feedback_text}

=== Candidate Papers ===
{papers_text}

=== Task ===
You are an astrophysics research assistant. For each candidate paper above, score its relevance to the researcher's interests on a scale of 0-10, where:
- 9-10: Directly matches core research interests (chemical enrichment, metal-poor stars, globular clusters, supernovae, Population III stars, Milky Way formation, stellar abundances)
- 7-8: Closely related to main interests
- 5-6: Somewhat related, tangential topic
- 3-4: Weakly related
- 0-2: Not relevant

For each paper, provide:
1. The paper index [i]
2. A relevance score (integer 0-10)
3. A one-line reason explaining the score

Respond ONLY with a valid JSON array in this exact format, no other text:
[
  {{"index": 0, "score": 8, "reason": "Directly studies chemical enrichment in metal-poor globular clusters"}},
  {{"index": 1, "score": 3, "reason": "Tangentially related through galaxy formation topic"}},
  ...
]
"""
    return prompt


def rank_papers(
    papers: list[dict],
    profile: dict,
    llm_config: dict,
) -> list[dict]:
    """Use LLM to rank papers by relevance to the user's interests.
    
    Args:
        papers: list of candidate paper dicts
        profile: interest profile dict from profile.build_profile()
        llm_config: dict with base_url, api_key_env, model

    Returns:
        list of paper dicts with added 'score' and 'reason' fields,
        sorted by score descending
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
    model = llm_config.get("model", "deepseek-chat")
    
    client = get_client(base_url, api_key)
    profile_text = profile_to_prompt_text(profile)
    
    # Process in batches if there are many papers (to avoid token limits)
    batch_size = 20
    all_scores = []
    
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i + batch_size]
        prompt = build_ranking_prompt(profile_text, batch)
        
        print(f"  Ranking batch {i // batch_size + 1} ({len(batch)} papers)...")
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an astrophysics research assistant. Respond only with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=4000,
        )
        
        content = response.choices[0].message.content
        if content is None:
            print(f"  Warning: LLM returned empty content for batch {i // batch_size + 1}")
            for j in range(len(batch)):
                all_scores.append({"index": i + j, "score": 5, "reason": "Empty LLM response"})
            continue
        content = content.strip()
        
        # Parse JSON response
        try:
            # Handle case where LLM wraps JSON in markdown code blocks
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            
            scores = json.loads(content)
            if not isinstance(scores, list):
                raise ValueError("Expected JSON array")
            # Adjust indices for batch offset
            for s in scores:
                s["index"] = s.get("index", -1) + i
            all_scores.extend(scores)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            print(f"  Warning: Failed to parse LLM response for batch {i // batch_size + 1}")
            print(f"  Response: {content[:200]}...")
            # Assign default score of 5 for unparseable batch
            for j in range(len(batch)):
                all_scores.append({"index": i + j, "score": 5, "reason": "Failed to parse LLM score"})
    
    # Attach scores to papers
    scored_papers = []
    for s in all_scores:
        idx = s.get("index", -1)
        if 0 <= idx < len(papers):
            paper = papers[idx].copy()
            try:
                paper["score"] = int(s.get("score", 5))
            except (ValueError, TypeError):
                paper["score"] = 5
            paper["reason"] = s.get("reason", "No reason provided")
            scored_papers.append(paper)
    
    # Sort by score descending
    scored_papers.sort(key=lambda x: x["score"], reverse=True)
    
    return scored_papers
