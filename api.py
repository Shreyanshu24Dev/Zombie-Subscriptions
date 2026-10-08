"""
STEP 5: The API layer.

Wraps detector.py in a live HTTP service so any frontend (a dashboard,
a browser extension, a mobile app) can call it.

Run it locally with:
    uvicorn api:app --reload

Then open http://127.0.0.1:8000/docs -- FastAPI auto-generates an
interactive test page there, so you can try it without writing a frontend.
"""

import base64
import csv
import io
import json
import os
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

from detector import (
    detect_subscriptions,
    merge_transaction_files,
    detect_recurring_multi_month,
    CSVValidationError,
)
from dark_pattern_detector import detect_flow
from gmail_integration import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_subscription_transactions,
    get_user_email,
    send_email,
)

app = FastAPI(
    title="Zombie Subscription Detector API",
    description="Upload transactions, get back detected recurring subscriptions.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

FEEDBACK_CSV_PATH = "feedback.csv"
FEEDBACK_CSV_HEADER = ["timestamp", "merchant", "predicted_subscription", "feedback"]


class Transaction(BaseModel):
    date: str
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
    steps: List[str]


@app.post("/detect/dark-pattern")
def detect_dark_pattern(request: DarkPatternRequest):
    if not request.steps:
        raise HTTPException(status_code=400, detail="Provide at least one step of flow text")
    return detect_flow(request.steps)


class FeedbackRequest(BaseModel):
    merchant: str
    estimated_annual_cost: float
    feedback: str


@app.post("/feedback")
def submit_feedback(request: FeedbackRequest):
    if request.feedback not in ("still_using", "cancel"):
        raise HTTPException(status_code=400, detail="feedback must be 'still_using' or 'cancel'")

    file_exists = os.path.isfile(FEEDBACK_CSV_PATH)
    with open(FEEDBACK_CSV_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(FEEDBACK_CSV_HEADER)
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            request.merchant,
            True,
            request.feedback,
        ])
    return {"status": "recorded"}


@app.get("/feedback/summary")
def feedback_summary():
    if not os.path.isfile(FEEDBACK_CSV_PATH):
        return {"cancelled_merchants": [], "num_cancelled": 0}
    cancelled = []
    with open(FEEDBACK_CSV_PATH) as f:
        for row in csv.DictReader(f):
            if row.get("feedback") == "cancel":
                cancelled.append(row["merchant"])
    return {"cancelled_merchants": cancelled, "num_cancelled": len(cancelled)}


class SubscriptionSummaryItem(BaseModel):
    merchant: str
    estimated_annual_cost: float


class BatchNotifyRequest(BaseModel):
    gmail_token: str
    cancelled: List[SubscriptionSummaryItem] = []
    kept: List[SubscriptionSummaryItem] = []


@app.post("/gmail/notify-batch")
def notify_batch(request: BatchNotifyRequest):
    if not request.cancelled and not request.kept:
        raise HTTPException(status_code=400, detail="Nothing to summarize yet")

    to_email = get_user_email(request.gmail_token)
    lines = ["Here's a summary of your subscription decisions:", ""]

    if request.cancelled:
        total_saved = sum(c.estimated_annual_cost for c in request.cancelled)
        lines.append("Marked for cancellation:")
        for c in request.cancelled:
            lines.append(f"- {c.merchant}: ${c.estimated_annual_cost:,.2f}/year")
        lines.append(f"Potential savings: ${total_saved:,.2f}/year")
        lines.append("")

    if request.kept:
        lines.append("Kept active:")
        for k in request.kept:
            lines.append(f"- {k.merchant}: ${k.estimated_annual_cost:,.2f}/year")
        lines.append("")

    lines.append(
        "Reminder: this app can't cancel real charges for you -- log in to each "
        "merchant or your card issuer to actually stop payment on anything above."
    )
    lines.append("\n-- Zombie Subscription Detector")

    send_email(request.gmail_token, to_email, "Your Subscription Decisions Summary", "\n".join(lines))
    return {"sent": True, "to": to_email, "cancelled_count": len(request.cancelled), "kept_count": len(request.kept)}


@app.get("/gmail/authorize")
def gmail_authorize(request: Request, return_to: str = None):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="Gmail integration isn't configured yet -- GOOGLE_CLIENT_ID is missing.",
        )
    redirect_uri = f"{str(request.base_url).rstrip('/')}/gmail/callback"
    state_payload = {"nonce": secrets.token_urlsafe(8), "return_to": return_to or ""}
    state = base64.urlsafe_b64encode(json.dumps(state_payload).encode()).decode()
    return RedirectResponse(build_authorize_url(GOOGLE_CLIENT_ID, redirect_uri, state))


