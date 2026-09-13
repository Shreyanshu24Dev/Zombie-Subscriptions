"""
STEP 6 (v2): The dashboard, combined-flow version.

Lets a user connect Gmail AND upload a CSV, analyzes both together as one
transaction set, emails them the summary, and shows a prototype of an
auto-cancel action per subscription.

Run it locally with:
    streamlit run streamlit_app.py
"""

import time
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Zombie Subscription Detector", page_icon="🧟", layout="centered")

# ---- Sidebar: which API to talk to, and this dashboard's own URL ----
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
if "gmail_token" not in st.session_state:
    st.session_state.gmail_token = None

query_params = st.query_params
if "gmail_token" in query_params:
    st.session_state.gmail_token = query_params["gmail_token"]
    st.query_params.clear()  # tidy the URL so a refresh doesn't resubmit it
if "gmail_error" in query_params:
    st.session_state.gmail_error = query_params["gmail_error"]
    st.query_params.clear()

st.title("🧟 Zombie Subscription Detector")
st.write(
    "Connect Gmail, upload a CSV of your bank transactions, or both -- "
    "everything gets analyzed together as one combined picture of your subscriptions."
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
    st.caption("Opens Google's own login screen. Only requests read + send access to your own inbox.")

st.divider()

# ---- CSV upload (optional -- works with or without Gmail connected) ----
uploaded_file = st.file_uploader("Upload transactions.csv (optional)", type="csv")

analyze_clicked = st.button(
    "🔍 Analyze my subscriptions",
    disabled=not (uploaded_file or st.session_state.gmail_token),
    type="primary",
)
if not (uploaded_file or st.session_state.gmail_token):
    st.caption("Connect Gmail and/or upload a CSV above before analyzing.")

# ---- Run combined detection ----
if analyze_clicked:
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
            st.session_state.result = response.json()
        except requests.exceptions.ConnectionError:
            st.error(f"Couldn't reach the API at {api_url}. Check the URL in the sidebar.")
            st.stop()
        except requests.exceptions.RequestException as e:
            st.error(f"The API returned an error: {e}")
            st.stop()

# ---- Show results (persisted in session_state so buttons below don't wipe them on rerun) ----
result = st.session_state.get("result")
if result:
    subs = result["subscriptions"]

    if not subs:
        st.info("No recurring subscriptions detected.")
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("Subscriptions found", len(subs))
        col2.metric("Est. annual cost", f"${result['total_estimated_annual_cost']:,.2f}")
        col3.metric("Transactions scanned", result["transactions_scanned"])

        st.divider()

        chart_df = pd.DataFrame(subs)[["merchant", "estimated_annual_cost"]].set_index("merchant")
        st.bar_chart(chart_df, horizontal=True)

        # --- Email me this summary ---
        if st.session_state.gmail_token:
            if st.button("📤 Email me this summary"):
                with st.spinner("Sending..."):
                    try:
                        r = requests.post(f"{api_url}/gmail/send-summary", json={
                            "gmail_token": st.session_state.gmail_token,
                            "subscriptions": subs,
                            "total_estimated_annual_cost": result["total_estimated_annual_cost"],
                        }, timeout=30)
                        r.raise_for_status()
                        st.success(f"Sent to {r.json()['to']} ✅")
                    except requests.exceptions.RequestException as e:
                        st.error(f"Couldn't send the email: {e}")
        else:
            st.caption("Connect Gmail above to enable emailing yourself this summary.")

        st.divider()

        # --- Detail cards, each with a cancel-prototype action ---
        st.subheader("Details")
        for s in subs:
            merchant_key = s["merchant"].replace(" ", "_")
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(f"**{s['merchant']}**  \n{s['frequency'].capitalize()} · ${s['avg_amount']} per charge")
                c2.metric("Per year", f"${s['estimated_annual_cost']:,.2f}")

                if c3.button("⬇️ Cancel", key=f"cancel_{merchant_key}"):
                    st.session_state[f"cancelling_{merchant_key}"] = True

                if st.session_state.get(f"cancelling_{merchant_key}"):
                    with st.status(f"Attempting to cancel {s['merchant']}...", expanded=True) as status:
                        st.write("Looking up cancellation flow...")
                        time.sleep(0.6)
                        st.write("Checking for known dark patterns...")
                        time.sleep(0.6)
                        status.update(label="Prototype only", state="complete")
                    st.info(
                        f"🚧 **This is a prototype of the auto-cancel feature.** "
                        f"In a full version, this would attempt to cancel {s['merchant']} on your "
                        f"behalf (or hand you a step-by-step script if it can't be automated safely). "
                        f"No real cancellation request was sent."
                    )

                st.caption(
                    f"Last charged {s['days_since_last_charge']} days ago · "
                    f"{s['num_charges_seen']} charges seen · "
                    f"usage signal: {s['usage_signal']}"
                )
