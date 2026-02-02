# ai/analysis.py
from __future__ import annotations

import json
import os
from typing import Optional, Dict, Any, List

import pandas as pd
from openai import AzureOpenAI

# --- Card contract (frontend-friendly) ---
CARD_ORDER = [
    ("general_overview", "General Overview"),
    ("echo360_engagement", "Echo360 Engagement"),
    ("gradebook_trends", "Gradebook Trends"),
    ("notable_trends", "Notable Trends"),
    ("further_investigations", "Further Investigations"),
]

ALLOWED_TONES = {"good", "warn", "bad", "neutral"}

# Keep your content instructions the same, but add a strict output contract.
SYSTEM_PROMPT = """You are an academic learning analytics assistant.
Write a concise, plain-English analysis for instructors teaching online asychronous courses.

Content Rules (keep these exactly):
- Be specific: cite modules and metrics with percentages/counts.
- Call out trends and outliers.
- Focus on descriptions of the data.
- Do not make teaching recommendations. Only report on the data.
- Keep it under ~750 words unless asked for more.
- Always provide these same sections with these headings: "General Overview", "Echo360 Engagement", "Gradebook Trends", "Notable Trends", and "Further Investigations", in that order.

OUTPUT FORMAT (required):
Return ONLY valid JSON (no Markdown, no extra text) in this exact shape:

{
  "version": "1.0",
  "cards": [
    {
      "id": "general_overview",
      "title": "General Overview",
      "summary": "1–3 sentences, plain text.",
      "bullets": ["2–6 short bullet strings (plain text)"],
      "metrics": [{"label":"...", "value":"...", "tone":"good|warn|bad|neutral"}]
    },
    ... (exactly 5 cards total; same order; same ids/titles)
  ]
}

Rules for JSON:
- cards MUST be exactly 5 objects in the order and ids/titles specified above.
- Each card must include: id, title, summary, bullets, metrics.
- bullets: 2–6 items (use [] only if truly nothing meaningful can be said).
- metrics: 0–4 items. If uncertain, leave metrics empty rather than inventing numbers.
- tone must be one of: good, warn, bad, neutral.
- No extra keys anywhere. No trailing commas. Must parse with json.loads.
"""


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _get_ai_client() -> AzureOpenAI:
    """
    Expected environment variables:
      - AZURE_OPENAI_ENDPOINT        e.g. "https://my-openai-resource.openai.azure.com"
      - AZURE_OPENAI_API_KEY         key from the Azure OpenAI resource
      - AZURE_OPENAI_API_VERSION     optional (defaults below)
    """
    endpoint = _get_env("AZURE_OPENAI_ENDPOINT")
    api_key = _get_env("AZURE_OPENAI_API_KEY")
    api_version = _get_env("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

    if not endpoint or not api_key:
        raise RuntimeError(
            "Missing Azure OpenAI configuration. "
            "Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY as environment variables."
        )

    return AzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version,
    )


def _df_to_markdown(df: Optional[pd.DataFrame], max_rows: int = 30) -> str:
    if df is None or df.empty:
        return "(empty)"
    df2 = df.copy().head(max_rows)

    # Round percentage-like columns if any are numeric fractions
    for c in df2.columns:
        if df2[c].dtype.kind in "fc":
            s = df2[c]
            try:
                frac_like = s.between(0, 1, inclusive="both").mean() > 0.6
            except Exception:
                frac_like = False
            if frac_like:
                df2[c] = (s * 100).round(1).astype(str) + "%"

    return df2.to_markdown(index=False)


def _blank_report(note: str) -> Dict[str, Any]:
    """Safe fallback that still matches the contract."""
    cards: List[Dict[str, Any]] = []
    for cid, title in CARD_ORDER:
        cards.append(
            {
                "id": cid,
                "title": title,
                "summary": note,
                "bullets": [],
                "metrics": [],
            }
        )
    return {"version": "1.0", "cards": cards}


