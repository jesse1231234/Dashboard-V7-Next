# ai/analysis.py
from __future__ import annotations
from typing import Optional
import os
import pandas as pd
import streamlit as st
from openai import AzureOpenAI

SYSTEM_PROMPT = """You are an academic learning analytics assistant.
Write a concise, plain-English analysis for instructors teaching online asychronous courses.
Rules:
- Be specific: cite modules and metrics with percentages/counts.
- Call out trends and outliers.
- Focus on descriptions of the data.
- Do not make teaching recommendations. Only report on the data.
- Keep it under ~500 words unless asked for more.
"""

def _get_ai_client() -> AzureOpenAI:
    """
    Create an Azure OpenAI client that talks directly to your Azure OpenAI resource.

    Expected secrets/env:
      - AZURE_OPENAI_ENDPOINT   e.g. "https://my-openai-resource.openai.azure.com"
      - AZURE_OPENAI_API_KEY    key from the Azure OpenAI resource
      - AZURE_OPENAI_API_VERSION (optional, default set below)
    """
    endpoint = (
        st.secrets.get("AZURE_OPENAI_ENDPOINT", None)
        or os.getenv("AZURE_OPENAI_ENDPOINT")
    )
    api_key = (
        st.secrets.get("AZURE_OPENAI_API_KEY", None)
        or os.getenv("AZURE_OPENAI_API_KEY")
    )
    api_version = (
        st.secrets.get("AZURE_OPENAI_API_VERSION", None)
        or os.getenv("AZURE_OPENAI_API_VERSION")
        or "2024-02-15-preview"   # or whatever version your resource uses
    )

    if not endpoint or not api_key:
        raise RuntimeError(
            "Missing Azure OpenAI configuration. "
            "Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY "
            "in Streamlit secrets or environment variables."
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
    # round percentage-like columns if any are numeric fractions
    for c in df2.columns:
        if df2[c].dtype.kind in "fc":
            # if it looks like a fraction, render as %
            if df2[c].between(0, 1, inclusive="both").mean() > 0.6:
                df2[c] = (df2[c] * 100).round(1).astype(str) + "%"
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
        if v is None: continue
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
- identify general trends and data points worthy of further investigation.
- No need to list each section of the course individually. Simply call out aspects of the data that seem important for further investigation.
"""
    
    # build payload above as you already do...

        # Build the payload (kpis + dataframes) above as you are already doing...

    client = _get_ai_client()

    # Deployment name in your Azure OpenAI resource, e.g. "gpt-4o-prod"
    deployment_name = (
        st.secrets.get("AZURE_OPENAI_DEPLOYMENT", None)
        or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or model  # fall back to the function arg if you want
    )

    try:
        resp = client.chat.completions.create(
            model=deployment_name,   # e.g. "gpt-4.1-mini", "grok-3", etc.
            temperature=temperature,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
    except Exception as e:
        # Nice clean error that surfaces the model name, but not internals
        raise RuntimeError(
            f"OpenAI/Foundry call failed for model '{deployment_name}': {e}"
        )

    return (resp.choices[0].message.content or "").strip()
