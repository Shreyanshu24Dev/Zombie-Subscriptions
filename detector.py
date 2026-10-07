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
    boilerplate = (
        r"\b(COM|INC|LLC|CO|USA|US|MEMBERSHIP|MEMBER|SUBSCRIBE|TRIP|ORDER|"
        r"MKTPLACE|BILL|BILLING|PAY|PAYMENT|RECUR|RECURRING)\b"
    )
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


class CSVValidationError(Exception):
    """Raised when an uploaded CSV is missing a required column."""
    pass


REQUIRED_TRANSACTION_COLUMNS = {"date", "merchant", "amount"}

# Multi-month detection has its own, separate tolerances from the
# single-file detector above -- with only 2 data points (one charge per
# month), there's no meaningful "variance" to measure the way the
# single-file path does, so instead we check amounts and intervals
# against fixed, explicit tolerances.
MULTI_MONTH_MIN_CHARGES = 2
MULTI_MONTH_AMOUNT_TOLERANCE_ABS = 2.0     # dollars
MULTI_MONTH_AMOUNT_TOLERANCE_PCT = 0.05    # or 5%, whichever is more forgiving
MULTI_MONTH_INTERVAL_MIN_DAYS = 27
MULTI_MONTH_INTERVAL_MAX_DAYS = 33
# An interval shorter than this means "twice in the same month," which is
# a different signal than monthly recurrence (could be a split charge, a
# refund+rebill, or coincidence) -- it should never count as confirming
# monthly billing, even if the amounts match.
SAME_MONTH_MAX_GAP_DAYS = 20


def validate_transaction_columns(rows: list, label: str = "file") -> None:
    """Raises CSVValidationError if the required columns aren't present.
    Called before any merge/detection logic touches the data."""
    if not rows:
        raise CSVValidationError(f"{label} has no rows to read")
    missing = REQUIRED_TRANSACTION_COLUMNS - set(rows[0].keys())
    if missing:
        raise CSVValidationError(
            f"{label} is missing required column(s): {', '.join(sorted(missing))}"
        )


def _dedup_key(row: dict) -> tuple:
    return (str(row["date"]), str(row["merchant"]), str(row["amount"]))


def merge_transaction_files(file1_rows: list, file2_rows: list = None) -> dict:
    """
    Merges two CSVs' worth of transaction rows (each a dict with at least
    date/merchant/amount keys) into one combined, deduplicated, sorted list.

    file2_rows is optional -- passing None (or an empty list) makes this a
    simple passthrough of file1_rows, so a caller can always go through this
    function regardless of whether a second file was actually uploaded.

    Returns {"transactions": [...], "warning": str | None}. The warning is
    set (non-fatal) when both files cover the exact same date range, since
    that adds no new signal for recurrence detection.
    """
    validate_transaction_columns(file1_rows, "first file")

    if not file2_rows:
        merged = sorted(file1_rows, key=lambda r: r["date"])
        return {"transactions": merged, "warning": None}

    validate_transaction_columns(file2_rows, "second file")

    dates1 = [r["date"] for r in file1_rows]
    dates2 = [r["date"] for r in file2_rows]
    range1 = (min(dates1), max(dates1))
    range2 = (min(dates2), max(dates2))

    warning = None
    if range1 == range2:
        warning = (
            f"Both files cover the exact same date range ({range1[0]} to "
            f"{range1[1]}) -- the second file doesn't add any new months, "
            f"so it won't improve detection."
        )

    seen = set()
    merged = []
    for row in file1_rows + file2_rows:
        key = _dedup_key(row)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)

    merged.sort(key=lambda r: r["date"])
    return {"transactions": merged, "warning": warning}


def detect_recurring_multi_month(transactions: list) -> list:
    """
    Runs recurrence detection across a (possibly multi-month) transaction
    list. Unlike detect_subscriptions() above, this only requires 2+
    charges (since two monthly files naturally produce exactly 2 charges
    per subscription) and checks amount/interval against fixed tolerances
    rather than relative statistical variance.

    Returns one entry per merchant group, always -- including merchants
    seen only once (confidence "insufficient_data") so the caller can show
    the full picture, not just confirmed hits.
    """
    parsed = []
    for t in transactions:
        raw_date = t["date"]
        parsed_date = (
            datetime.fromisoformat(str(raw_date)).date()
            if not isinstance(raw_date, date)
            else raw_date
        )
        parsed.append({
            "date": parsed_date,
            "merchant": t["merchant"],
            "amount": float(t["amount"]),
        })

    for t in parsed:
        t["normalized"] = normalize_merchant(t["merchant"])
    mapping = cluster_merchants({t["normalized"] for t in parsed})
    for t in parsed:
        t["group"] = mapping[t["normalized"]]

    groups = {}
    for t in parsed:
        groups.setdefault(t["group"], []).append(t)

    results = []
    for group_name, charges in groups.items():
        charges.sort(key=lambda c: c["date"])
        count = len(charges)
        avg_amount = round(statistics.mean(c["amount"] for c in charges), 2)
        display_name = charges[-1]["merchant"]

        if count < MULTI_MONTH_MIN_CHARGES:
            results.append({
                "merchant": display_name,
                "normalized_merchant": group_name,
                "charge_count": count,
                "avg_amount": avg_amount,
                "avg_interval_days": None,
                "confidence": "insufficient_data",
            })
            continue

        intervals = [(charges[i + 1]["date"] - charges[i]["date"]).days for i in range(count - 1)]
        avg_interval = round(statistics.mean(intervals), 1)

        amounts = [c["amount"] for c in charges]
        amount_spread = max(amounts) - min(amounts)
        amounts_consistent = (
            amount_spread <= MULTI_MONTH_AMOUNT_TOLERANCE_ABS
            or amount_spread <= MULTI_MONTH_AMOUNT_TOLERANCE_PCT * avg_amount
        )

        has_monthly_gap = any(
            MULTI_MONTH_INTERVAL_MIN_DAYS <= iv <= MULTI_MONTH_INTERVAL_MAX_DAYS
            for iv in intervals
        )
        has_same_month_gap = any(iv < SAME_MONTH_MAX_GAP_DAYS for iv in intervals)

        if amounts_consistent and has_monthly_gap:
            confidence = "confirmed_recurring"
        elif has_same_month_gap and not has_monthly_gap:
            confidence = "same_month_duplicate"
        else:
            confidence = "insufficient_data"

        results.append({
            "merchant": display_name,
            "normalized_merchant": group_name,
            "charge_count": count,
            "avg_amount": avg_amount,
            "avg_interval_days": avg_interval,
            "confidence": confidence,
        })

    # confirmed first, then by how many charges back each one up
    results.sort(key=lambda r: (r["confidence"] != "confirmed_recurring", -r["charge_count"]))
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
