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
from typing import List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

from detector import detect_subscriptions
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

# Where "still using this?" responses get logged. Kept as a flat CSV (not a
# database) on purpose -- this is a small MVP and a CSV is trivial to open,
# inspect, or hand off to a notebook later.
FEEDBACK_CSV_PATH = "feedback.csv"
FEEDBACK_CSV_HEADER = ["timestamp", "merchant", "predicted_subscription", "feedback"]


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


class FeedbackRequest(BaseModel):
    merchant: str
    estimated_annual_cost: float
    feedback: str  # "still_using" or "cancel"


@app.post("/feedback")
def submit_feedback(request: FeedbackRequest):
    """Records the user's answer to 'Still using this?' for one detected
    subscription. Every row that reaches this endpoint was, by definition,
    something the detector flagged as recurring -- so predicted_subscription
    is always True here; the column is kept for consistency with any future
    source (e.g. a human-labeled row) where that might not be the case."""
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
    """Rolls up every 'cancel' response logged so far -- how much money the
    user has said goodbye to, and which merchants they flagged. Useful for
    a dashboard-level 'total unlocked savings' number that survives page
    refreshes (unlike Streamlit's in-memory session state)."""
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
    """Sends ONE email covering every decision made so far in this session --
    whether the user cancelled one subscription or five, this fires once,
    only when they click 'Send summary to Gmail'."""
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
    """STEP 1 of Gmail connect. If return_to is given (your dashboard's own
    URL), it's carried through Google's round-trip via the state parameter,
    so the callback can send the user back into your dashboard's normal
    results UI -- the same feedback cards CSV uploads already use -- instead
    of a disconnected standalone page."""
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
    """STEP 2 of Gmail connect. If a return_to was carried in state, redirect
    there with the token attached so the dashboard can pick it up and run it
    through the SAME detection + feedback-card flow as a CSV upload. Falls
    back to a standalone results page only if no return_to was provided
    (e.g. someone hits this URL directly, without going through a dashboard)."""
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
        # Hand the token back to the dashboard via URL param -- good enough
        # for an MVP; a hardened version would use a short-lived server-side
        # session instead of putting the token in a URL.
        return RedirectResponse(f"{return_to}?gmail_token={access_token}")

    # Fallback for testing the flow directly in a browser, without a dashboard.
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
    """Merges CSV-sourced and Gmail-sourced transactions into ONE detection
    pass -- a subscription seen in both sources doesn't get double-counted,
    it's just a bigger transaction list feeding the same clustering logic.
    Either input alone still works fine on its own."""
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