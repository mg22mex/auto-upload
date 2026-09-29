"""Autosell — Executive CRM Dashboard (Streamlit).

Run:
  PYTHONPATH=. streamlit run dashboard/app.py --server.port 8501
"""
from __future__ import annotations

import io
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard.secrets_util import apply_odoo_secrets_to_environ

# Streamlit Cloud: st.secrets → ODOO_* env before any XML-RPC client init.
apply_odoo_secrets_to_environ()

from dashboard.odoo_data import (
    build_acta_pdf,
    compute_kpis,
    fetch_calendar_appointments,
    fetch_leads,
    fetch_sale_orders,
    get_odoo_client,
    load_junta_notes,
    normalize_leads,
    save_junta_note,
)

st.set_page_config(
    page_title="Autosell · Gerencia Comercial",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Premium theme (works in light + dark Streamlit themes) ──────────────────
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

html, body, [class*="css"]  {
  font-family: 'DM Sans', system-ui, sans-serif;
}
h1, h2, h3 { letter-spacing: -0.02em; }
.block-container { padding-top: 1.4rem; max-width: 1400px; }

div[data-testid="stMetric"] {
  background: linear-gradient(145deg, rgba(15,23,42,0.04), rgba(15,23,42,0.01));
  border: 1px solid rgba(148,163,184,0.28);
  border-radius: 14px;
  padding: 14px 16px 10px 16px;
  box-shadow: 0 1px 2px rgba(15,23,42,0.04);
}
div[data-testid="stMetric"] label { font-size: 0.82rem !important; opacity: 0.75; }
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 500;
  font-size: 1.55rem !important;
}
.as-hero {
  display: flex; align-items: baseline; gap: 12px; margin-bottom: 0.4rem;
}
.as-hero h1 { margin: 0; font-size: 1.75rem; font-weight: 700; }
.as-badge {
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 3px 9px; border-radius: 999px;
  border: 1px solid rgba(56,189,248,0.45);
  color: #0ea5e9; background: rgba(14,165,233,0.08);
}
.as-sub { opacity: 0.65; font-size: 0.92rem; margin-bottom: 1.1rem; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_data(ttl=120, show_spinner=False)
def _load_bundle(days: int) -> dict:
    client = get_odoo_client()
    raw = fetch_leads(client, days=days, limit=3000, include_lost=True)
    leads = normalize_leads(raw)
    appointments = fetch_calendar_appointments(client)
    orders = fetch_sale_orders(client, days=days)
    kpis = compute_kpis(leads, appointments)
    return {
        "leads": leads,
        "appointments": appointments,
        "orders": orders,
        "kpis": kpis,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw_count": len(raw),
    }


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _funnel_fig(by_stage: dict[str, int]) -> go.Figure:
    # Preserve a sensible commercial order when present
    preferred = [
        "Primer contacto",
        "Cita/Prueba de manejo",
        "Entra a Credito",
        "Apartado",
        "Won",
    ]
    keys = [k for k in preferred if k in by_stage] + [
        k for k in by_stage if k not in preferred
    ]
    values = [by_stage[k] for k in keys]
    fig = go.Figure(
        go.Funnel(
            y=keys,
            x=values,
            textinfo="value+percent initial",
            marker={
                "color": [
                    "#0ea5e9",
                    "#38bdf8",
                    "#818cf8",
                    "#a78bfa",
                    "#34d399",
                ]
                + ["#94a3b8"] * max(0, len(keys) - 5)
            },
        )
    )
    fig.update_layout(
        margin=dict(l=20, r=20, t=30, b=10),
        height=380,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="DM Sans"),
    )
    return fig


def _bar_eff(df: pd.DataFrame, x: str, title: str) -> go.Figure:
    fig = px.bar(
        df,
        x=x,
        y="efectividad_%",
        color="efectividad_%",
        color_continuous_scale="Tealgrn",
        text="efectividad_%",
        title=title,
    )
    fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
    fig.update_layout(
        margin=dict(l=10, r=10, t=40, b=10),
        height=340,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        coloraxis_showscale=False,
        font=dict(family="DM Sans"),
        yaxis_title="Efectividad %",
        xaxis_title="",
    )
    return fig


# ── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Autosell BI")
    st.caption("Odoo CRM · vivo vía XML-RPC")
    days = st.slider("Ventana (días)", min_value=14, max_value=180, value=90, step=7)
    refresh = st.button("↻ Actualizar datos", use_container_width=True, type="primary")
    if refresh:
        _load_bundle.clear()
    st.divider()
    st.caption("Sucursales: Periférico · San Felipe")
    st.caption("Setter: Marco · Closers RR")

# ── Data load ───────────────────────────────────────────────────────────────
error: str | None = None
bundle: dict | None = None
try:
    with st.spinner("Consultando Odoo CRM…"):
        bundle = _load_bundle(days)
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"

st.markdown(
    '<div class="as-hero"><h1>Gerencia Comercial</h1>'
    '<span class="as-badge">Live Odoo</span></div>'
    '<div class="as-sub">Prospectos, citas, financiamientos y junta semanal — '
    "datos en tiempo real desde autosellmx.odoo.com</div>",
    unsafe_allow_html=True,
)

if error:
    st.error(f"No se pudo conectar a Odoo: {error}")
    st.stop()

assert bundle is not None
leads: list[dict] = bundle["leads"]
kpis: dict = bundle["kpis"]
appointments = bundle["appointments"]
orders = bundle["orders"]
df = pd.DataFrame(leads)

st.caption(
    f"Última sync: {bundle['fetched_at'][:19]}Z · "
    f"{bundle['raw_count']} leads · {len(appointments)} citas calendario · "
    f"{len(orders)} órdenes de venta"
)

tab1, tab2, tab3, tab4 = st.tabs(
    [
        "1 · Dashboard Gerencia",
        "2 · Control Diario",
        "3 · Financiamientos & Perdidos",
        "4 · Junta Semanal",
    ]
)

# ═══════════════════════════════════════════════════════════════════════════
# TAB 1 — KPIs + Funnel + Effectiveness
# ═══════════════════════════════════════════════════════════════════════════
with tab1:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Prospectos", f"{kpis['total_prospectos']:,}")
    c2.metric("Citas Agendadas", f"{kpis['citas_agendadas']:,}")
    c3.metric("Eficiencia de Cierre", f"{kpis['eficiencia_cierre']}%")
    c4.metric("Venta Total Estimada", _money(kpis["venta_total_estimada"]))

    left, right = st.columns([1.15, 1])
    with left:
        st.markdown("#### Embudo de ventas")
        if kpis["by_stage"]:
            st.plotly_chart(
                _funnel_fig(kpis["by_stage"]),
                use_container_width=True,
                theme="streamlit",
            )
        else:
            st.info("Sin etapas con prospectos activos en la ventana.")

    with right:
        st.markdown("#### Efectividad por sucursal")
        branch_df = pd.DataFrame(kpis["branch_eff"])
        if not branch_df.empty:
            st.plotly_chart(
                _bar_eff(branch_df, "sucursal", ""),
                use_container_width=True,
                theme="streamlit",
            )
            st.dataframe(
                branch_df,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("Sin datos de sucursal.")

    st.markdown("#### Efectividad por vendedor")
    rep_df = pd.DataFrame(kpis["rep_eff"])
    if not rep_df.empty:
        top = rep_df.head(12)
        st.plotly_chart(
            _bar_eff(top, "vendedor", ""),
            use_container_width=True,
            theme="streamlit",
        )
        st.dataframe(rep_df, use_container_width=True, hide_index=True)
    else:
        st.info("Sin vendedores en el periodo.")

# ═══════════════════════════════════════════════════════════════════════════
# TAB 2 — Daily prospect control
# ═══════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown("#### Control diario de prospectos & seguimiento")
    if df.empty:
        st.warning("No hay leads en la ventana seleccionada.")
    else:
        f1, f2, f3, f4 = st.columns([1.2, 1, 1, 1.2])
        with f1:
            q = st.text_input(
                "Buscar",
                placeholder="Nombre, teléfono, vehículo…",
            )
        with f2:
            reps = ["(todos)"] + sorted(
                {str(x) for x in df["vendedor"].dropna().unique()}
            )
            rep = st.selectbox("Vendedor", reps)
        with f3:
            branches = ["(todas)"] + sorted(
                {str(x) for x in df["sucursal"].dropna().unique()}
            )
            branch = st.selectbox("Sucursal", branches)
        with f4:
            statuses = ["(todos)"] + sorted(
                {str(x) for x in df["estatus"].dropna().unique()}
            )
            status = st.selectbox("Estatus", statuses)

        view = df.copy()
        if q.strip():
            needle = q.strip().lower()
            mask = (
                view["name"].str.lower().str.contains(needle, na=False)
                | view["contact"].str.lower().str.contains(needle, na=False)
                | view["phone"].astype(str).str.contains(needle, na=False)
                | view["proxima_accion"].str.lower().str.contains(needle, na=False)
            )
            view = view[mask]
        if rep != "(todos)":
            view = view[view["vendedor"] == rep]
        if branch != "(todas)":
            view = view[view["sucursal"] == branch]
        if status != "(todos)":
            view = view[view["estatus"] == status]

        table = view[
            [
                "id",
                "name",
                "contact",
                "phone",
                "vendedor",
                "sucursal",
                "estatus",
                "proxima_accion",
                "activity_deadline",
                "expected_revenue",
                "medium",
                "source",
                "write_date",
            ]
        ].rename(
            columns={
                "id": "ID",
                "name": "Oportunidad",
                "contact": "Cliente",
                "phone": "Teléfono",
                "vendedor": "Vendedor",
                "sucursal": "Sucursal",
                "estatus": "Estatus",
                "proxima_accion": "Próxima acción",
                "activity_deadline": "Fecha acción",
                "expected_revenue": "Monto est.",
                "medium": "Medium",
                "source": "Source",
                "write_date": "Actualizado",
            }
        )
        st.caption(f"{len(table)} registros")
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            height=480,
        )
        csv = table.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Exportar CSV",
            data=csv,
            file_name=f"prospectos_{date.today().isoformat()}.csv",
            mime="text/csv",
        )

