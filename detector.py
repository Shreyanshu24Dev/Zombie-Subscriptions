"""
STEP 3: The core detector.

Given a CSV of transactions (date, merchant, amount), this finds recurring
charges and flags them as subscriptions -- the foundation of the whole
project. Everything else (API, dashboard, real bank data) wraps around this.

Run it with:  python detector.py
"""

import csv
import re
import statistics
from datetime import date, datetime
from difflib import SequenceMatcher

# How consistent the interval between charges needs to be to count as
# "recurring" -- lower = stricter. 0.2 means the spread of gaps between
# charges can be at most 20% of the average gap.
MAX_INTERVAL_VARIATION = 0.2
MAX_AMOUNT_VARIATION = 0.1
MIN_CHARGES_TO_QUALIFY = 3


def normalize_merchant(raw_name: str) -> str:
    """Strip punctuation, numbers, and boilerplate words so that
    'NETFLIX.COM' and 'NETFLIX  *MEMBER 4471' end up in the same bucket."""
    name = raw_name.upper()
    name = re.sub(r"[^A-Z ]", " ", name)  # drop digits/punctuation
    boilerplate = r"\b(COM|INC|LLC|CO|USA|US|MEMBERSHIP|MEMBER|SUBSCRIBE|TRIP|ORDER|MKTPLACE)\b"
    name = re.sub(boilerplate, "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def cluster_merchants(normalized_names, similarity_threshold=0.82):
    """Groups near-duplicate normalized names together (handles cases
    normalization alone doesn't catch)."""
    clusters = []  # list of (representative_name, [members])
    for name in normalized_names:
        placed = False
        for cluster in clusters:
            if SequenceMatcher(None, name, cluster[0]).ratio() >= similarity_threshold:
                cluster[1].append(name)
                placed = True
                break
        if not placed:
            clusters.append((name, [name]))
    # map every original normalized name -> cluster representative
    mapping = {}
    for rep, members in clusters:
        for m in members:
            mapping[m] = rep
    return mapping


def classify_frequency(mean_interval_days: float) -> str:
    if mean_interval_days <= 10:
        return "weekly"
    if mean_interval_days <= 35:
        return "monthly"
    if mean_interval_days <= 100:
        return "quarterly"
    return "yearly"


def load_transactions(csv_path: str):
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            rows.append({
                "date": datetime.fromisoformat(r["date"]).date(),
                "merchant": r["merchant"],
                "amount": float(r["amount"]),
            })
    return rows


def detect_subscriptions(transactions, today: date = None):
    today = today or date.today()

    # 1. normalize + cluster merchant names
    for t in transactions:
        t["normalized"] = normalize_merchant(t["merchant"])
    mapping = cluster_merchants({t["normalized"] for t in transactions})
    for t in transactions:
        t["group"] = mapping[t["normalized"]]

    # 2. group charges by merchant cluster
    groups = {}
    for t in transactions:
        groups.setdefault(t["group"], []).append(t)

    results = []
    for group_name, charges in groups.items():
        if len(charges) < MIN_CHARGES_TO_QUALIFY:
            continue  # not enough data points to call it recurring

        charges.sort(key=lambda t: t["date"])
        dates = [c["date"] for c in charges]
        amounts = [c["amount"] for c in charges]
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]

        mean_interval = statistics.mean(intervals)
        interval_variation = (statistics.pstdev(intervals) / mean_interval) if mean_interval else 1
        mean_amount = statistics.mean(amounts)
        amount_variation = (statistics.pstdev(amounts) / mean_amount) if mean_amount else 1

        is_recurring = (
            interval_variation <= MAX_INTERVAL_VARIATION
            and amount_variation <= MAX_AMOUNT_VARIATION
        )
        if not is_recurring:
            continue

        days_since_last = (today - dates[-1]).days
        annual_cost = round(mean_amount * (365 / mean_interval), 2)

        results.append({
            "merchant": charges[-1]["merchant"],  # show most recent raw name
            "frequency": classify_frequency(mean_interval),
            "avg_amount": round(mean_amount, 2),
            "num_charges_seen": len(charges),
            "days_since_last_charge": days_since_last,
            "estimated_annual_cost": annual_cost,
            # No usage data yet -- this is a hook for Step 3b. Plug in app
            # opens, logins, or a user "still using this?" survey response
            # here to turn this into a real zombie-risk score.
            "usage_signal": "not connected (defaults to 'review recommended')",
        })

    results.sort(key=lambda r: -r["estimated_annual_cost"])
    return results


def main():
    transactions = load_transactions("transactions.csv")
    subscriptions = detect_subscriptions(transactions)

    total_annual = sum(s["estimated_annual_cost"] for s in subscriptions)

    print(f"Found {len(subscriptions)} recurring subscriptions "
          f"(scanned {len(transactions)} transactions)\n")
    for s in subscriptions:
        print(f"- {s['merchant']:<28} {s['frequency']:<10} "
              f"${s['avg_amount']:<8} ~${s['estimated_annual_cost']}/yr "
              f"(last charged {s['days_since_last_charge']}d ago)")
    print(f"\nEstimated total recurring spend: ${total_annual:,.2f}/year")


if __name__ == "__main__":
    main()
