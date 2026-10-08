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

if "feedback_state" not in st.session_state:
    st.session_state.feedback_state = {}
if "gmail_token" not in st.session_state:
    st.session_state.gmail_token = None


def record_feedback(api_url: str, merchant: str, estimated_annual_cost: float, feedback: str):
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
        col1, col2, col3 = st.columns(3)
        col1.metric("Subscriptions found", len(subs))
        col2.metric("Est. annual cost", f"${result['total_estimated_annual_cost']:,.2f}")
        col3.metric("Transactions scanned", result["transactions_scanned"])

        st.divider()

        chart_df = pd.DataFrame(subs)[["merchant", "estimated_annual_cost"]].set_index("merchant")
        st.bar_chart(chart_df, horizontal=True)

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

        cancelled = [
            s for i, s in enumerate(subs)
            if st.session_state.feedback_state.get(f"fb_{i}_{s['merchant']}") == "cancel"
        ]
        kept = [
            s for i, s in enumerate(subs)
            if st.session_state.feedback_state.get(f"fb_{i}_{s['merchant']}") == "still_using"
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

        if cancelled or kept:
            st.divider()
            if st.session_state.gmail_token:
                if st.button("📧 Send summary to Gmail"):
                    with st.spinner("Sending..."):
                        try:
                            r = requests.post(f"{api_url}/gmail/notify-batch", json={
                                "gmail_token": st.session_state.gmail_token,
                                "cancelled": [
                                    {"merchant": s["merchant"], "estimated_annual_cost": s["estimated_annual_cost"]}
                                    for s in cancelled
                                ],
                                "kept": [
                                    {"merchant": s["merchant"], "estimated_annual_cost": s["estimated_annual_cost"]}
                                    for s in kept
                                ],
                            }, timeout=30)
                            r.raise_for_status()
                            st.success(f"Sent one summary email to {r.json()['to']} ✅")
                        except requests.exceptions.RequestException as e:
                            st.error(f"Couldn't send the email: {e}")
            else:
                st.caption("Connect Gmail above to email yourself this summary.")
else:
    st.caption("No file uploaded yet. Try the `transactions.csv` from Step 2 to see it in action.")


# ============================================================================
# Multi-month detection (optional, separate from the flow above)
#
# A single month's CSV has every merchant appearing once, so there's nothing
# to compare against -- recurrence can't be confirmed from one data point.
# This section lets you upload a second month to fix that. The second file
# is entirely optional; uploading just the first still works.
# ============================================================================
st.divider()
with st.expander("📊 Multi-month analysis (more accurate detection)"):
    st.write(
        "A single month's data can't confirm a subscription is recurring -- "
        "every merchant only appears once. Upload a second CSV from a "
        "different month and this will cross-check both together."
    )

    mm_col1, mm_col2 = st.columns(2)
    mm_file1 = mm_col1.file_uploader("First month (required)", type="csv", key="mm_file1")
    mm_file2 = mm_col2.file_uploader("Second month (optional)", type="csv", key="mm_file2")

    if mm_file1 is not None:
        if st.button("🔍 Analyze across months"):
            with st.spinner("Comparing months..."):
                try:
                    files = {"file": (mm_file1.name, mm_file1.getvalue(), "text/csv")}
                    if mm_file2 is not None:
                        files["file2"] = (mm_file2.name, mm_file2.getvalue(), "text/csv")

                    mm_response = requests.post(f"{api_url}/detect/multi-month", files=files, timeout=60)
                    mm_response.raise_for_status()
                    st.session_state.mm_result = mm_response.json()
                except requests.exceptions.ConnectionError:
                    st.error(f"Couldn't reach the API at {api_url}.")
                    st.stop()
                except requests.exceptions.RequestException as e:
                    st.error(f"The API returned an error: {e}")
                    st.stop()

        mm_result = st.session_state.get("mm_result")
        if mm_result:
            if mm_result.get("warning"):
                st.warning(mm_result["warning"])

            subs_found = mm_result["merchants"]
            hidden = mm_result.get("other_merchants_not_flagged", 0)

            st.caption(
                f"Scanned {mm_result['transactions_scanned']} transactions across both months."
            )

            if not subs_found:
                st.info(
                    "No recurring subscriptions found. A subscription needs to appear in "
                    "both months at a similar amount, roughly 30 days apart."
                )
            else:
                m_col1, m_col2 = st.columns(2)
                m_col1.metric("Subscriptions found", len(subs_found))
                m_col2.metric(
                    "Est. monthly total",
                    f"${sum(m['avg_amount'] for m in subs_found):,.2f}",
                )

                for m in subs_found:
                    with st.container(border=True):
                        c1, c2 = st.columns([3, 1])
                        c1.markdown(f"**{m['merchant']}**")
                        c1.caption(
                            f"✅ Seen {m['charge_count']} times, "
                            f"~{m['avg_interval_days']} days apart"
                        )
                        c2.metric("Per month", f"${m['avg_amount']:,.2f}")

            if hidden:
                st.caption(
                    f"{hidden} other merchant(s) were left out because they aren't "
                    f"recurring subscriptions (one-off purchases, or not on a monthly cycle)."
                )
    else:
        st.caption("Upload at least the first month's CSV to run this analysis.")
