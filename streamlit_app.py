"""
STEP 6: The dashboard.

A visual frontend that calls your live API (from Step 5) and shows the
results in a way a non-technical person could actually use -- no /docs
page, no JSON.

Run it locally with:
    streamlit run streamlit_app.py

By default it talks to your API running on your own machine
(http://127.0.0.1:8000). Once your API is deployed on Render, paste that
URL into the sidebar instead -- the dashboard doesn't care where the API
lives, it just calls whatever URL you give it.
"""

import pandas as pd
import requests
import streamlit as st
from urllib.parse import quote

st.set_page_config(page_title="Zombie Subscription Detector", page_icon="🧟", layout="centered")

# Tracks the user's "Still using this?" answer per merchant across reruns
# (Streamlit reruns the whole script on every button click, so without
# this the buttons would forget what was just clicked).
if "feedback_state" not in st.session_state:
    st.session_state.feedback_state = {}  # merchant -> "still_using" | "cancel"
if "gmail_token" not in st.session_state:
    st.session_state.gmail_token = None


def record_feedback(api_url: str, merchant: str, estimated_annual_cost: float, feedback: str):
    """Best-effort: log the answer to the API/feedback.csv. If the deployed
    API predates this endpoint, or the network hiccups, we still keep the
    answer locally in session_state so the UI doesn't break."""
    try:
        requests.post(
            f"{api_url}/feedback",
            json={
                "merchant": merchant,
                "estimated_annual_cost": estimated_annual_cost,
                "feedback": feedback,
            },
            timeout=10,
        )
    except requests.exceptions.RequestException:
        pass

# ---- Sidebar: which API to talk to ----
st.sidebar.header("Settings")
api_url = st.sidebar.text_input(
    "API base URL",
    value="https://zombie-subscriptions.onrender.com",
    help="This is your deployed API. Only change this if testing locally (http://127.0.0.1:8000).",
).strip().rstrip("/")
if api_url and not api_url.startswith(("http://", "https://")):
    api_url = f"https://{api_url}"
dashboard_url = st.sidebar.text_input(
    "This dashboard's own URL",
    value="https://zombie-subscriptions-2.onrender.com",
    help="Needed so Google knows where to redirect you back to after connecting Gmail.",
).strip().rstrip("/")

# ---- Pick up the Gmail token Google redirected back with, if any ----
query_params = st.query_params
if "gmail_token" in query_params:
    st.session_state.gmail_token = query_params["gmail_token"]
    st.query_params.clear()
if "gmail_error" in query_params:
    st.session_state.gmail_error = query_params["gmail_error"]
    st.query_params.clear()

st.title("🧟 Zombie Subscription Detector")
st.write(
    "Find your recurring subscriptions two ways: upload a CSV of your bank "
    "transactions, or connect Gmail to scan receipt/subscription emails. "
    "Both feed into the same results below."
)

# ---- Gmail connection status ----
if st.session_state.gmail_token:
    col1, col2 = st.columns([4, 1])
    col1.success("✅ Gmail connected")
    if col2.button("Disconnect"):
        st.session_state.gmail_token = None
        st.rerun()
else:
    if st.session_state.get("gmail_error"):
        st.warning(f"Gmail connection didn't complete: {st.session_state.gmail_error}")
    authorize_url = f"{api_url}/gmail/authorize?return_to={quote(dashboard_url)}"
    st.link_button("📧 Connect Gmail", authorize_url)
    st.caption(
        "Opens Google's own login screen and only requests **read-only** email access -- "
        "nothing can be sent, deleted, or changed. You'll be brought right back here."
    )

st.divider()

uploaded_file = st.file_uploader("Upload transactions.csv (optional)", type="csv")