def _normalize_report(obj: Any) -> Dict[str, Any]:
    """
    Ensure the model output exactly matches the contract:
    - version string
    - 5 cards in correct order with required keys
    - allowed tones only
    """
    if not isinstance(obj, dict):
        return _blank_report("AI analysis returned an invalid structure (non-object).")

    cards = obj.get("cards")
    if not isinstance(cards, list):
        return _blank_report("AI analysis returned no 'cards' array.")

    # Index any returned cards by id (if present)
    by_id: Dict[str, Dict[str, Any]] = {}
    for c in cards:
        if isinstance(c, dict) and isinstance(c.get("id"), str):
            by_id[c["id"]] = c

    normalized_cards: List[Dict[str, Any]] = []
    for cid, title in CARD_ORDER:
        src = by_id.get(cid, {})
        summary = src.get("summary")
        bullets = src.get("bullets")
        metrics = src.get("metrics")

        if not isinstance(summary, str) or not summary.strip():
            summary = "No analysis returned for this section."

        if not isinstance(bullets, list) or not all(isinstance(x, str) for x in bullets):
            bullets = []
        # Clip bullet count/length defensively
        bullets = [b.strip() for b in bullets if b.strip()][:6]

        if not isinstance(metrics, list):
            metrics = []
        clean_metrics: List[Dict[str, str]] = []
        for m in metrics:
            if not isinstance(m, dict):
                continue
            label = m.get("label")
            value = m.get("value")
            tone = m.get("tone", "neutral")
            if not isinstance(label, str) or not isinstance(value, str):
                continue
            if tone not in ALLOWED_TONES:
                tone = "neutral"
            clean_metrics.append({"label": label.strip(), "value": value.strip(), "tone": tone})
            if len(clean_metrics) >= 4:
                break

        normalized_cards.append(
            {
                "id": cid,
                "title": title,
                "summary": summary.strip(),
                "bullets": bullets,
                "metrics": clean_metrics,
            }
        )

    return {"version": "1.0", "cards": normalized_cards}


def generate_analysis(
    kpis: dict,
    echo_module_df: Optional[pd.DataFrame],
    gradebook_module_df: Optional[pd.DataFrame],
    gradebook_summary_df: Optional[pd.DataFrame],
    model: str = "gpt-4o-mini",
    temperature: float = 0.3,
) -> str:
    """
    Returns a JSON STRING that matches the cards contract.
    Keep your API shape the same: result.analysis.text remains a string,
    but now it's parseable JSON for the frontend to render as cards.
    """
    # Build a compact, de-identified payload
    kpi_lines = []
    for k, v in (kpis or {}).items():
        if v is None:
            continue
        if isinstance(v, float) and 0 <= v <= 1:
            kpi_lines.append(f"- {k}: {v*100:.1f}%")
        else:
            kpi_lines.append(f"- {k}: {v}")

    payload = f"""
Data for analysis (de-identified):

# KPIs
{os.linesep.join(kpi_lines) if kpi_lines else "(none)"}

# Echo Module Metrics (per-module)
{_df_to_markdown(echo_module_df)}

# Gradebook Summary Rows
{_df_to_markdown(gradebook_summary_df)}

# Gradebook Module Metrics (per-module)
{_df_to_markdown(gradebook_module_df)}

Additional analysis rules:
- Identify general trends and data points worthy of further investigation.
- No need to list each section of the course individually. Call out aspects that seem important.
- Provide a short summary at the end of each section (put it into the card.summary field).
- In the "Notable Trends" section, compare overall patterns between Gradebook Module Metrics and Echo Module Metrics.
""".strip()

    client = _get_ai_client()

    # In Azure OpenAI, "model" here should be your deployment name
    deployment_name = _get_env("AZURE_OPENAI_DEPLOYMENT", model)

    # Try to force JSON mode (supported on many Azure deployments).
    # If Azure rejects response_format, we fall back gracefully.
    try:
        resp = client.chat.completions.create(
            model=deployment_name,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
    except Exception:
        # Fallback call without response_format (older deployments/APIs)
        resp = client.chat.completions.create(
            model=deployment_name,
            temperature=temperature,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )

    raw = (resp.choices[0].message.content or "").strip()

    # Parse + normalize to guarantee contract
    try:
        obj = json.loads(raw)
        normalized = _normalize_report(obj)
    except Exception:
        # If the model output isn't parseable JSON, return a valid contract with the raw text tucked into the first card.
        normalized = _blank_report("AI analysis could not be parsed as JSON.")
        normalized["cards"][0]["bullets"] = [raw[:5000]] if raw else []

    return json.dumps(normalized, ensure_ascii=False)