@app.get("/gmail/callback")
def gmail_callback(request: Request, code: str = None, error: str = None, state: str = None):
    return_to = None
    if state:
        try:
            payload = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
            return_to = payload.get("return_to") or None
        except Exception:
            return_to = None

    if error:
        if return_to:
            return RedirectResponse(f"{return_to}?gmail_error={error}")
        return HTMLResponse(
            f"<h3>Gmail connection was cancelled.</h3><p>({error})</p>"
            f"<p><a href='/gmail/authorize'>Try again</a></p>"
        )
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code from Google")

    redirect_uri = f"{str(request.base_url).rstrip('/')}/gmail/callback"
    access_token = exchange_code_for_token(code, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, redirect_uri)

    if return_to:
        return RedirectResponse(f"{return_to}?gmail_token={access_token}")

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


@app.post("/detect/combined", response_model=DetectionResponse)
async def detect_combined(file: UploadFile = File(None), gmail_token: str = Form(None)):
    all_raw = []

    if file is not None:
        contents = await file.read()
        reader = csv.DictReader(io.StringIO(contents.decode("utf-8")))
        all_raw.extend(list(reader))

    if gmail_token:
        all_raw.extend(fetch_subscription_transactions(gmail_token))

    if not all_raw:
        raise HTTPException(status_code=400, detail="Provide a CSV file, a Gmail token, or both")

    return _run_detection(all_raw)


@app.get("/")
def root():
    return {
        "message": "Zombie Subscription Detector API is running.",
        "try_this": "/docs",
        "health_check": "/health",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/detect", response_model=DetectionResponse)
def detect_from_json(transactions: List[Transaction]):
    return _run_detection([t.dict() for t in transactions])


@app.post("/detect/upload-csv", response_model=DetectionResponse)
async def detect_from_csv(file: UploadFile = File(...)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    contents = await file.read()
    reader = csv.DictReader(io.StringIO(contents.decode("utf-8")))
    rows = list(reader)
    return _run_detection(rows)


class MultiMonthMerchantResult(BaseModel):
    merchant: str
    normalized_merchant: str
    charge_count: int
    avg_amount: float
    avg_interval_days: Optional[float] = None
    confidence: str  # "confirmed_recurring" | "insufficient_data" | "same_month_duplicate"


class MultiMonthDetectionResponse(BaseModel):
    merchants: List[MultiMonthMerchantResult]  # confirmed subscriptions ONLY
    other_merchants_not_flagged: int = 0       # one-offs etc., counted but not listed
    transactions_scanned: int
    warning: Optional[str] = None


async def _read_csv_upload(upload_file: UploadFile) -> list:
    if not upload_file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail=f"{upload_file.filename} is not a .csv file")
    contents = await upload_file.read()
    return list(csv.DictReader(io.StringIO(contents.decode("utf-8"))))


@app.post("/detect/multi-month", response_model=MultiMonthDetectionResponse)
async def detect_multi_month(file: UploadFile = File(...), file2: UploadFile = File(None)):
    """Upload one CSV (required) and a second CSV covering a different
    month (optional). With only one file, this behaves like a plain
    recurrence scan of that file. With two, transactions are merged and
    deduplicated first, which is what lets a subscription billed once a
    month actually show up as 'confirmed_recurring' -- a single month's
    data alone can never have two charges to compare."""
    rows1 = await _read_csv_upload(file)
    rows2 = await _read_csv_upload(file2) if file2 is not None else None

    try:
        merge_result = merge_transaction_files(rows1, rows2)
    except CSVValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    all_merchants = detect_recurring_multi_month(merge_result["transactions"])

    # Only return actual subscriptions -- one-off purchases, merchants seen
    # once, and same-month double charges are not subscriptions, so they
    # stay out of the response (we just count them).
    subscriptions = [m for m in all_merchants if m["confidence"] == "confirmed_recurring"]

    return MultiMonthDetectionResponse(
        merchants=subscriptions,
        other_merchants_not_flagged=len(all_merchants) - len(subscriptions),
        transactions_scanned=len(merge_result["transactions"]),
        warning=merge_result["warning"],
    )
