"""StmtForge - Interactive Streamlit Dashboard."""

import io
import os
import sys
import tempfile
from pathlib import Path

import pikepdf
import pdfplumber
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
from uuid import uuid4

from stmtforge.database.db import Database
from stmtforge.hybrid_pipeline import HybridPipeline
from stmtforge.parsers.registry import list_available_parsers
from stmtforge.utils.config import load_config, get_all_passwords
from stmtforge.utils.hashing import file_hash as compute_file_hash
from stmtforge.utils.privacy_logging import PrivacyEventLogger, pseudonymize_value

# ── Page config ──────────────────────────────────────────────────
st.set_page_config(
    page_title="StmtForge — Credit Card Analytics",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Design System ────────────────────────────────────────────────
# Cohesive palette: deep navy base, teal/emerald accents, warm amber highlights
COLORS = {
    "primary": "#0f172a",      # slate-900
    "secondary": "#1e293b",    # slate-800
    "accent": "#0ea5e9",       # sky-500
    "accent2": "#10b981",      # emerald-500
    "accent3": "#f59e0b",      # amber-500
    "accent4": "#8b5cf6",      # violet-500
    "accent5": "#ec4899",      # pink-500
    "text": "#f8fafc",         # slate-50
    "text_muted": "#94a3b8",   # slate-400
    "surface": "#1e293b",      # slate-800
    "border": "#334155",       # slate-700
    "success": "#10b981",
    "warning": "#f59e0b",
    "error": "#ef4444",
}

# Professional chart color sequence
CHART_COLORS = [
    "#0ea5e9", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899",
    "#06b6d4", "#84cc16", "#f97316", "#6366f1", "#14b8a6",
    "#e879f9", "#22d3ee", "#a3e635", "#fb923c", "#818cf8",
]

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Inter, -apple-system, system-ui, sans-serif", color="#e2e8f0", size=12),
    margin=dict(l=0, r=0, t=10, b=0),
    xaxis=dict(gridcolor="rgba(148,163,184,0.1)", zerolinecolor="rgba(148,163,184,0.1)"),
    yaxis=dict(gridcolor="rgba(148,163,184,0.1)", zerolinecolor="rgba(148,163,184,0.1)"),
    hoverlabel=dict(bgcolor="#1e293b", font_size=13, font_family="Inter, sans-serif", bordercolor="#334155"),
)

