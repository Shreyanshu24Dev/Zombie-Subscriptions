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

st.set_page_config(page_title="Zombie Subscription Detector", page_icon="🧟", layout="centered")

# ---- Sidebar: which API to talk to ----
st.sidebar.header("Settings")
api_url = st.sidebar.text_input(
    "API base URL",
    value="http://127.0.0.1:8000",
    help="Running locally? Leave as-is. Deployed on Render? Paste that URL here instead.",
).rstrip("/")

st.title("🧟 Zombie Subscription Detector")
st.write(
    "Find your recurring subscriptions two ways: upload a CSV of your bank "
    "transactions, or connect Gmail to scan receipt/subscription emails."
)

tab_csv, tab_gmail = st.tabs(["📄 Upload CSV", "📧 Connect Gmail"])

with tab_gmail:
    st.write(
        "This opens Google's own login screen in a new tab and only requests "
        "**read-only** email access -- nothing can be sent, deleted, or changed."
    )
    st.link_button("Connect Gmail", f"{api_url}/gmail/authorize")
    st.caption(
        "Results open on a page served by the API itself once you approve access, "
        "since that keeps things simple and avoids storing your data anywhere."
    )

with tab_csv:
    uploaded_file = st.file_uploader("Upload transactions.csv", type="csv")

    if uploaded_file is not None:
        with st.spinner("Scanning transactions..."):
            try:
                files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "text/csv")}
                response = requests.post(f"{api_url}/detect/upload-csv", files=files, timeout=60)
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

            # --- Detail cards ---
            st.subheader("Details")
            for s in subs:
                with st.container(border=True):
                    c1, c2 = st.columns([3, 1])
                    c1.markdown(f"**{s['merchant']}**  \n{s['frequency'].capitalize()} · ${s['avg_amount']} per charge")
                    c2.metric("Per year", f"${s['estimated_annual_cost']:,.2f}")
                    st.caption(
                        f"Last charged {s['days_since_last_charge']} days ago · "
                        f"{s['num_charges_seen']} charges seen · "
                        f"usage signal: {s['usage_signal']}"
                    )
    else:
        st.caption("No file uploaded yet. Try the `transactions.csv` from Step 2 to see it in action.")
