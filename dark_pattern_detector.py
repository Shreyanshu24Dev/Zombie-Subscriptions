"""
STEP 4: The dark-pattern detector.

Scans the text of a cancellation flow (screen-by-screen) and flags
manipulative UX patterns, using the taxonomy researchers and the FTC
commonly use to classify these tactics.

This is intentionally rule-based (keyword/phrase matching) rather than a
trained model -- for an MVP, it's transparent, fast, needs no training
data, and is easy for you to extend as you notice new patterns. If you
want more nuance later, the same categories work as a prompt for an LLM
classifier -- swap detect_flow()'s internals, keep the same output shape.

Run it with:  python dark_pattern_detector.py
"""

import re

# Each category: human-readable description + regex triggers.
# Patterns are intentionally broad/illustrative -- tune them as you
# encounter real examples.
TAXONOMY = {
    "confirmshaming": {
        "description": "Guilt-tripping language aimed at discouraging cancellation.",
        "patterns": [
            r"are you sure you want to (cancel|leave|give up)",
            r"you('| )?ll (lose|miss out on)",
            r"we('| )?ll miss you",
            r"don'?t want to (save money|keep your (benefits|discount))",
        ],
    },
    "forced_continuity": {
        "description": "Trial/subscription auto-renews without a clear, timely warning.",
        "patterns": [
            r"automatically (renew|convert|continue)",
            r"unless you cancel",
            r"your (trial|subscription) will (convert|renew)",
        ],
    },
    "roach_motel": {
        "description": "Cancellation requires extra hoops not required to sign up (calls, extra steps, mail).",
        "patterns": [
            r"call (us|customer service|\d)",
            r"contact (support|us) to cancel",
            r"speak (with|to) a representative",
            r"cancel(lation)? (by|via) (phone|mail)",
        ],
    },
    "hidden_information": {
        "description": "Fees, terms, or consequences buried or vaguely worded.",
        "patterns": [
            r"additional fees may apply",
            r"see terms for details",
            r"non-refundable",
        ],
    },
    "obstruction": {
        "description": "Interrupting the cancel flow with unrelated offers or extra confirmation screens.",
        "patterns": [
            r"before you go",
            r"wait[!,]",
            r"special offer",
            r"here('| )?s (a|an) (deal|discount) (just )?for you",
        ],
    },
}

# A flow with this many steps or more gets flagged for friction, even
# without matching any keyword -- step count itself is a signal.
HIGH_FRICTION_STEP_THRESHOLD = 4


def score_step_text(text: str):
    """Returns the list of categories matched in a single screen's text."""
    matches = []
    for category, info in TAXONOMY.items():
        for pattern in info["patterns"]:
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                matches.append({
                    "category": category,
                    "description": info["description"],
                    "matched_text": found.group(0),
                })
                break  # one match per category per step is enough signal
    return matches


def detect_flow(steps: list[str]):
    """
    steps: ordered list of strings, one per screen in the cancellation flow
    (e.g. steps[0] = text on the first screen you see after clicking "Cancel").
    """
    step_results = []
    all_categories_hit = set()

    for i, text in enumerate(steps, start=1):
        matches = score_step_text(text)
        for m in matches:
            all_categories_hit.add(m["category"])
        step_results.append({"step": i, "text": text, "flags": matches})

    if len(steps) >= HIGH_FRICTION_STEP_THRESHOLD:
        all_categories_hit.add("roach_motel")

    # Simple scoring: each distinct category found adds weight; more
    # categories = more manipulative tactics stacked together.
    risk_score = min(100, len(all_categories_hit) * 22 + max(0, len(steps) - 2) * 5)

    if risk_score >= 60:
        verdict = "high friction -- likely uses multiple dark patterns"
    elif risk_score >= 25:
        verdict = "moderate friction -- some manipulative tactics present"
    else:
        verdict = "low friction -- cancellation flow looks reasonably clean"

    return {
        "num_steps": len(steps),
        "categories_detected": sorted(all_categories_hit),
        "risk_score": risk_score,
        "verdict": verdict,
        "step_details": step_results,
    }


# ---- Demo with two invented example flows (not real company text) ----
if __name__ == "__main__":
    easy_flow = [
        "Cancel your subscription. You'll keep access until the end of your billing period.",
    ]

    hard_flow = [
        "Wait! Before you go, here's a discount just for you -- 50% off your next 3 months.",
        "Are you sure you want to cancel? You'll lose access to all your saved data.",
        "To finish cancelling, please call customer service at the number on your statement.",
        "Your subscription will automatically renew unless you complete all steps above.",
    ]

    for name, flow in [("Easy flow", easy_flow), ("Hard flow", hard_flow)]:
        result = detect_flow(flow)
        print(f"\n=== {name} ===")
        print(f"Steps: {result['num_steps']}  |  Risk score: {result['risk_score']}/100")
        print(f"Verdict: {result['verdict']}")
        print(f"Categories detected: {result['categories_detected']}")