# ── Custom CSS ───────────────────────────────────────────────────
st.markdown("""
<style>
    /* ── Global ─────────────────────────────────── */
    .stApp { background-color: #0f172a; }
    .stApp header { background-color: #0f172a !important; }

    /* ── Sidebar ────────────────────────────────── */
    section[data-testid="stSidebar"] {
        background-color: #1e293b;
        border-right: 1px solid #334155;
    }
    section[data-testid="stSidebar"] .stMarkdown h1,
    section[data-testid="stSidebar"] .stMarkdown h2,
    section[data-testid="stSidebar"] .stMarkdown h3 {
        color: #f1f5f9;
        font-weight: 600;
        letter-spacing: -0.01em;
    }
    section[data-testid="stSidebar"] hr {
        border-color: #334155;
    }

    /* ── Metric cards ───────────────────────────── */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 16px 20px;
        transition: border-color 0.2s;
    }
    div[data-testid="stMetric"]:hover {
        border-color: #0ea5e9;
    }
    div[data-testid="stMetric"] label {
        color: #94a3b8 !important;
        font-size: 0.75rem !important;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        font-weight: 500 !important;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: #f1f5f9 !important;
        font-weight: 700 !important;
        font-size: 1.5rem !important;
    }

    /* ── Cards / containers ─────────────────────── */
    .chart-container {
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 24px;
        margin-bottom: 16px;
    }
    .chart-title {
        color: #f1f5f9;
        font-size: 1rem;
        font-weight: 600;
        margin-bottom: 16px;
        display: flex;
        align-items: center;
        gap: 8px;
    }

    /* ── Section headers ────────────────────────── */
    .stMarkdown h3, .stMarkdown h2 {
        color: #f1f5f9 !important;
        font-weight: 600 !important;
        letter-spacing: -0.01em;
    }
    .stMarkdown hr {
        border-color: #334155 !important;
    }

    /* ── Tabs ───────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        background-color: #1e293b;
        border-radius: 10px;
        padding: 4px;
        border: 1px solid #334155;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        color: #94a3b8;
        font-weight: 500;
        padding: 8px 20px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #0ea5e9 !important;
        color: #ffffff !important;
    }
    .stTabs [data-baseweb="tab-highlight"] {
        display: none;
    }
    .stTabs [data-baseweb="tab-border"] {
        display: none;
    }

    /* ── Dataframe ──────────────────────────────── */
    .stDataFrame {
        border: 1px solid #334155;
        border-radius: 8px;
    }

    /* ── Buttons ────────────────────────────────── */
    .stDownloadButton > button {
        background: linear-gradient(135deg, #0ea5e9, #0284c7);
        color: white;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        padding: 10px 24px;
        transition: opacity 0.2s;
    }
    .stDownloadButton > button:hover {
        opacity: 0.9;
    }

    /* ── Select / inputs ────────────────────────── */
    .stSelectbox > div > div,
    .stMultiSelect > div > div,
    .stNumberInput > div > div > input,
    .stTextInput > div > div > input {
        background-color: #0f172a;
        border-color: #334155;
        color: #e2e8f0;
    }

    /* ── Multiselect tags ───────────────────────── */
    span[data-baseweb="tag"] {
        background-color: #0ea5e9 !important;
        border-radius: 6px !important;
    }

    /* ── Hide default footer ────────────────────── */
    footer { visibility: hidden; }

    /* ── Info/warning/success boxes ──────────────── */
    .stAlert { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_db():
    return Database()


@st.cache_resource
def get_client_event_logger():
    return PrivacyEventLogger(channel="client")


@st.cache_data(ttl=300)
def load_transactions(_db, filters_key):
    """Load transactions with caching.

    filters_key is a tuple-of-2-tuples (hashable); multi-value entries are
    stored as tuples and must be converted back to lists for get_transactions.
    """
    filters = {k: list(v) if isinstance(v, tuple) else v for k, v in filters_key}
    return _db.get_transactions(filters)


def format_inr(amount):
    """Format amount in INR."""
    if amount >= 10_000_000:
        return f"₹{amount/10_000_000:.2f} Cr"
    elif amount >= 100_000:
        return f"₹{amount/100_000:.2f} L"
    elif amount >= 1000:
        return f"₹{amount/1000:.1f}K"
    return f"₹{amount:,.2f}"


def main():
    db = get_db()
    client_logger = get_client_event_logger()

    if "client_session_id" not in st.session_state:
        st.session_state["client_session_id"] = uuid4().hex

    session_hash = pseudonymize_value(st.session_state["client_session_id"])

    if not st.session_state.get("client_page_view_logged"):
        client_logger.log_event(
            "dashboard_page_loaded",
            {
                "session_id": st.session_state["client_session_id"],
                "session_hash": session_hash,
            },
            source="dashboard",
        )
        st.session_state["client_page_view_logged"] = True

    # ── Sidebar ──────────────────────────────────────────────────
    _logo_svg = (
        '<svg width="48" height="48" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">'
        '<rect width="200" height="200" rx="40" fill="#0B1220"/>'
        '<rect x="50" y="60" width="100" height="10" rx="5" fill="#22D3EE"/>'
        '<rect x="50" y="85" width="80" height="10" rx="5" fill="#22D3EE" opacity="0.7"/>'
        '<rect x="50" y="110" width="90" height="10" rx="5" fill="#22D3EE" opacity="0.5"/>'
        '<rect x="80" y="130" width="40" height="30" rx="6" fill="#6366F1"/>'
        '<path d="M90 130 V115 A10 10 0 0 1 110 115 V130" fill="none" stroke="#6366F1" stroke-width="4"/>'
        '</svg>'
    )
    st.sidebar.markdown(f"""
    <div style="text-align:center; padding: 8px 0 4px 0;">
        {_logo_svg}
        <div style="margin-top:6px;">
            <span style="font-size:1.6rem; font-weight:700; color:#f1f5f9; letter-spacing:-0.02em;">
                StmtForge
            </span>
        </div>
        <span style="font-size:0.7rem; color:#64748b; letter-spacing:0.05em; text-transform:uppercase;">
            Credit Card Analytics
        </span>
    </div>
    """, unsafe_allow_html=True)
    st.sidebar.markdown("---")

    summary = db.get_summary()

    # ── Always show tabs (Parse PDF works even with empty DB) ────
    st.markdown("""
    <h1 style="font-size:1.8rem; font-weight:700; color:#f1f5f9; margin-bottom:4px; letter-spacing:-0.02em;">
        Credit Card Analytics
    </h1>
    <p style="color:#64748b; font-size:0.85rem; margin-bottom:20px;">
        Analyze spend across all your banks and cards
    </p>
    """, unsafe_allow_html=True)
    tab_analytics, tab_statements, tab_stmt_analysis, tab_parse = st.tabs(
        ["Analytics", "Statements", "Monthly Analysis", "Parse PDF"]
    )

    with tab_parse:
        _render_parse_pdf(db)

    # No-data state: analytics/statements show a prompt
    if summary["total_transactions"] == 0:
        with tab_analytics:
            st.warning(
                "No transactions found. Run the pipeline first:\n\n"
                "```\npython run_pipeline.py\n```"
            )
            st.info(
                "**Setup steps:**\n"
                "1. Place your `credentials.json` from Google Cloud Console in the project root\n"
                "2. Fill in `.env` with your DOB and PAN\n"
                "3. Run `python run_pipeline.py`\n"
                "4. Refresh this dashboard — or use **Parse PDF** above to import a statement now"
            )
        with tab_stmt_analysis:
            st.info("No statements found yet. Run the pipeline or use Parse PDF.")
        return

    if hasattr(db, "get_date_anchor_options"):
        date_anchors = db.get_date_anchor_options()
    else:
        fallback_end = summary["date_range"].get("end") if summary.get("date_range") else None
        fallback_start = summary["date_range"].get("start") if summary.get("date_range") else None
        date_anchors = {
            "latest_statement_end_date": None,
            "latest_statement_received_date": fallback_end,
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "transaction_min_date": fallback_start,
        }
    banks = db.get_banks()
    categories = db.get_categories()
    cards = db.get_cards()
    card_names = db.get_card_names()

    # Date range
    st.sidebar.subheader("📅 Date Range")
    date_min = date_anchors["transaction_min_date"] or summary["date_range"]["start"] or "2024-06-01"

    date_to_options = {
        "Latest statement end date": (
            date_anchors["latest_statement_end_date"]
            or date_anchors["latest_statement_received_date"]
            or date_anchors["current_date"]
        ),
        "Latest statement received date": (
            date_anchors["latest_statement_received_date"]
            or date_anchors["current_date"]
        ),
        "Current date": date_anchors["current_date"],
    }
    selected_to_anchor = st.sidebar.selectbox(
        "To date basis",
        options=list(date_to_options.keys()),
        index=0,
    )
    date_max = date_to_options[selected_to_anchor]

    # Default "from" to 2 years before the selected end date so we don't
    # show decade-old rows caused by bad date parsing.
    date_max_dt = datetime.strptime(date_max, "%Y-%m-%d")
    date_default_from = max(
        date_max_dt - timedelta(days=730),
        datetime.strptime(date_min, "%Y-%m-%d"),
    )

    col1, col2 = st.sidebar.columns(2)
    date_from = col1.date_input(
        "From",
        value=date_default_from,
        min_value=datetime.strptime(date_min, "%Y-%m-%d"),
        max_value=date_max_dt,
    )
    date_to = col2.date_input(
        "To",
        value=date_max_dt,
        min_value=datetime.strptime(date_min, "%Y-%m-%d"),
        max_value=date_max_dt,
    )

    # Bank filter
    st.sidebar.subheader("🏦 Bank")
    selected_banks = st.sidebar.multiselect("Select banks", banks, default=banks)

    # Card name filter
    if card_names:
        st.sidebar.subheader("💳 Card")
        selected_card_names = st.sidebar.multiselect("Select cards", card_names, default=card_names)
    else:
        selected_card_names = []

    # Category filter
    st.sidebar.subheader("🏷️ Category")
    selected_categories = st.sidebar.multiselect("Select categories", categories, default=categories)

    # Amount range
    st.sidebar.subheader("💰 Amount Range")
    amount_col1, amount_col2 = st.sidebar.columns(2)
    amount_min = amount_col1.number_input("Min", min_value=0.0, value=0.0, step=100.0)
    amount_max = amount_col2.number_input("Max", min_value=0.0, value=0.0, step=100.0)

    # Search
    st.sidebar.subheader("🔍 Search")
    search_query = st.sidebar.text_input("Search merchant/description")

    # Card last-4 filter
    if cards:
        st.sidebar.subheader("🔢 Card Last 4")
        selected_card = st.sidebar.selectbox("Card (last 4 digits)", ["All"] + cards)
    else:
        selected_card = "All"

    # Transaction type
    st.sidebar.subheader("📊 Type")
    txn_type = st.sidebar.radio("Transaction type", ["All", "Debit", "Credit"], horizontal=True)

    # Build filters
    filters = {
        "date_from": date_from.strftime("%Y-%m-%d"),
        "date_to": date_to.strftime("%Y-%m-%d"),
    }
    if selected_banks and len(selected_banks) < len(banks):
        filters["bank"] = selected_banks
    if selected_card_names and len(selected_card_names) < len(card_names):
        filters["card_name"] = selected_card_names
    if selected_categories and len(selected_categories) < len(categories):
        filters["category"] = selected_categories
    if amount_min > 0:
        filters["amount_min"] = amount_min
    if amount_max > 0:
        filters["amount_max"] = amount_max
    if search_query:
        filters["search"] = search_query
    if selected_card != "All":
        filters["card_last4"] = selected_card
    if txn_type != "All":
        filters["type"] = txn_type.lower()

    # Load data
    # Convert any list values to tuples so the cache key is fully hashable.
    hashable_filters = {
        k: tuple(v) if isinstance(v, list) else v
        for k, v in filters.items()
    }
    df = load_transactions(db, tuple(sorted(hashable_filters.items())))

    current_filter_signature = str({
        "date_from": filters.get("date_from"),
        "date_to": filters.get("date_to"),
        "to_basis": selected_to_anchor,
        "banks_count": len(selected_banks or []),
        "cards_count": len(selected_card_names or []),
        "categories_count": len(selected_categories or []),
        "has_search": bool(search_query),
        "search_len": len(search_query or ""),
        "txn_type": txn_type,
    })

    if st.session_state.get("last_filter_signature") != current_filter_signature:
        st.session_state["last_filter_signature"] = current_filter_signature
        client_logger.log_event(
            "dashboard_filters_applied",
            {
                "session_id": st.session_state["client_session_id"],
                "session_hash": session_hash,
                "to_date_basis": selected_to_anchor,
                "date_from": filters.get("date_from"),
                "date_to": filters.get("date_to"),
                "banks_count": len(selected_banks or []),
                "cards_count": len(selected_card_names or []),
                "categories_count": len(selected_categories or []),
                "has_search": bool(search_query),
                "search_len": len(search_query or ""),
                "txn_type": txn_type,
                "result_count": int(len(df)),
            },
            source="dashboard",
        )

    # ── Main Content ─────────────────────────────────────────────
    with tab_analytics:
        _render_analytics(df)

    with tab_statements:
        _render_statements(db)

    with tab_stmt_analysis:
        _render_monthly_analysis(db)


# ── Analytics tab renderer ────────────────────────────────────────────────────

def _render_analytics(df: "pd.DataFrame") -> None:
    if df.empty:
        st.info("No transactions match the selected filters.")
        return

    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M").astype(str)
    df["day_of_week"] = df["date"].dt.day_name()
    df["week"] = df["date"].dt.isocalendar().week.astype(int)

    debits = df[df["type"] == "debit"]
    credits = df[df["type"] == "credit"]

    # ── Summary Cards ─────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)

    total_spend = debits["amount"].sum()
    total_credit = credits["amount"].sum()
    net_bill = total_spend - total_credit
    num_months = max(debits["month"].nunique(), 1)
    avg_monthly = total_spend / num_months
    top_category = debits.groupby("category")["amount"].sum().idxmax() if not debits.empty else "N/A"
    num_txns = len(df)

    m1.metric("Total Spend", format_inr(total_spend), help="Sum of all debit transactions (purchases, EMIs, fees)")
    m2.metric("Total Payments & Refunds", format_inr(total_credit), help="Sum of all credits: bill payments you made + merchant refunds/cashbacks. High value is expected — it means you paid your bills regularly.")
    m3.metric("Outstanding Balance", format_inr(net_bill), help="Total Spend minus all Payments & Refunds. This is the approximate unpaid balance across all cards (typically just the latest statement cycle).")
    m4.metric("Top Category", top_category)
    m5.metric("Transactions", f"{num_txns:,}")

    st.markdown("---")

    # ── Charts Row 1 ─────────────────────────────────────────────
    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.markdown('<div class="chart-title">Monthly Spend Trend</div>', unsafe_allow_html=True)
        monthly = debits.groupby("month").agg(
            total=("amount", "sum"),
            count=("amount", "count"),
        ).reset_index()

        if not monthly.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=monthly["month"], y=monthly["total"],
                mode="lines+markers",
                line=dict(color="#0ea5e9", width=2.5, shape="spline"),
                marker=dict(size=6, color="#0ea5e9", line=dict(width=1, color="#0f172a")),
                fill="tozeroy",
                fillcolor="rgba(14,165,233,0.08)",
                hovertemplate="<b>%{x}</b><br>₹%{y:,.0f}<extra></extra>",
            ))
            fig.update_layout(
                **PLOTLY_LAYOUT,
                yaxis_tickprefix="₹",
                hovermode="x unified",
                height=350,
            )
            st.plotly_chart(fig, width='stretch')

    with chart_col2:
        st.markdown('<div class="chart-title">Category Breakdown</div>', unsafe_allow_html=True)
        cat_spend = debits.groupby("category")["amount"].sum().reset_index()
        cat_spend = cat_spend.sort_values("amount", ascending=False)

        if not cat_spend.empty:
            fig = go.Figure(data=[go.Pie(
                labels=cat_spend["category"],
                values=cat_spend["amount"],
                hole=0.55,
                marker=dict(colors=CHART_COLORS[:len(cat_spend)], line=dict(color="#0f172a", width=2)),
                textinfo="percent+label",
                textposition="outside",
                textfont=dict(size=11, color="#e2e8f0"),
                hovertemplate="<b>%{label}</b><br>₹%{value:,.0f}<br>%{percent}<extra></extra>",
                pull=[0.03 if i == 0 else 0 for i in range(len(cat_spend))],
            )])
            fig.update_layout(**PLOTLY_LAYOUT, height=350, showlegend=False)
            st.plotly_chart(fig, width='stretch')

    # ── Charts Row 2 ─────────────────────────────────────────────
    chart_col3, chart_col4 = st.columns(2)

    with chart_col3:
        st.markdown('<div class="chart-title">Top Merchants</div>', unsafe_allow_html=True)
        merchant_spend = debits.groupby("description")["amount"].sum().nlargest(10).reset_index()

        if not merchant_spend.empty:
            fig = go.Figure(data=[go.Bar(
                x=merchant_spend["amount"],
                y=merchant_spend["description"],
                orientation="h",
                marker=dict(
                    color=merchant_spend["amount"],
                    colorscale=[[0, "#0ea5e9"], [1, "#10b981"]],
                    cornerradius=4,
                    line=dict(width=0),
                ),
                hovertemplate="<b>%{y}</b><br>₹%{x:,.0f}<extra></extra>",
            )])
            fig.update_layout(**PLOTLY_LAYOUT, xaxis_tickprefix="₹", height=400)
            fig.update_layout(yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"))
            st.plotly_chart(fig, width='stretch')

    with chart_col4:
        st.markdown('<div class="chart-title">Spend by Bank & Card</div>', unsafe_allow_html=True)
        if "card_name" in debits.columns and debits["card_name"].notna().any():
            debits_with_card = debits.copy()
            debits_with_card["bank_card"] = debits_with_card.apply(
                lambda r: f"{r['bank']} · {r['card_name']}" if pd.notna(r.get("card_name")) else r["bank"],
                axis=1,
            )
            card_spend = debits_with_card.groupby("bank_card")["amount"].sum().reset_index()
            card_spend = card_spend.sort_values("amount", ascending=False)
            if not card_spend.empty:
                fig = go.Figure(data=[go.Bar(
                    x=card_spend["bank_card"],
                    y=card_spend["amount"],
                    marker=dict(
                        color=CHART_COLORS[:len(card_spend)],
                        cornerradius=4,
                        line=dict(width=0),
                    ),
                    hovertemplate="<b>%{x}</b><br>₹%{y:,.0f}<extra></extra>",
                )])
                fig.update_layout(
                    **PLOTLY_LAYOUT,
                    yaxis_tickprefix="₹",
                    height=400,
                )
                st.plotly_chart(fig, width='stretch')
        else:
            bank_spend = debits.groupby("bank")["amount"].sum().reset_index()
            bank_spend = bank_spend.sort_values("amount", ascending=False)
            if not bank_spend.empty:
                fig = go.Figure(data=[go.Bar(
                    x=bank_spend["bank"],
                    y=bank_spend["amount"],
                    marker=dict(
                        color=CHART_COLORS[:len(bank_spend)],
                        cornerradius=4,
                        line=dict(width=0),
                    ),
                    hovertemplate="<b>%{x}</b><br>₹%{y:,.0f}<extra></extra>",
                )])
                fig.update_layout(
                    **PLOTLY_LAYOUT,
                    yaxis_tickprefix="₹",
                    height=400,
                )
                st.plotly_chart(fig, width='stretch')

    # ── Daily Heatmap ─────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="chart-title">Daily Spend Heatmap</div>', unsafe_allow_html=True)

    if not debits.empty:
        daily = debits.groupby("date")["amount"].sum().reset_index()
        daily["day_of_week"] = daily["date"].dt.day_name()
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        fig = px.scatter(
            daily, x="date", y="day_of_week",
            size="amount", color="amount",
            color_continuous_scale=[[0, "#0f172a"], [0.3, "#0ea5e9"], [0.7, "#f59e0b"], [1, "#ef4444"]],
            size_max=18,
            labels={"amount": "Spend (₹)", "day_of_week": "Day", "date": "Date"},
            category_orders={"day_of_week": day_order},
        )
        fig.update_layout(**PLOTLY_LAYOUT, height=280, coloraxis_colorbar=dict(title="Spend (₹)", tickprefix="₹", bgcolor="rgba(0,0,0,0)"))
        fig.update_layout(yaxis=dict(categoryorder="array", categoryarray=day_order[::-1], gridcolor="rgba(0,0,0,0)"))
        st.plotly_chart(fig, width='stretch')

    # ── Category Drill-down ───────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="chart-title">Category Drill-down</div>', unsafe_allow_html=True)

    if not debits.empty:
        selected_drill_category = st.selectbox(
            "Select category to drill down",
            options=sorted(debits["category"].unique()),
        )

        cat_df = debits[debits["category"] == selected_drill_category]

        drill_col1, drill_col2 = st.columns(2)

        with drill_col1:
            cat_monthly = cat_df.groupby("month")["amount"].sum().reset_index()
            if not cat_monthly.empty:
                fig = go.Figure(data=[go.Bar(
                    x=cat_monthly["month"],
                    y=cat_monthly["amount"],
                    marker=dict(color="#8b5cf6", cornerradius=4, line=dict(width=0)),
                    hovertemplate="<b>%{x}</b><br>₹%{y:,.0f}<extra></extra>",
                )])
                fig.update_layout(**PLOTLY_LAYOUT, yaxis_tickprefix="₹", height=350)
                st.plotly_chart(fig, width='stretch')

        with drill_col2:
            cat_merchants = cat_df.groupby("description")["amount"].sum().nlargest(10).reset_index()
            if not cat_merchants.empty:
                fig = go.Figure(data=[go.Bar(
                    x=cat_merchants["amount"],
                    y=cat_merchants["description"],
                    orientation="h",
                    marker=dict(color="#ec4899", cornerradius=4, line=dict(width=0)),
                    hovertemplate="<b>%{y}</b><br>₹%{x:,.0f}<extra></extra>",
                )])
                fig.update_layout(**PLOTLY_LAYOUT, xaxis_tickprefix="₹", height=350)
                fig.update_layout(yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"))
                st.plotly_chart(fig, width='stretch')

    # ── Transaction Table ─────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="chart-title">Transaction Details</div>', unsafe_allow_html=True)

    sort_col1, sort_col2 = st.columns([3, 1])
    sort_by = sort_col1.selectbox(
        "Sort by",
        ["date", "amount", "description", "category", "bank"],
        index=0,
    )
    sort_order = sort_col2.radio("Order", ["Descending", "Ascending"], horizontal=True)

    display_df = df.copy()
    display_df = display_df.sort_values(sort_by, ascending=(sort_order == "Ascending"))

    display_cols = ["date", "description", "amount", "type", "category", "bank", "card_name", "card_last4"]
    display_cols = [c for c in display_cols if c in display_df.columns]
    display_df = display_df[display_cols].copy()
    display_df["date"] = display_df["date"].dt.strftime("%Y-%m-%d")
    display_df["amount"] = display_df["amount"].apply(lambda x: f"₹{x:,.2f}")

    page_size = st.selectbox("Rows per page", [25, 50, 100, 200], index=0)
    total_pages = max(1, (len(display_df) - 1) // page_size + 1)
    page = st.number_input("Page", min_value=1, max_value=total_pages, value=1)

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size

    st.dataframe(
        display_df.iloc[start_idx:end_idx],
        width='stretch',
        hide_index=True,
    )
    st.caption(f"Showing {start_idx + 1}-{min(end_idx, len(display_df))} of {len(display_df)} transactions")

    # ── Download ─────────────────────────────────────────────────
    st.markdown("---")
    export_cols = ["date", "description", "amount", "type", "category", "bank", "card_name", "card_last4"]
    export_cols = [c for c in export_cols if c in df.columns]
    csv_data = df[export_cols].copy()
    csv_data["date"] = csv_data["date"].dt.strftime("%Y-%m-%d")

    st.download_button(
        label="📥 Download Filtered Data as CSV",
        data=csv_data.to_csv(index=False).encode("utf-8"),
        file_name=f"stmtforge_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )


# ── Statements tab renderer ───────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _load_statements(_db):
    """Load all statement metadata rows."""
    import sqlite3
    with sqlite3.connect(str(_db.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT bank, card_name, filename, status, transaction_count, "
            "email_date, statement_period_start, statement_period_end, "
            "total_amount_due, total_spends, total_credits, "
            "error_message, created_at "
            "FROM statements_metadata ORDER BY created_at DESC"
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _render_statements(db) -> None:
    st.markdown('<div class="chart-title">Processed Statements</div>', unsafe_allow_html=True)

    sm = _load_statements(db)

    if sm.empty:
        st.info("No statements recorded yet. Run the pipeline first.")
        return

    # ── Summary metrics ───────────────────────────────────────────
    total = len(sm)
    completed = (sm["status"] == "completed").sum()
    no_data = (sm["status"] == "no_data").sum()
    errored = (sm["status"] == "error").sum()
    txn_total = sm["transaction_count"].sum()

    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Total Statements", total)
    mc2.metric("✅ Parsed", completed)
    mc3.metric("🔒 No Data / Locked", no_data)
    mc4.metric("❌ Errors", errored)
    mc5.metric("Transactions Imported", int(txn_total))

    st.markdown("---")

    # ── Status breakdown by bank ──────────────────────────────────
    if not sm.empty:
        bank_status = sm.groupby(["bank", "status"]).size().reset_index(name="count")
        if not bank_status.empty:
            fig = px.bar(
                bank_status, x="bank", y="count", color="status",
                barmode="stack",
                labels={"bank": "Bank", "count": "Statements", "status": "Status"},
                color_discrete_map={
                    "completed": "#10b981",
                    "no_data": "#f59e0b",
                    "error": "#ef4444",
                    "pending": "#64748b",
                },
            )
            fig.update_layout(**PLOTLY_LAYOUT, showlegend=True, height=300)
            st.plotly_chart(fig, width='stretch')

    st.markdown("---")

    # ── Filter + table ────────────────────────────────────────────
    status_filter = st.multiselect(
        "Filter by status",
        options=sorted(sm["status"].unique()),
        default=sorted(sm["status"].unique()),
    )
    bank_filter = st.multiselect(
        "Filter by bank",
        options=sorted(sm["bank"].dropna().unique()),
        default=sorted(sm["bank"].dropna().unique()),
    )

    filtered_sm = sm[
        sm["status"].isin(status_filter) & sm["bank"].isin(bank_filter)
    ].copy()

    # Friendly column names
    filtered_sm = filtered_sm.rename(columns={
        "bank": "Bank",
        "card_name": "Card",
        "filename": "File",
        "status": "Status",
        "transaction_count": "Txns",
        "email_date": "Email Date",
        "statement_period_start": "Period Start",
        "statement_period_end": "Period End",
        "total_amount_due": "Total Due (₹)",
        "total_spends": "Spends (₹)",
        "total_credits": "Credits (₹)",
        "error_message": "Error",
    })
    filtered_sm = filtered_sm.drop(columns=["created_at"], errors="ignore")

    st.dataframe(filtered_sm, width='stretch', hide_index=True)
    st.caption(f"{len(filtered_sm)} of {len(sm)} statements shown")


# ── Monthly Statement Analysis tab ───────────────────────────────────────────

@st.cache_data(ttl=300)
def _load_statement_months(_db):
    return _db.get_statement_months()


@st.cache_data(ttl=300)
def _load_statements_for_month(_db, year_month: str):
    return _db.get_statements_for_month(year_month)


@st.cache_data(ttl=300)
def _load_txns_for_hashes(_db, hashes_tuple: tuple):
    return _db.get_transactions_for_file_hashes(list(hashes_tuple))


def _fmt_period(start, end) -> str:
    """Return a human-readable billing period string, safely handling None/NaN."""
    def _fmt(d):
        if d is None:
            return "?"
        s = str(d).strip()
        if not s or s.lower() in ("nan", "none", "nat", ""):
            return "?"
        try:
            return pd.to_datetime(s, dayfirst=False).strftime("%d %b %Y")
        except Exception:
            return s
    return f"{_fmt(start)} → {_fmt(end)}"


def _txn_period(txns: pd.DataFrame) -> str:
    """Return 'DD Mon YYYY → DD Mon YYYY' from actual transaction dates."""
    if txns.empty or "date" not in txns.columns:
        return "? → ?"
    dates = pd.to_datetime(txns["date"], errors="coerce").dropna()
    if dates.empty:
        return "? → ?"
    return f"{dates.min().strftime('%d %b %Y')} → {dates.max().strftime('%d %b %Y')}"


def _render_monthly_analysis(db) -> None:
    """Render the Monthly Statement Analysis tab."""
    st.markdown('<div class="chart-title">📅 Monthly Card Statement Analysis</div>', unsafe_allow_html=True)
    st.caption("Select a billing month to drill into per-card statement details, spend breakdown, and transaction history.")

    # ── Month selector ────────────────────────────────────────────
    months = _load_statement_months(db)
    if not months:
        st.info("No processed statements found. Run the pipeline or parse a PDF first.")
        return

    def _month_label(ym: str) -> str:
        try:
            return pd.to_datetime(ym + "-01").strftime("%B %Y")
        except Exception:
            return ym

    month_labels = [_month_label(m) for m in months]
    month_map = dict(zip(month_labels, months))

    col_sel, col_info = st.columns([2, 3])
    with col_sel:
        selected_label = st.selectbox(
            "📆 Billing Month",
            options=month_labels,
            index=0,
        )
    selected_ym = month_map[selected_label]

    # ── Load statements for selected month ────────────────────────
    sm = _load_statements_for_month(db, selected_ym)

    if sm.empty:
        st.info(f"No statements found for {selected_label}.")
        return

    # Separate completed vs others
    completed = sm[sm["status"] == "completed"].copy()
    others = sm[sm["status"] != "completed"].copy()

    # ── Aggregate top metrics ─────────────────────────────────────
    all_hashes = [str(h) for h in completed["file_hash"].dropna().unique().tolist()]
    all_txns = _load_txns_for_hashes(db, tuple(all_hashes)) if all_hashes else pd.DataFrame()

    if not all_txns.empty:
        all_txns["date"] = pd.to_datetime(all_txns["date"], errors="coerce")
        all_debits = all_txns[all_txns["type"] == "debit"]
        all_credits = all_txns[all_txns["type"] == "credit"]
    else:
        all_debits = pd.DataFrame()
        all_credits = pd.DataFrame()

    skipped_count = len(others[others["status"] == "skipped_irrelevant"]) if not others.empty else 0
    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Statements", len(sm), delta=f"-{skipped_count} skipped" if skipped_count else None, delta_color="inverse")
    mc2.metric("✅ Parsed", len(completed))
    mc3.metric("Transactions", len(all_txns) if not all_txns.empty else 0)
    mc4.metric("Total Spend", format_inr(all_debits["amount"].sum()) if not all_debits.empty else "₹0")
    mc5.metric("Total Credits", format_inr(all_credits["amount"].sum()) if not all_credits.empty else "₹0")

    # ── Month-level charts (only when >1 card) ────────────────────
    if len(completed) > 1 and not all_debits.empty:
        st.markdown("---")
        st.markdown("#### Month-level Breakdown")

        ov_col1, ov_col2 = st.columns(2)

        with ov_col1:
            st.markdown('<div class="chart-title">Spend by Card</div>', unsafe_allow_html=True)
            # Join card_name onto all_debits via file_hash
            hash_to_card = dict(zip(completed["file_hash"], completed.apply(
                lambda r: f"{(r.get('bank') or 'unknown').upper()} · {r['card_name']}" if pd.notna(r.get("card_name")) else (r.get("bank") or "unknown").upper(),
                axis=1,
            )))
            card_spend = (
                all_debits.assign(card_label=all_debits["file_hash"].map(hash_to_card))
                .groupby("card_label")["amount"].sum()
                .reset_index(name="amount")
                .sort_values("amount", ascending=False)
            )
            if not card_spend.empty:
                fig = go.Figure(data=[go.Pie(
                    labels=card_spend["card_label"],
                    values=card_spend["amount"],
                    hole=0.55,
                    marker=dict(colors=CHART_COLORS[:len(card_spend)], line=dict(color="#0f172a", width=2)),
                    textinfo="percent+label",
                    textposition="outside",
                    textfont=dict(size=11, color="#e2e8f0"),
                    hovertemplate="<b>%{label}</b><br>₹%{value:,.0f}<br>%{percent}<extra></extra>",
                )])
                fig.update_layout(**PLOTLY_LAYOUT, height=300, showlegend=False)
                st.plotly_chart(fig, width='stretch')

        with ov_col2:
            st.markdown('<div class="chart-title">Category Breakdown (All Cards)</div>', unsafe_allow_html=True)
            cat_spend = all_debits.groupby("category")["amount"].sum().reset_index(name="amount")
            cat_spend = cat_spend.sort_values("amount", ascending=False)
            if not cat_spend.empty:
                fig = go.Figure(data=[go.Bar(
                    x=cat_spend["amount"],
                    y=cat_spend["category"],
                    orientation="h",
                    marker=dict(
                        color=CHART_COLORS[:len(cat_spend)],
                        cornerradius=4,
                        line=dict(width=0),
                    ),
                    hovertemplate="<b>%{y}</b><br>₹%{x:,.0f}<extra></extra>",
                )])
                fig.update_layout(**PLOTLY_LAYOUT, xaxis_tickprefix="₹", height=300)
                fig.update_layout(yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"))
                st.plotly_chart(fig, width='stretch')

    # ── Per-statement card sections ───────────────────────────────
    st.markdown("---")
    st.markdown("#### Per-Card Statement Details")

    if completed.empty:
        st.info("No successfully parsed statements for this month.")
    else:
        for idx, (_, stmt_row) in enumerate(completed.iterrows()):
            bank = (stmt_row.get("bank") or "unknown").upper()
            card_name = stmt_row.get("card_name") or ""
            card_last4 = stmt_row.get("card_last4") or ""
            filename = stmt_row.get("filename") or "—"
            file_hash = stmt_row.get("file_hash")
            txn_count_meta = int(stmt_row.get("transaction_count") or 0)

            # Load transactions early so we can derive the actual period
            stmt_txns = _load_txns_for_hashes(db, (str(file_hash),)) if file_hash else pd.DataFrame()
            if not stmt_txns.empty:
                stmt_txns["date"] = pd.to_datetime(stmt_txns["date"], errors="coerce")

            # Determine billing period: prefer DB fields, fall back to transaction date range
            db_period_start = stmt_row.get("statement_period_start")
            db_period_end = stmt_row.get("statement_period_end")
            db_period_str = _fmt_period(db_period_start, db_period_end)
            if db_period_str == "? → ?":
                # Derive from actual transaction dates
                period = _txn_period(stmt_txns)
            else:
                period = db_period_str

            stmt_debits = stmt_txns[stmt_txns["type"] == "debit"] if not stmt_txns.empty else pd.DataFrame()
            stmt_credits = stmt_txns[stmt_txns["type"] == "credit"] if not stmt_txns.empty else pd.DataFrame()

            # Card header label
            card_label = f"{bank}"
            if card_name:
                card_label += f" · {card_name}"
            if card_last4:
                card_label += f" (····{card_last4})"

            total_spend = format_inr(stmt_debits["amount"].sum()) if not stmt_debits.empty else "₹0"
            with st.expander(f"🏦 {card_label}   |   📅 {period}   |   {total_spend}", expanded=True):

                # ── Statement header info ─────────────────────────
                info_col, _ = st.columns([3, 1])
                with info_col:
                    st.caption(
                        f"📄 **File:** {filename}  |  "
                        f"📅 **Statement Period:** {period}  |  "
                        f"📧 **Email date:** {stmt_row.get('email_date') or '—'}"
                    )

                # ── Key metrics ───────────────────────────────────
                km1, km2, km3, km4, km5 = st.columns(5)
                km1.metric("Total Spend", format_inr(stmt_debits["amount"].sum()) if not stmt_debits.empty else "₹0")
                km2.metric("Total Credits", format_inr(stmt_credits["amount"].sum()) if not stmt_credits.empty else "₹0")
                net = (stmt_debits["amount"].sum() if not stmt_debits.empty else 0) - (stmt_credits["amount"].sum() if not stmt_credits.empty else 0)
                # Show canonical Total Amount Due from statement summary if available
                stmt_total_due = stmt_row.get("total_amount_due")
                if stmt_total_due is not None and not pd.isna(stmt_total_due):
                    km3.metric("Total Amount Due", format_inr(float(stmt_total_due)), help="From statement summary")
                else:
                    km3.metric("Net Spend", format_inr(net))
                km4.metric("Transactions", len(stmt_txns) if not stmt_txns.empty else txn_count_meta)
                if not stmt_debits.empty and "category" in stmt_debits.columns and stmt_debits["category"].notna().any():
                    top_cat = stmt_debits.groupby("category")["amount"].sum().idxmax()
                    km5.metric("Top Category", top_cat)
                else:
                    km5.metric("Top Category", "—")

                if stmt_txns.empty:
                    st.info("No transaction details available for this statement.")
                    continue

                # ── Charts ────────────────────────────────────────
                ch1, ch2 = st.columns(2)

                with ch1:
                    st.markdown('<div class="chart-title">Category Breakdown</div>', unsafe_allow_html=True)
                    if not stmt_debits.empty and "category" in stmt_debits.columns:
                        cat_d = stmt_debits.groupby("category")["amount"].sum().reset_index(name="amount")
                        cat_d = cat_d.sort_values("amount", ascending=False)
                        fig = go.Figure(data=[go.Pie(
                            labels=cat_d["category"],
                            values=cat_d["amount"],
                            hole=0.5,
                            marker=dict(colors=CHART_COLORS[:len(cat_d)], line=dict(color="#0f172a", width=2)),
                            textinfo="percent+label",
                            textposition="outside",
                            textfont=dict(size=11, color="#e2e8f0"),
                            hovertemplate="<b>%{label}</b><br>₹%{value:,.0f}<br>%{percent}<extra></extra>",
                            pull=[0.03 if i == 0 else 0 for i in range(len(cat_d))],
                        )])
                        fig.update_layout(**PLOTLY_LAYOUT, height=280, showlegend=False)
                        st.plotly_chart(fig, width='stretch')

                with ch2:
                    st.markdown('<div class="chart-title">Top 10 Merchants</div>', unsafe_allow_html=True)
                    if not stmt_debits.empty:
                        merch = stmt_debits.groupby("description")["amount"].sum().nlargest(10).reset_index(name="amount")
                        fig = go.Figure(data=[go.Bar(
                            x=merch["amount"],
                            y=merch["description"],
                            orientation="h",
                            marker=dict(
                                color=merch["amount"],
                                colorscale=[[0, "#0ea5e9"], [1, "#10b981"]],
                                cornerradius=4,
                                line=dict(width=0),
                            ),
                            hovertemplate="<b>%{y}</b><br>₹%{x:,.0f}<extra></extra>",
                        )])
                        fig.update_layout(**PLOTLY_LAYOUT, xaxis_tickprefix="₹", height=280)
                        fig.update_layout(yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"))
                        st.plotly_chart(fig, width='stretch')

                # ── Daily spend trend ─────────────────────────────
                if not stmt_debits.empty:
                    daily = stmt_debits.groupby("date")["amount"].sum().reset_index()
                    st.markdown('<div class="chart-title">Daily Spend Trend</div>', unsafe_allow_html=True)
                    fig = go.Figure()
                    fig.add_trace(go.Bar(
                        x=daily["date"],
                        y=daily["amount"],
                        marker=dict(color="#0ea5e9", cornerradius=3, line=dict(width=0)),
                        hovertemplate="<b>%{x|%d %b}</b><br>₹%{y:,.0f}<extra></extra>",
                    ))
                    fig.update_layout(**PLOTLY_LAYOUT, yaxis_tickprefix="₹", height=200)
                    st.plotly_chart(fig, width='stretch')

                # ── Transaction table ─────────────────────────────
                with st.expander(f"📋 Transactions ({len(stmt_txns)})", expanded=False):
                    disp = stmt_txns.copy()
                    show_cols = ["date", "description", "amount", "type", "category", "card_last4"]
                    show_cols = [c for c in show_cols if c in disp.columns]
                    disp = disp[show_cols].copy()
                    disp["date"] = disp["date"].dt.strftime("%Y-%m-%d").fillna("")
                    disp["amount"] = disp["amount"].apply(lambda x: f"₹{x:,.2f}")
                    st.dataframe(disp, width='stretch', hide_index=True)

                    # Download
                    export = stmt_txns[show_cols].copy()
                    export["date"] = export["date"].dt.strftime("%Y-%m-%d").fillna("")
                    safe_name = (card_name or bank).replace(" ", "_").replace("/", "_")
                    st.download_button(
                        label="📥 Download CSV",
                        data=export.to_csv(index=False).encode("utf-8"),
                        file_name=f"stmt_{selected_ym}_{safe_name}.csv",
                        mime="text/csv",
                        key=f"dl_{file_hash}_{idx}",
                    )

    # ── Non-completed statements ──────────────────────────────────
    if not others.empty:
        skipped = (others["status"] == "skipped_irrelevant").sum()
        no_data = (others["status"] == "no_data").sum()
        err = (others["status"] == "error").sum()
        label_parts = []
        if skipped:
            label_parts.append(f"{skipped} skipped/irrelevant")
        if no_data:
            label_parts.append(f"{no_data} no data")
        if err:
            label_parts.append(f"{err} error")
        label = ", ".join(label_parts) or str(len(others))
        st.markdown("---")
        with st.expander(f"⚠️ Other statements this month ({label})", expanded=False):
            st.caption("These statements were not parsed. Check the 'Skip Reason' column for details.")
            disp_others = others[["bank", "card_name", "filename", "status", "error_message"]].copy()
            disp_others = disp_others.rename(columns={
                "bank": "Bank", "card_name": "Card", "filename": "File",
                "status": "Status", "error_message": "Skip Reason",
            })
            st.dataframe(disp_others, width='stretch', hide_index=True)


# ── Parse PDF helpers ─────────────────────────────────────────────────────────

def _detect_bank_from_pdf_text(text: str) -> str | None:
    """Heuristic bank detection from first-page PDF text."""
    t = text.lower()
    if "csb bank" in t or "edge csb" in t:
        return "csb"
    if "federal bank" in t or "federalbank" in t:
        return "federal"
    if "idfc first" in t or "idfcfirstbank" in t:
        return "idfc_first"
    if "axis bank" in t or "axisbank" in t:
        return "axis"
    if "hdfc bank" in t:
        return "hdfc"
    if "sbi card" in t or "sbicard" in t:
        return "sbi"
    if "icici" in t:
        return "icici"
    if "yes bank" in t or "yesbank" in t:
        return "yes"
    if "kotak" in t:
        return "kotak"
    return None


def _unlock_pdf_bytes(pdf_bytes: bytes, extra_password: str | None) -> tuple[bytes | None, str]:
    """
    Try to produce an unlocked PDF from raw bytes.

    Priority:
      1. Open without password (already unlocked)
      2. User-supplied password
      3. Configured bank passwords

    Returns (unlocked_bytes, status_message).
    """
    # 1. Try as-is
    try:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue(), "not_encrypted"
    except pikepdf.PasswordError:
        pass
    except Exception as e:
        return None, f"open_error:{e}"

    # 2. Build password candidate list
    cfg = load_config()
    known_pwds = get_all_passwords(cfg)
    candidates: list[str] = []
    if extra_password:
        candidates.append(extra_password)
    candidates.extend(known_pwds)

    for pwd in candidates:
        try:
            with pikepdf.open(io.BytesIO(pdf_bytes), password=pwd) as pdf:
                buf = io.BytesIO()
                pdf.save(buf)
                return buf.getvalue(), "unlocked"
        except pikepdf.PasswordError:
            continue
        except Exception:
            continue

    return None, "unlock_failed"


def _do_parse(
    pdf_bytes: bytes,
    filename: str,
    bank_ui: str,
    password: str | None,
    db: "Database",
) -> tuple[dict, str | None]:
    """
    Unlock, detect bank, and run the hybrid pipeline on the uploaded PDF.

    Returns (result_dict, error_message).  error_message is None on success.
    """
    # ── 1. Unlock ────────────────────────────────────────────────
    unlocked_bytes, unlock_status = _unlock_pdf_bytes(pdf_bytes, password)
    if unlocked_bytes is None:
        return {}, (
            "Could not unlock the PDF. "
            "Please enter the correct password in the 'PDF Password' field."
        )

    # ── 2. Extract first-page text for bank detection ─────────────
    first_page_text = ""
    try:
        with pdfplumber.open(io.BytesIO(unlocked_bytes)) as _pdf:
            if _pdf.pages:
                first_page_text = _pdf.pages[0].extract_text() or ""
    except Exception:
        pass

    # ── 3. Resolve bank ──────────────────────────────────────────
    if bank_ui == "Auto-detect":
        bank = _detect_bank_from_pdf_text(first_page_text) or "unknown"
    else:
        bank = bank_ui

    # ── 4. Write unlocked bytes to temp file ──────────────────────
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(unlocked_bytes)

        fhash = compute_file_hash(tmp_path)

        # Ensure statement row exists so update_statement_status works
        db.record_statement(
            file_hash=fhash,
            original_path=filename,
            bank=bank,
            filename=filename,
        )

        # ── 5. Run pipeline ───────────────────────────────────────
        pipeline = HybridPipeline(db=db)
        result = pipeline.process_pdf(
            tmp_path,
            bank=bank,
            fhash=fhash,
            path=tmp_path,
        )
        result["detected_bank"] = bank
        result["filename"] = filename
        result["unlock_status"] = unlock_status
        return result, None

    except Exception as exc:
        return {}, f"Parsing failed: {exc}"

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Parse PDF UI ──────────────────────────────────────────────────────────────

def _render_parse_pdf(db: "Database") -> None:
    """Render the Parse PDF tab — full pipeline without Gmail fetch."""
    st.markdown('<div class="chart-title">Parse PDF Statement</div>', unsafe_allow_html=True)
    st.caption(
        "Upload a credit card statement PDF to extract transactions. "
        "Results are saved to the database automatically."
    )

    # ── Upload + options row ──────────────────────────────────────
    upload_col, opts_col = st.columns([3, 2])

    with upload_col:
        uploaded_file = st.file_uploader(
            "Drop or browse a credit card statement PDF",
            type=["pdf"],
            help=(
                "Encrypted PDFs will be auto-unlocked using known bank passwords. "
                "Supply a custom password below if the file uses a different one."
            ),
        )

    with opts_col:
        bank_choices = ["Auto-detect"] + sorted(list_available_parsers()) + ["unknown"]
        selected_bank_ui = st.selectbox(
            "Bank",
            bank_choices,
            index=0,
            help="'Auto-detect' reads the first page to identify the bank.",
        )
        pdf_password = st.text_input(
            "PDF Password (optional)",
            type="password",
            help="Only needed if the PDF password is not in the standard bank list.",
        )

    st.markdown("---")

    parse_clicked = st.button(
        "🔍 Parse PDF",
        type="primary",
        disabled=uploaded_file is None,
    )

    if uploaded_file is None:
        st.info("⬆️ Upload a PDF above to get started.")
        return

    # ── Trigger or re-display ────────────────────────────────────
    result_key = f"_parse_result_{uploaded_file.name}_{uploaded_file.size}"

    if parse_clicked:
        # Clear any previous result for this file
        st.session_state.pop(result_key, None)

        with st.spinner("🔓 Unlocking & parsing PDF — this may take a moment…"):
            result, err = _do_parse(
                uploaded_file.getvalue(),
                uploaded_file.name,
                selected_bank_ui,
                pdf_password.strip() or None,
                db,
            )

        if err:
            st.error(f"❌ {err}")
            return

        st.session_state[result_key] = result
        # Invalidate caches so all tabs reflect new data
        load_transactions.clear()
        _load_statements.clear()
        _load_statement_months.clear()
        _load_statements_for_month.clear()
        _load_txns_for_hashes.clear()

    result = st.session_state.get(result_key)
    if result:
        _display_parse_result(result, uploaded_file.name)


def _display_parse_result(result: dict, filename: str) -> None:
    """Render metrics + transaction table for a parse result."""
    method = result.get("method", "unknown")
    confidence = result.get("confidence", 0.0)
    txn_count = result.get("transaction_count", 0)
    inserted = result.get("inserted", 0)
    card_name = result.get("card_name")
    bank = result.get("detected_bank", "unknown")
    unlock_status = result.get("unlock_status", "")

    st.markdown('<div class="chart-title">Parse Results</div>', unsafe_allow_html=True)

    # ── Top metrics ───────────────────────────────────────────────
    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Bank Detected", bank.upper())
    mc2.metric("Method", method.split("(")[0])
    mc3.metric("Confidence", f"{confidence:.0%}")
    mc4.metric("Transactions", txn_count)
    mc5.metric("New to DB", inserted)

    if card_name:
        st.success(f"Card identified: **{card_name}**")
    if unlock_status == "unlocked":
        st.info("🔓 PDF was password-protected and successfully unlocked.")

    if inserted < txn_count:
        st.caption(
            f"ℹ️ {txn_count - inserted} transaction(s) already existed in the database "
            "(duplicate hashes — no double-counting)."
        )

    if txn_count == 0:
        st.warning(
            "No transactions could be extracted from this PDF. "
            "Try selecting the correct bank or ensure the file is a credit card statement."
        )
        return

    # ── Transaction breakdown ─────────────────────────────────────
    txns = result.get("transactions", [])
    if not txns:
        return

    df = pd.DataFrame(txns)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    debits = df[df["type"] == "debit"] if "type" in df.columns else df
    credits = df[df["type"] == "credit"] if "type" in df.columns else pd.DataFrame()

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Total Debit", format_inr(debits["amount"].sum() if not debits.empty else 0))
    sc2.metric("Total Credit", format_inr(credits["amount"].sum() if not credits.empty else 0))
    if not debits.empty and "category" in debits.columns and debits["category"].notna().any():
        top_cat = debits.groupby("category")["amount"].sum().idxmax()
        sc3.metric("Top Category", top_cat)

    # ── Transaction table ─────────────────────────────────────────
    st.markdown("#### Extracted Transactions")
    display_cols = ["date", "description", "amount", "type", "category", "card_name", "card_last4"]
    display_cols = [c for c in display_cols if c in df.columns]

    disp = df[display_cols].copy()
    if "date" in disp.columns:
        disp["date"] = disp["date"].dt.strftime("%Y-%m-%d").fillna("")
    if "amount" in disp.columns:
        disp["amount"] = disp["amount"].apply(lambda x: f"₹{x:,.2f}")

    st.dataframe(disp, width='stretch', hide_index=True)

    # ── Download ──────────────────────────────────────────────────
    export_df = df[display_cols].copy()
    if "date" in export_df.columns:
        export_df["date"] = export_df["date"].dt.strftime("%Y-%m-%d").fillna("")
    stem = Path(filename).stem
    st.download_button(
        label="📥 Download as CSV",
        data=export_df.to_csv(index=False).encode("utf-8"),
        file_name=f"parse_{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