# ═══════════════════════════════════════════════════════════════════════════
# TAB 3 — Financing & lost analysis
# ═══════════════════════════════════════════════════════════════════════════
with tab3:
    st.markdown("#### Financiamientos & clientes perdidos")
    a, b, c = st.columns(3)
    a.metric("En crédito / financiamiento", kpis["financiamientos"])
    b.metric("Clientes perdidos", kpis["perdidos"])
    c.metric("Ventas ganadas", kpis["ganados"])

    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown("##### Solicitudes de crédito (etapa)")
        credit = pd.DataFrame(kpis.get("credit_leads") or [])
        if not credit.empty:
            show = credit[
                [
                    "id",
                    "name",
                    "contact",
                    "phone",
                    "vendedor",
                    "sucursal",
                    "expected_revenue",
                    "stage",
                ]
            ].rename(
                columns={
                    "id": "ID",
                    "name": "Oportunidad",
                    "contact": "Cliente",
                    "phone": "Teléfono",
                    "vendedor": "Vendedor",
                    "sucursal": "Sucursal",
                    "expected_revenue": "Monto",
                    "stage": "Etapa",
                }
            )
            st.dataframe(show, use_container_width=True, hide_index=True, height=360)
        else:
            st.info("Sin leads en etapa de crédito en la ventana.")

        st.markdown("##### Órdenes de venta (sale.order)")
        if orders:
            so_df = pd.DataFrame(
                [
                    {
                        "Orden": o.get("name"),
                        "Estado": o.get("state"),
                        "Total": float(o.get("amount_total") or 0),
                        "Cliente": (
                            o.get("partner_id")[1]
                            if isinstance(o.get("partner_id"), (list, tuple))
                            else "—"
                        ),
                        "Fecha": str(o.get("date_order") or "")[:10],
                    }
                    for o in orders[:200]
                ]
            )
            st.dataframe(so_df, use_container_width=True, hide_index=True, height=280)
        else:
            st.caption("Sin sale.order recientes o módulo no accesible.")

    with col_r:
        st.markdown("##### Razones de pérdida")
        reasons = kpis.get("lost_reasons") or {}
        if reasons:
            rdf = pd.DataFrame(
                [{"reason": k, "count": v} for k, v in reasons.items()]
            ).sort_values("count", ascending=False)
            fig = px.pie(
                rdf,
                names="reason",
                values="count",
                hole=0.45,
                color_discrete_sequence=px.colors.sequential.RdBu,
            )
            fig.update_layout(
                margin=dict(l=10, r=10, t=20, b=10),
                height=360,
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(family="DM Sans"),
            )
            st.plotly_chart(fig, use_container_width=True, theme="streamlit")
            st.dataframe(rdf, use_container_width=True, hide_index=True)
        else:
            st.info("Sin leads perdidos con `lost_reason_id` en la ventana.")

