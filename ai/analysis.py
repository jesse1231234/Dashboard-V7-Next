# ai/analysis.py
from __future__ import annotations

import os
from typing import Optional

import pandas as pd
from openai import AzureOpenAI

SYSTEM_PROMPT = """You are an academic learning analytics assistant.
Write a concise, plain-English analysis for instructors teaching online asychronous courses.
Rules:
- Be specific: cite modules and metrics with percentages/counts.
- Call out trends and outliers.
- Focus on descriptions of the data.
- Do not make teaching recommendations. Only report on the data.
- Keep it under ~750 words unless asked for more.
- Use Appropriate Heading structure.
- Always provide these same sections with these headings: "General Overview", "Echo360 Engagement", "Gradebook Trends", "Notable Trends", and "Further Investigations", in that order.
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

def generate_analysis(
    kpis: dict,
    echo_module_df: Optional[pd.DataFrame],
    gradebook_module_df: Optional[pd.DataFrame],
    gradebook_summary_df: Optional[pd.DataFrame],
    model: str = "gpt-4o-mini",
    temperature: float = 0.3,
) -> str:
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

Instructions:
- Be specific: cite modules and metrics with percentages/counts.
- Call out trends and outliers.
- Focus on descriptions of the data.
- Identify general trends and data points worthy of further investigation.
- No need to list each section of the course individually. Simply call out aspects of the data that seem important for further investigation.
- Provide a short summary at the end of each section.
- In the "Notable Patterns" section, compare the general values within the Gradebook Module Metrics and the Echo Module Metrics.
""".strip()

    client = _get_ai_client()

    # In Azure OpenAI, "model" here should be your *deployment name*
    deployment_name = _get_env("AZURE_OPENAI_DEPLOYMENT", model)

    try:
        resp = client.chat.completions.create(
            model=deployment_name,
            temperature=temperature,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
    except Exception as e:
        raise RuntimeError(
            f"Azure OpenAI call failed for deployment '{deployment_name}': {e}"
        )

    return (resp.choices[0].message.content or "").strip()
