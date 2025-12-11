# backend/main.py
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any, List

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# --- Make sure we can import your existing modules from the repo root ---
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.canvas import CanvasService
from processors.echo_adapter import build_echo_tables, EchoTables
from processors.grades_adapter import build_gradebook_tables, GradebookTables
from ui.kpis import compute_kpis
from ai.analysis import generate_analysis


# ---------- FastAPI app setup ----------

app = FastAPI(
    title="CLE Analytics Backend",
    description="FastAPI backend that wraps the existing Canvas/Echo360 analytics logic.",
    version="0.2.0",
)

# ⚠️ Update this list with your actual Vercel + backend URLs
ALLOWED_ORIGINS: List[str] = [
    "https://dashboard-v7-frontend.vercel.app",  # <-- update to your real Vercel URL
    "https://dashboard-v7-next.onrender.com/",
    "http://localhost:3000",                     # optional, for local dev later
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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
            "Missing Canvas configuration. "
            "Set CANVAS_BASE_URL and CANVAS_TOKEN in the backend environment."
        )

    return base_url, token


def df_to_records(df: Optional[pd.DataFrame]) -> list[Dict[str, Any]]:
    """Convert a DataFrame to list-of-dicts safely for JSON responses."""
    if df is None or df.empty:
        return []
    return df.reset_index().to_dict(orient="records")


def sort_by_canvas_order(df: pd.DataFrame, module_col: str, canvas_df: pd.DataFrame) -> pd.DataFrame:
    """
    Sort a dataframe by Canvas module order using module_position; tolerate duplicate names.

    This is a copy of the helper from the Streamlit app so the backend
    returns modules in the same order instructors see in Canvas.
    """
    if (
        df is None
        or df.empty
        or canvas_df is None
        or canvas_df.empty
        or module_col not in df.columns
    ):
        return df

    # Build ordered list of module names (keep first occurrence only)
    order = (
        canvas_df[["module", "module_position"]]
        .dropna(subset=["module"])
        .sort_values(["module_position", "module"], kind="stable")
    )
    # Deduplicate module names while preserving order
    categories = pd.unique(order["module"].astype(str))

    if len(categories) == 0:
        return df

    out = df.copy()
    out[module_col] = out[module_col].astype(str)
    out[module_col] = pd.Categorical(out[module_col], categories=categories, ordered=True)
    out = out.sort_values(module_col).reset_index(drop=True)
    # Return as string for downstream display
    out[module_col] = out[module_col].astype(str)
    return out


def get_canvas_context(course_id: int) -> dict:
    """
    Fetch Canvas-derived context: module order dataframe and student count.

    This mirrors the Streamlit app's:
      - fetch_canvas_order_df(...)
      - fetch_student_count(...)
    but reads base URL + token from environment.
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
    """Simple health check endpoint."""
    return {"status": "ok"}


@app.post("/analyze")
async def analyze_course(
    course_id: int = Form(..., description="Canvas course ID"),
    canvas_gradebook_csv: UploadFile = File(..., description="Canvas gradebook export CSV"),
    echo_analytics_csv: UploadFile = File(..., description="Echo360 analytics CSV export"),
) -> Dict[str, Any]:
    """
    Core endpoint: accepts course ID + two CSV uploads and returns JSON with
    de-identified analytics outputs.

    Canvas base URL and token are read from environment variables:
      - CANVAS_BASE_URL
      - CANVAS_TOKEN
    """

    # ---------- 1) Canvas context (module order + student count) ----------
    try:
        ctx = get_canvas_context(int(course_id))
    except RuntimeError as e:
        # Config error – expose as 500 with clear message
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Canvas fetch error: {e}")

    canvas_order_df: pd.DataFrame = ctx["canvas_order_df"]
    student_count: Optional[int] = ctx["student_count"]

    # ---------- 2) Read uploaded CSVs ----------
    try:
        canvas_bytes = await canvas_gradebook_csv.read()
        echo_bytes = await echo_analytics_csv.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error reading uploaded files: {e}")

    # ---------- 3) Run existing processors ----------
    try:
        echo_tables: EchoTables = build_echo_tables(
            io.BytesIO(echo_bytes),
            canvas_order_df,
            class_total_students=student_count,
        )

        gradebook_tables: GradebookTables = build_gradebook_tables(
            io.BytesIO(canvas_bytes),
            canvas_order_df,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing CSV data: {e}")

    # ---------- 4) Sort Echo module table to match Canvas order ----------
    if echo_tables and echo_tables.module_table is not None:
        echo_module_table_sorted = sort_by_canvas_order(
            echo_tables.module_table,
            module_col="Module",
            canvas_df=canvas_order_df,
        )
    else:
        echo_module_table_sorted = echo_tables.module_table if echo_tables else None

    # ---------- 5) KPIs (same logic as in the Streamlit app) ----------
    try:
        kpis: Dict[str, Any] = compute_kpis(
            echo_tables=echo_tables,
            gb_tables=gradebook_tables,
            students_from_canvas=student_count,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error computing KPIs: {e}")

    # ---------- 6) AI-generated summary (using ai/analysis.py) ----------
    analysis_text: Optional[str] = None
    analysis_error: Optional[str] = None
    try:
        analysis_text = generate_analysis(
            kpis=kpis,
            echo_module_df=echo_module_table_sorted,
            gradebook_module_df=gradebook_tables.module_assignment_metrics_df,
            gradebook_summary_df=gradebook_tables.gradebook_summary_df,
            # 'model' here matches the deployment/model name expected by analysis.py
            model="gpt-4o-mini",
            temperature=0.3,
        )
    except Exception as e:
        # Don't fail the whole request if Azure OpenAI config is missing/misconfigured
        analysis_error = str(e)

    # ---------- 7) Build JSON-safe response ----------
    response: Dict[str, Any] = {
        "course_id": course_id,
        "student_count": student_count,
        "kpis": kpis,
        "echo": {
            "summary": df_to_records(echo_tables.echo_summary),
            "modules": df_to_records(echo_module_table_sorted),
            "students": df_to_records(echo_tables.student_table),
        },
        "grades": {
            "gradebook": df_to_records(gradebook_tables.gradebook_df),
            "summary": df_to_records(gradebook_tables.gradebook_summary_df),
            "module_metrics": df_to_records(gradebook_tables.module_assignment_metrics_df),
        },
        "analysis": {
            "text": analysis_text,
            "error": analysis_error,
        },
    }

    return response