if uploaded_file is not None or st.session_state.gmail_token:
    with st.spinner("Scanning transactions..."):
        try:
            files = {}
            if uploaded_file is not None:
                files["file"] = (uploaded_file.name, uploaded_file.getvalue(), "text/csv")
            data = {}
            if st.session_state.gmail_token:
                data["gmail_token"] = st.session_state.gmail_token

            response = requests.post(f"{api_url}/detect/combined", files=files, data=data, timeout=60)
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.ConnectionError:
            st.error(
                f"Couldn't reach the API at {api_url}. "
                "Is it running locally (`uvicorn api:app --reload`), or is the URL in the "
                "sidebar wrong if you meant to use the deployed version?"
            )
            st.stop()
        except requests.exceptions.RequestException as e:
            st.error(f"The API returned an error: {e}")
            st.stop()

    subs = result["subscriptions"]

    if not subs:
        st.info("No recurring subscriptions detected in this file.")
    else:
        # --- Top-line numbers ---
        col1, col2, col3 = st.columns(3)
        col1.metric("Subscriptions found", len(subs))
        col2.metric("Est. annual cost", f"${result['total_estimated_annual_cost']:,.2f}")
        col3.metric("Transactions scanned", result["transactions_scanned"])

        st.divider()

        # --- Chart ---
        chart_df = pd.DataFrame(subs)[["merchant", "estimated_annual_cost"]].set_index("merchant")
        st.bar_chart(chart_df, horizontal=True)

        # --- Detail cards, each with a "still using it?" check-in ---
        st.subheader("Details")
        for i, s in enumerate(subs):
            merchant = s["merchant"]
            state_key = f"fb_{i}_{merchant}"

            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                c1.markdown(f"**{merchant}**  \n{s['frequency'].capitalize()} · ${s['avg_amount']} per charge")
                c2.metric("Per year", f"${s['estimated_annual_cost']:,.2f}")
                st.caption(
                    f"Last charged {s['days_since_last_charge']} days ago · "
                    f"{s['num_charges_seen']} charges seen · "
                    f"usage signal: {s['usage_signal']}"
                )

                answer = st.session_state.feedback_state.get(state_key)

                st.write("**Still using this?**")
                yes_col, no_col = st.columns(2)
                if yes_col.button("✅ Yes, keep it", key=f"yes_{state_key}", use_container_width=True):
                    st.session_state.feedback_state[state_key] = "still_using"
                    record_feedback(api_url, merchant, s["estimated_annual_cost"], "still_using")
                    st.rerun()
                if no_col.button("❌ No, cancel it", key=f"no_{state_key}", use_container_width=True):
                    st.session_state.feedback_state[state_key] = "cancel"
                    record_feedback(api_url, merchant, s["estimated_annual_cost"], "cancel")
                    if st.session_state.gmail_token:
                        try:
                            requests.post(f"{api_url}/gmail/notify-cancellation", json={
                                "gmail_token": st.session_state.gmail_token,
                                "merchant": merchant,
                                "estimated_annual_cost": s["estimated_annual_cost"],
                            }, timeout=15)
                        except requests.exceptions.RequestException:
                            pass  # feedback is still recorded even if the email fails
                    st.rerun()

                if answer == "still_using":
                    st.success(f"Good to know — {merchant} stays active. We'll keep tracking it.")

                elif answer == "cancel":
                    st.warning(
                        f"🔌 Autopay for **{merchant}** is marked to be turned off. "
                        "This app can't cancel a real charge for you (it has no write access to "
                        "your bank or the merchant) — treat this as a to-do: log in to "
                        f"{merchant.split()[0].title()} or your card issuer and cancel the recurring "
                        "payment there."
                    )
                    monthly = s["avg_amount"] if s["frequency"] == "monthly" else s["estimated_annual_cost"] / 12
                    annual = s["estimated_annual_cost"]
                    compare_df = pd.DataFrame(
                        {
                            "Keep it": [monthly, annual, annual * 3],
                            "Cancel it": [0.0, 0.0, 0.0],
                        },
                        index=["Per month", "Per year", "Over 3 years"],
                    )
                    st.table(compare_df.style.format("${:,.2f}"))

        # --- Running savings summary across every "cancel" answer so far ---
        cancelled = [
            s for i, s in enumerate(subs)
            if st.session_state.feedback_state.get(f"fb_{i}_{s['merchant']}") == "cancel"
        ]
        if cancelled:
            st.divider()
            st.subheader("💰 Your potential savings")
            current_total = result["total_estimated_annual_cost"]
            saved = sum(s["estimated_annual_cost"] for s in cancelled)
            projected_total = current_total - saved

            summary_df = pd.DataFrame(
                {
                    "Before": [current_total],
                    "After cancelling flagged subs": [projected_total],
                    "Annual savings": [saved],
                },
                index=["Estimated annual cost"],
            )
            st.table(summary_df.style.format("${:,.2f}"))
            st.caption(
                f"Marked for cancellation: {', '.join(s['merchant'] for s in cancelled)}."
            )
else:
    st.caption("No file uploaded yet. Try the `transactions.csv` from Step 2 to see it in action.")