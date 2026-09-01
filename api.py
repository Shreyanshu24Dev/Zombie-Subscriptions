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
import os
import secrets
from datetime import datetime
from typing import List

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

from detector import detect_subscriptions
from dark_pattern_detector import detect_flow
from gmail_integration import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_subscription_transactions,
)

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

# Set these in your hosting platform's environment variables -- never
# commit real credentials to the repo. See the Gmail setup guide for
# where to get these from Google Cloud Console.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")


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


@app.get("/gmail/authorize")
def gmail_authorize(request: Request):
    """STEP 1 of Gmail connect: sends the user to Google's own consent
    screen. Nothing happens on our side yet except building the URL."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="Gmail integration isn't configured yet -- GOOGLE_CLIENT_ID is missing.",
        )
    redirect_uri = f"{str(request.base_url).rstrip('/')}/gmail/callback"
    state = secrets.token_urlsafe(16)  # basic CSRF protection
    return RedirectResponse(build_authorize_url(GOOGLE_CLIENT_ID, redirect_uri, state))


@app.get("/gmail/callback")
def gmail_callback(request: Request, code: str = None, error: str = None):
    """STEP 2 of Gmail connect: Google redirects here after the user
    approves (or denies) access. We exchange the code for a token, pull
    matching emails, and render the results directly -- no separate
    storage needed since this all happens in one request."""
    if error:
        return HTMLResponse(
            f"<h3>Gmail connection was cancelled.</h3><p>({error})</p>"
            f"<p><a href='/gmail/authorize'>Try again</a></p>"
        )
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code from Google")

    redirect_uri = f"{str(request.base_url).rstrip('/')}/gmail/callback"
    access_token = exchange_code_for_token(code, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, redirect_uri)

    transactions = fetch_subscription_transactions(access_token)
    if not transactions:
        return HTMLResponse(
            "<h3>No subscription-like emails found.</h3>"
            "<p>Try the CSV upload instead, or check back after more receipts arrive.</p>"
        )

    result = _run_detection(transactions)
    rows = "".join(
        f"<tr><td>{s.merchant}</td><td>{s.frequency}</td>"
        f"<td>${s.avg_amount:,.2f}</td><td>${s.estimated_annual_cost:,.2f}/yr</td></tr>"
        for s in result.subscriptions
    )
    return HTMLResponse(f"""
        <html><body style="font-family: -apple-system, sans-serif; padding: 2rem; max-width: 700px; margin: auto;">
            <h2>Subscriptions found in your Gmail</h2>
            <p>Scanned {result.transactions_scanned} matching emails,
               found {len(result.subscriptions)} recurring subscriptions.</p>
            <p><b>Estimated total: ${result.total_estimated_annual_cost:,.2f}/year</b></p>
            <table border="1" cellpadding="8" style="border-collapse:collapse; width:100%;">
                <tr><th>Merchant</th><th>Frequency</th><th>Avg amount</th><th>Est. annual cost</th></tr>
                {rows}
            </table>
        </body></html>
    """)


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