# ═══════════════════════════════════════════════════════════════════════════
# TAB 4 — Weekly sales meeting
# ═══════════════════════════════════════════════════════════════════════════
with tab4:
    st.markdown("#### Junta semanal de ventas — acta & compromisos")
    iso = date.today().isocalendar()
    default_week = f"{iso.year}-W{iso.week:02d}"
    week_label = st.text_input("Semana (etiqueta)", value=default_week)
    author = st.text_input("Autor", value="Gerencia Comercial")

    c_left, c_right = st.columns(2)
    with c_left:
        compromisos = st.text_area(
            "Compromisos de la semana",
            height=180,
            placeholder="Ej. Cerrar 8 citas Periférico · Reactivar 5 créditos…",
        )
    with c_right:
        acuerdos = st.text_area(
            "Acuerdos / seguimiento",
            height=180,
            placeholder="Ej. Alfonso da follow-up a Apartados · Veronica revisa SF…",
        )

    b1, b2, b3 = st.columns([1, 1, 1])
    with b1:
        if st.button("Guardar acta", type="primary", use_container_width=True):
            if not compromisos.strip() and not acuerdos.strip():
                st.warning("Escribe al menos un compromiso o acuerdo.")
            else:
                entry = save_junta_note(
                    week_label=week_label,
                    compromisos=compromisos,
                    acuerdos=acuerdos,
                    author=author,
                )
                st.success(f"Acta guardada ({entry['id']})")

    with b2:
        pdf_bytes = build_acta_pdf(
            week_label=week_label,
            kpis=kpis,
            compromisos=compromisos,
            acuerdos=acuerdos,
        )
        st.download_button(
            "Descargar PDF",
            data=pdf_bytes,
            file_name=f"acta_junta_{week_label}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

    with b3:
        # Excel export: KPI summary + commitments
        xbuf = io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
            pd.DataFrame(
                [
                    {
                        "Semana": week_label,
                        "Prospectos": kpis["total_prospectos"],
                        "Citas": kpis["citas_agendadas"],
                        "Eficiencia_%": kpis["eficiencia_cierre"],
                        "Venta_estimada": kpis["venta_total_estimada"],
                        "Ganados": kpis["ganados"],
                        "Perdidos": kpis["perdidos"],
                        "Compromisos": compromisos,
                        "Acuerdos": acuerdos,
                        "Autor": author,
                    }
                ]
            ).to_excel(writer, sheet_name="Acta", index=False)
            pd.DataFrame(kpis["branch_eff"]).to_excel(
                writer, sheet_name="Sucursales", index=False
            )
            pd.DataFrame(kpis["rep_eff"]).to_excel(
                writer, sheet_name="Vendedores", index=False
            )
        st.download_button(
            "Descargar Excel",
            data=xbuf.getvalue(),
            file_name=f"acta_junta_{week_label}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    st.divider()
    st.markdown("##### Histórico de actas")
    history = load_junta_notes()
    if history:
        hist_df = pd.DataFrame(
            [
                {
                    "Semana": n.get("week"),
                    "Autor": n.get("author"),
                    "Creado": str(n.get("created_at") or "")[:19],
                    "Compromisos": (n.get("compromisos") or "")[:120],
                    "Acuerdos": (n.get("acuerdos") or "")[:120],
                }
                for n in history
            ]
        )
        st.dataframe(hist_df, use_container_width=True, hide_index=True)
    else:
        st.caption("Aún no hay actas guardadas en `data/junta_semanal.json`.")
