"""
STEP 5: The API layer.

Wraps detector.py in a live HTTP service so any frontend (a dashboard,
a browser extension, a mobile app) can call it.

Run it locally with:
    uvicorn api:app --reload

Then open http://127.0.0.1:8000/docs -- FastAPI auto-generates an
interactive test page there, so you can try it without writing a frontend.
"""

import csv
import io
from datetime import datetime
from typing import List

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from detector import detect_subscriptions
from dark_pattern_detector import detect_flow

app = FastAPI(
    title="Zombie Subscription Detector API",
    description="Upload transactions, get back detected recurring subscriptions.",
    version="0.1.0",
)

# Allows a frontend running on a different origin (e.g. localhost:3000,
# or your deployed dashboard's URL) to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your real frontend's URL before going to production
    allow_methods=["*"],
    allow_headers=["*"],
)


class Transaction(BaseModel):
    date: str      # ISO format, e.g. "2026-03-14"
    merchant: str
    amount: float


class Subscription(BaseModel):
    merchant: str
    frequency: str
    avg_amount: float
    num_charges_seen: int
    days_since_last_charge: int
    estimated_annual_cost: float
    usage_signal: str


class DetectionResponse(BaseModel):
    subscriptions: List[Subscription]
    total_estimated_annual_cost: float
    transactions_scanned: int


def _run_detection(raw_transactions: List[dict]) -> DetectionResponse:
    """Shared logic for both endpoints below."""
    parsed = []
    for t in raw_transactions:
        try:
            parsed.append({
                "date": datetime.fromisoformat(t["date"]).date(),
                "merchant": t["merchant"],
                "amount": float(t["amount"]),
            })
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Bad transaction row {t}: {e}")

    subscriptions = detect_subscriptions(parsed)
    total = round(sum(s["estimated_annual_cost"] for s in subscriptions), 2)

    return DetectionResponse(
        subscriptions=subscriptions,
        total_estimated_annual_cost=total,
        transactions_scanned=len(parsed),
    )


class DarkPatternRequest(BaseModel):
    steps: List[str]  # one string per screen in the cancellation flow, in order


@app.post("/detect/dark-pattern")
def detect_dark_pattern(request: DarkPatternRequest):
    """Send the text of each screen in a cancellation flow, in order.
    Returns a risk score and which manipulative tactics were detected."""
    if not request.steps:
        raise HTTPException(status_code=400, detail="Provide at least one step of flow text")
    return detect_flow(request.steps)


@app.get("/")
def root():
    """Friendly landing response so visiting the bare URL isn't confusing."""
    return {
        "message": "Zombie Subscription Detector API is running.",
        "try_this": "/docs",
        "health_check": "/health",
    }


@app.get("/health")
def health():
    """Basic liveness check -- hitting this confirms the API is up."""
    return {"status": "ok"}


@app.post("/detect", response_model=DetectionResponse)
def detect_from_json(transactions: List[Transaction]):
    """Send transactions directly as JSON. Good for when a frontend already
    has the data (e.g. pulled live from Plaid) and just needs it analyzed."""
    return _run_detection([t.dict() for t in transactions])


@app.post("/detect/upload-csv", response_model=DetectionResponse)
async def detect_from_csv(file: UploadFile = File(...)):
    """Upload a CSV with columns: date, merchant, amount.
    This is the easiest way to test the API by hand -- e.g. with the
    transactions.csv from Step 2."""
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    contents = await file.read()
    reader = csv.DictReader(io.StringIO(contents.decode("utf-8")))
    rows = list(reader)
    return _run_detection(rows)
