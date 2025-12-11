# backend/main.py
import sys
from pathlib import Path

# --- make sure we can import your existing code from the repo root ---
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import io
import pandas as pd

# ✅ adjust these imports to match your real module/function names
from services.canvas import CanvasService
from processors.echo_adapter import build_echo_tables
from processors.grades_adapter import build_gradebook_tables
# If you have something like this:
# from ui.kpis import compute_kpis
# from ai.analysis import generate_analysis

app = FastAPI()

# CORS so a Next.js frontend can talk to this later
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock this down later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


def get_canvas_service(base_url: str, token: str) -> CanvasService:
    """Helper to construct your Canvas service."""
    return CanvasService(base_url=base_url, token=token)


@app.post("/analyze")
async def analyze_course(
    canvas_base_url: str = Form(...),
    canvas_token: str = Form(...),
    course_id: str = Form(...),
    canvas_file: UploadFile = File(...),
    echo_file: UploadFile = File(...),
):
    """
    Core endpoint: accepts Canvas+Echo CSVs and returns JSON.
    Later the Next.js UI will call this.
    """
    # 1) Canvas student count (adjust to your real method)
    with get_canvas_service(canvas_base_url, canvas_token) as svc:
        student_count = svc.get_student_count(int(course_id))

    # 2) Read uploaded files into dataframes / buffers
    canvas_bytes = await canvas_file.read()
    echo_bytes = await echo_file.read()

    canvas_df = pd.read_csv(io.BytesIO(canvas_bytes))

    # 3) Use your existing logic
    echo_tables = build_echo_tables(
        io.BytesIO(echo_bytes),
        canvas_df,
        class_total_students=student_count,
    )
    gradebook_tables = build_gradebook_tables(
        io.BytesIO(canvas_bytes),
        canvas_df,
    )

    # 4) If you have KPI + AI functions, plug them in here
    # kpis = compute_kpis(echo_tables=echo_tables, gradebook_tables=gradebook_tables)
    # ai_summary = generate_analysis(
    #     kpis=kpis,
    #     echo_tables=echo_tables,
    #     gradebook_tables=gradebook_tables,
    # )

    # 5) Return something simple for now; we can expand later
    return {
        "student_count": student_count,
        "echo_summary": echo_tables["summary"].to_dict(orient="records")
        if "summary" in echo_tables
        else {},
        "gradebook_preview": gradebook_tables["summary"].to_dict(orient="records")
        if "summary" in gradebook_tables
        else {},
        # "kpis": kpis,
        # "analysis": ai_summary,
    }
