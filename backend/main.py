# backend/main.py
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import io
import pandas as pd

from services.canvas import CanvasService
from processors.echo_adapter import build_echo_tables
from processors.grades_adapter import build_gradebook_tables
from ui.kpis import compute_kpis
from ai.analysis import generate_analysis

app = FastAPI()

# Adjust origins for your Next.js domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_canvas_service(base_url: str, token: str) -> CanvasService:
    return CanvasService(base_url=base_url, token=token)

@app.post("/analyze")
async def analyze_course(
    canvas_base_url: str = Form(...),
    canvas_token: str = Form(...),
    course_id: str = Form(...),
    canvas_file: UploadFile = File(...),
    echo_file: UploadFile = File(...),
):
    # 1) Canvas student count
    with get_canvas_service(canvas_base_url, canvas_token) as svc:
        student_count = svc.get_student_count(int(course_id))

    # 2) Read uploaded files into dataframes
    canvas_bytes = await canvas_file.read()
    echo_bytes = await echo_file.read()

    canvas_df = pd.read_csv(io.BytesIO(canvas_bytes))
    # Use your adapters exactly as you already do
    echo_tables = build_echo_tables(io.BytesIO(echo_bytes), canvas_df, class_total_students=student_count)
    gradebook_tables = build_gradebook_tables(io.BytesIO(canvas_bytes), canvas_df)

    # 3) KPIs – adapt this to what compute_kpis expects/returns
    kpis = compute_kpis(echo_tables=echo_tables, gradebook_tables=gradebook_tables)

    # 4) AI analysis – again, reuse your function
    ai_summary = generate_analysis(kpis=kpis, echo_tables=echo_tables, gradebook_tables=gradebook_tables)

    # 5) Return JSON for the frontend
    return {
        "student_count": student_count,
        "kpis": kpis,
        "analysis": ai_summary,
        # You can also return sliced data for charts:
        "echo_summary": echo_tables["summary"].to_dict(orient="records"),
        "gradebook_summary": gradebook_tables["summary"].to_dict(orient="records"),
    }
