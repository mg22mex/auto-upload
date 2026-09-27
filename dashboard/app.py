"""Autosell analytics dashboard (Streamlit).

Run:
  streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

st.set_page_config(
    page_title="Autosell Analytics",
    page_icon=None,
    layout="wide",
)

st.title("Autosell — Analytics")
st.caption("CRM attribution, AI-sourced leads, and sales commissions")

tab_overview, tab_commissions, tab_ops = st.tabs(
    ["Resumen", "Comisiones y Atribución", "Operación"]
)

with tab_overview:
    st.subheader("Estado del pipeline")
    st.markdown(
        """
- **FB Marketplace** — sync live (`account_1` / `account_2`)
- **Beatriz / WhatsApp** — leads tagged `MG Quote Lead` in Odoo
- **Round Robin** — `data/rr_cursor.db` + Evolution rep cards
- **Comisiones** — ledger in `data/commissions.db` (`src/attribution.py`)

Call logs are stored as Odoo `crm.lead` + `mail.activity` (“Llamada Entrante”),
not a separate `call.log` model.
        """
    )
    st.info(
        "No prior Streamlit surface existed — this dashboard is greenfield. "
        "Channel UTM attribution already lives in `odoo_sync`."
    )

with tab_commissions:
    st.subheader("Comisiones y Atribución")
    from src.attribution import (
        SALE_IN_PROGRESS,
        SALE_LOST,
        SALE_WON,
        commissions_db_path,
        list_commissions,
        monthly_summary,
        reconcile_commissions_from_odoo,
    )

    col_a, col_b, col_c = st.columns([1, 1, 1])
    with col_a:
        month = st.text_input(
            "Mes (YYYY-MM)",
            value=datetime.now(timezone.utc).strftime("%Y-%m"),
        )
    with col_b:
        st.write("")
        st.write("")
        refresh = st.button("Reconciliar desde Odoo", type="primary")
    with col_c:
        st.write("")
        st.write("")
        st.caption(f"DB: `{commissions_db_path()}`")

    if refresh:
        with st.spinner("Consultando Odoo CRM (MG Quote Lead)…"):
            try:
                result = reconcile_commissions_from_odoo()
                if result.get("ok"):
                    st.success(
                        f"Synced {result['synced']}/{result['fetched']} leads "
                        f"(won={result['won']}, lost={result['lost']})"
                    )
                else:
                    st.warning(
                        f"Partial sync {result['synced']}/{result['fetched']}: "
                        f"{result.get('errors')}"
                    )
            except Exception as exc:
                st.error(f"Reconcile failed: {exc}")

    summary = monthly_summary(month)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Ventas cerradas (AI)", summary.won_count)
    m2.metric(
        "Monto cerrado",
        f"${summary.total_deal_amount:,.0f}",
    )
    m3.metric(
        "Comisión acumulada",
        f"${summary.total_commission:,.2f}",
    )
    m4.metric(
        "Pipeline / perdidos",
        f"{summary.in_progress_count} / {summary.lost_count}",
    )

    if summary.by_rep:
        st.markdown("#### Por asesor")
        rep_rows = [
            {
                "Asesor": name,
                "Cerradas": int(vals.get("won", 0)),
                "Monto": float(vals.get("deal_amount", 0)),
                "Comisión": float(vals.get("commission", 0)),
            }
            for name, vals in sorted(summary.by_rep.items())
        ]
        st.dataframe(rep_rows, use_container_width=True, hide_index=True)

    st.markdown("#### Ledger")
    status_filter = st.selectbox(
        "Estado",
        options=["(todos)", SALE_WON, SALE_IN_PROGRESS, SALE_LOST],
    )
    if status_filter == SALE_WON:
        rows = list_commissions(sale_status=SALE_WON, month=month)
    elif status_filter == "(todos)":
        rows = list_commissions()
    else:
        rows = list_commissions(sale_status=status_filter)

    table = [
        {
            "Lead": r.lead_id,
            "Cliente": r.customer_name or "—",
            "Teléfono": r.customer_phone,
            "Vehículo": r.vehicle_name or r.vehicle_vin or "—",
            "Asesor": r.assigned_rep or "—",
            "Estado": r.sale_status,
            "Monto": r.deal_amount,
            "%": r.commission_percentage,
            "Comisión": r.commission_amount,
            "Primer contacto": r.first_contact_timestamp,
            "Cierre": r.closed_at,
        }
        for r in rows
    ]
    if table:
        st.dataframe(table, use_container_width=True, hide_index=True)
    else:
        st.write("Sin registros todavía. Ejecuta reconciliación o cierra un lead en Odoo.")

with tab_ops:
    st.subheader("Verificación rápida")
    st.code(
        "\n".join(
            [
                "# Unit tests",
                "PYTHONPATH=. python -m unittest tests.test_attribution -q",
                "",
                "# CLI reconcile (Oracle / local with Odoo env)",
                "PYTHONPATH=. python scripts/reconcile_commissions.py",
                "",
                "# Dashboard",
                "streamlit run dashboard/app.py",
            ]
        ),
        language="bash",
    )
