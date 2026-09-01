"""
STEP 9: Gmail integration.

Lets a user connect their Gmail so we can find subscription/receipt emails
they never uploaded a bank statement for. Built with plain HTTP calls via
`requests` -- no google-api-python-client dependency needed, which keeps
this light and easy to read.

This file has NO FastAPI code in it on purpose -- it's pure logic
(build a URL, exchange a code, parse an email). api.py wires it into
actual endpoints. That separation makes both halves easier to test.
"""

import re
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import requests

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Read-only is deliberate -- this app never needs to send, delete, or
# modify anything in the user's inbox.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Emails likely to mention a subscription or recurring charge.
SEARCH_QUERY = 'subject:(receipt OR subscription OR invoice OR renewal OR payment OR "your trial")'

AMOUNT_PATTERN = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Step 1 of OAuth: the URL we send the user to so they can approve access."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "online",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str, client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Step 2 of OAuth: trade the one-time code Google gave us for an access token."""
    response = requests.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def parse_amount(text: str):
    """Pulls the first dollar amount out of a snippet/subject, e.g. '$14.99'."""
    match = AMOUNT_PATTERN.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def parse_merchant(from_header: str) -> str:
    """Turns 'Netflix <info@mailer.netflix.com>' into 'Netflix', or falls
    back to the sending domain if there's no display name."""
    display_match = re.match(r'^"?([^"<]+)"?\s*<', from_header)
    if display_match:
        return display_match.group(1).strip()
    domain_match = re.search(r"@([\w.-]+)", from_header)
    if domain_match:
        return domain_match.group(1).split(".")[0].capitalize()
    return from_header.strip() or "Unknown sender"


def parse_email_date(date_header: str):
    """Converts an email's RFC 2822 Date header into a plain date, or None
    if it's missing/malformed (some emails have inconsistent headers)."""
    try:
        return parsedate_to_datetime(date_header).date()
    except (TypeError, ValueError):
        return None


def list_candidate_messages(access_token: str, max_results: int = 50):
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(
        f"{GMAIL_API_BASE}/messages",
        headers=headers,
        params={"q": SEARCH_QUERY, "maxResults": max_results},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("messages", [])


def get_message_detail(access_token: str, message_id: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(
        f"{GMAIL_API_BASE}/messages/{message_id}",
        headers=headers,
        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def fetch_subscription_transactions(access_token: str, max_results: int = 50):
    """The main entry point: returns a list of {date, merchant, amount}
    dicts in the SAME SHAPE detector.py already expects -- so Gmail-sourced
    and CSV-sourced transactions run through identical detection logic."""
    transactions = []
    for message_ref in list_candidate_messages(access_token, max_results):
        detail = get_message_detail(access_token, message_ref["id"])
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        snippet = detail.get("snippet", "")

        amount = parse_amount(f"{snippet} {headers.get('Subject', '')}")
        if amount is None:
            continue  # can't build a transaction without a dollar amount

        date_obj = parse_email_date(headers.get("Date", ""))
        if date_obj is None:
            continue

        transactions.append({
            "date": date_obj.isoformat(),
            "merchant": parse_merchant(headers.get("From", "")),
            "amount": amount,
        })
    return transactions
