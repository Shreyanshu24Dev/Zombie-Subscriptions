"""
Proves the Gmail parsing logic works, without needing a real Google
account -- we can't test the OAuth handshake itself outside a real
deployment (Google's servers aren't reachable from here), but the actual
value-add logic (turning an email into a transaction) is fully testable.
"""

from gmail_integration import parse_amount, parse_merchant, parse_email_date
from detector import detect_subscriptions
from datetime import datetime

# --- Unit-level checks on the parsing helpers ---
assert parse_amount("Your receipt: $14.99 charged today") == 14.99
assert parse_amount("Total: $1,299.00") == 1299.00
assert parse_amount("No amount mentioned here") is None
assert parse_merchant("Netflix <info@mailer.netflix.com>") == "Netflix"
assert parse_merchant("billing@spotify.com") == "Spotify"
assert parse_email_date("Mon, 3 Aug 2026 09:15:00 -0700") == datetime(2026, 8, 3).date()
print("All parsing unit checks passed.")

# --- Simulate what fetch_subscription_transactions would build from
#     6 months of realistic receipt-style emails, then run full detection ---
fake_parsed_transactions = []
for month in range(1, 7):
    fake_parsed_transactions.append({
        "date": f"2026-{month:02d}-05",
        "merchant": "Netflix",
        "amount": 15.49,
    })
    fake_parsed_transactions.append({
        "date": f"2026-{month:02d}-12",
        "merchant": "Spotify",
        "amount": 11.99,
    })

parsed = [
    {"date": datetime.fromisoformat(t["date"]).date(), "merchant": t["merchant"], "amount": t["amount"]}
    for t in fake_parsed_transactions
]
results = detect_subscriptions(parsed, today=datetime(2026, 9, 1).date())

print(f"\nFrom {len(fake_parsed_transactions)} simulated Gmail-derived transactions:")
for r in results:
    print(f"- {r['merchant']}: {r['frequency']}, ~${r['estimated_annual_cost']}/yr")
