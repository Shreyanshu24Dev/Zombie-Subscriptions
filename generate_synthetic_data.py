"""
STEP 2 helper: Generates fake bank-transaction data that looks like a real
Plaid export, so we have something to test the detector against before
connecting a real bank account.

Run it with:  python generate_synthetic_data.py
Output:       transactions.csv
"""

import csv
import random
from datetime import date, timedelta

random.seed(42)

TODAY = date(2026, 9, 1)
START = TODAY - timedelta(days=420)  # ~14 months of history

# Recurring subscriptions we want the detector to find.
# (merchant name as it might actually appear on a statement, amount, interval in days)
SUBSCRIPTIONS = [
    ("NETFLIX.COM", 15.49, 30),
    ("SPOTIFY USA", 11.99, 30),
    ("PLANET FITNESS #4471", 24.99, 30),
    ("ADOBE  *CREATIVE CLOUD", 54.99, 30),
    ("AMAZON PRIME*MEMBERSHIP", 139.00, 365),
    ("NYTIMES.COM/SUBSCRIBE", 4.25, 30),
    ("DISNEY PLUS", 13.99, 30),
]

# One-off purchases, to make sure the detector doesn't falsely flag these.
ONE_OFF_MERCHANTS = [
    "TRADER JOES #112", "SHELL OIL", "AMAZON.COM*MKTPLACE",
    "UBER *TRIP", "TARGET T-1902", "STARBUCKS #221", "DOORDASH*ORDER",
]


def generate_subscription_charges(merchant, amount, interval_days):
    rows = []
    current = START + timedelta(days=random.randint(0, 10))
    while current <= TODAY:
        # real statements aren't perfectly regular -- add a little jitter
        jittered_amount = round(amount + random.uniform(-0.02, 0.02) * amount, 2)
        rows.append((current.isoformat(), merchant, jittered_amount))
        current += timedelta(days=interval_days + random.randint(-1, 1))
    return rows


def generate_one_off_charges(n=40):
    rows = []
    for _ in range(n):
        d = START + timedelta(days=random.randint(0, 420))
        merchant = random.choice(ONE_OFF_MERCHANTS)
        amount = round(random.uniform(4, 90), 2)
        rows.append((d.isoformat(), merchant, amount))
    return rows


def main():
    all_rows = []
    for merchant, amount, interval in SUBSCRIPTIONS:
        all_rows.extend(generate_subscription_charges(merchant, amount, interval))
    all_rows.extend(generate_one_off_charges())
    all_rows.sort(key=lambda r: r[0])

    with open("transactions.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "merchant", "amount"])
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} transactions to transactions.csv")


if __name__ == "__main__":
    main()
