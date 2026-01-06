# backend/main.py
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path
from typing import Optional, Dict, Any

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# --- Make sure we can import your existing modules from the repo root ---
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.canvas import CanvasService
from processors.echo_adapter import build_echo_tables
from processors.grades_adapter import build_gradebook_tables
from ui.kpis import compute_kpis
from ai.analysis import generate_analysis


# ---------- FastAPI app setup ----------

app = FastAPI(
    title="CLE Analytics Backend",
    description="FastAPI backend that wraps the existing Canvas/Echo360 analytics logic.",
    version="0.3.0",
)

# Allow Vercel deployments + localhost dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Helpers ----------

def get_canvas_config() -> tuple[str, str]:
    """
    Read Canvas base URL and token from environment variables.

    Expected env vars (set in Render):
      - CANVAS_BASE_URL
      - CANVAS_TOKEN
    """
    base_url = os.getenv("CANVAS_BASE_URL", "").strip()
    token = os.getenv("CANVAS_TOKEN", "").strip()

    if not base_url or not token:
        raise RuntimeError(
            "Missing Canvas configuration. Set CANVAS_BASE_URL and CANVAS_TOKEN in the backend environment."
        )

    return base_url, token


def df_to_records(df: Optional[pd.DataFrame]) -> list[Dict[str, Any]]:
    """
    Convert a DataFrame to list-of-dicts safely for JSON responses.

    IMPORTANT:
    - Do NOT reset_index() here. Streamlit shows index separately; turning it into a column changes table shape.
    - Normalize NaN -> None for JSON.
    """
    if df is None or df.empty:
        return []
    out = df.copy()
    out = out.where(pd.notnull(out), None)
    return out.to_dict(orient="records")


def df_to_records_with_index(df: Optional[pd.DataFrame], index_name: str) -> list[Dict[str, Any]]:
    """
    For Streamlit-style summary rows where the index is meaningful (e.g., Average / % Turned In),
    materialize the index into a named column (NOT a generic 'index' column).
    """
    if df is None or df.empty:
        return []
    out = df.copy()
    out.index.name = index_name
    out = out.reset_index()
    out = out.where(pd.notnull(out), None)
    return out.to_dict(orient="records")


def sort_by_canvas_order(df: pd.DataFrame, module_col: str, canvas_df: pd.DataFrame) -> pd.DataFrame:
    """
    Sort a dataframe by Canvas module order using module_position; tolerate duplicate names.
    Mirrors the Streamlit helper so module ordering matches Canvas.
    """
    if (
        df is None
        or df.empty
        or canvas_df is None
        or canvas_df.empty
        or module_col not in df.columns
    ):
        return df

    if "module_name" not in canvas_df.columns or "module_position" not in canvas_df.columns:
        return df

    order = (
        canvas_df[["module_name", "module_position"]]
        .dropna(subset=["module_name", "module_position"])
        .drop_duplicates(subset=["module_name"])
        .set_index("module_name")["module_position"]
        .to_dict()
    )

    df2 = df.copy()
    df2["_module_pos"] = df2[module_col].map(order)

    # Push unknown modules to end
    df2["_module_pos"] = df2["_module_pos"].fillna(10**9).astype(int)

    df2 = df2.sort_values(["_module_pos", module_col], kind="stable").drop(columns=["_module_pos"])
    return df2


def get_canvas_context(course_id: int) -> dict:
    """
    Fetch Canvas-derived context: module order dataframe and student count.
    Reads base URL + token from environment.
    """
    base_url, token = get_canvas_config()

    svc = CanvasService(base_url, token)
    try:
        canvas_order_df = svc.build_order_df(course_id)
        student_count = svc.get_student_count(course_id)
    finally:
        svc.close()

    return {
        "canvas_order_df": canvas_order_df,
        "student_count": student_count,
    }


# ---------- Endpoints ----------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(
    course_id: int = Form(...),
    canvas_gradebook_csv: UploadFile = File(...),
    echo_analytics_csv: UploadFile = File(...),
) -> Dict[str, Any]:
    # ---------- 1) Read upload bytes ----------
    try:
        canvas_bytes = await canvas_gradebook_csv.read()
        echo_bytes = await echo_analytics_csv.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error reading uploaded files: {e}")

    # ---------- 2) Get Canvas context ----------
    try:
        ctx = get_canvas_context(course_id)
        canvas_order_df: pd.DataFrame = ctx["canvas_order_df"]
        student_count: Optional[int] = ctx["student_count"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching Canvas context: {e}")

    # ---------- 3) Build tables (same processors as Streamlit) ----------
    try:
        echo_tables = build_echo_tables(io.BytesIO(echo_bytes), canvas_order_df, class_total_students=student_count)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error building Echo tables: {e}")

    try:
        gradebook_tables = build_gradebook_tables(io.BytesIO(canvas_bytes), canvas_order_df)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error building Gradebook tables: {e}")

    # ---------- 4) Ensure module ordering matches Canvas ----------
    try:
        echo_module_sorted = sort_by_canvas_order(echo_tables.module_table, "Module", canvas_order_df)
        gb_module_sorted = sort_by_canvas_order(
            gradebook_tables.module_assignment_metrics_df, "Module", canvas_order_df
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sorting tables by Canvas order: {e}")

    # ---------- 5) KPIs ----------
    try:
        kpis: Dict[str, Any] = compute_kpis(
            echo_tables=echo_tables,
            gb_tables=gradebook_tables,
            students_from_canvas=student_count,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error computing KPIs: {e}")

    # ---------- 6) AI summary ----------
    analysis_text: Optional[str] = None
    analysis_error: Optional[str] = None
    try:
        analysis_text = generate_analysis(
            kpis=kpis,
            echo_module_df=echo_module_sorted,
            gradebook_module_df=gb_module_sorted,
            gradebook_summary_df=gradebook_summary_df,
        )
    except Exception as e:
        analysis_error = str(e)

    # ---------- 7) Response (Streamlit-parity tables only) ----------
    response: Dict[str, Any] = {
        "kpis": kpis,
        "echo": {
            "summary": df_to_records(echo_tables.echo_summary),
            "modules": df_to_records(echo_module_sorted),
        },
        "grades": {
            # Streamlit shows index labels as row headers; we send them as a named column "Metric"
            "summary": df_to_records_with_index(gradebook_tables.gradebook_summary_df, "Metric"),
            "module_metrics": df_to_records(gb_module_sorted),
        },
        "analysis": {
            "text": analysis_text,
            "error": analysis_error,
        },
    }

    return response
