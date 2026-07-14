from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "processed" / "tbb.db"

SOURCE_LABELS = {
    "mali_bunye": "Mali Bünye",
    "aktifler": "Aktifler",
    "pasifler": "Pasifler",
    "gelir_gider": "Gelir – Gider",
    "nazim": "Nazım Hesaplar",
}
ENTITY_LABELS = {"bank": "Bankalar", "group": "Banka Grupları", "sector": "Sektör"}


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as connection:
        return pd.read_sql_query(sql, connection, params=params)


@st.cache_data(show_spinner=False)
def load_catalog() -> pd.DataFrame:
    return query(
        """
        SELECT source_group, sheet_name, sheet_key, report_title,
               metric_path, metric_key, unit
        FROM observations
        GROUP BY source_group, sheet_name, sheet_key, report_title,
                 metric_path, metric_key, unit
        ORDER BY source_group, sheet_name, source_col
        """
    )


@st.cache_data(show_spinner=False)
def load_series(metric_key: str, entity_type: str) -> pd.DataFrame:
    return query(
        """
        SELECT period_end, period_label, entity_name, value, unit
        FROM observations
        WHERE metric_key = ? AND entity_type = ?
        ORDER BY period_end, entity_name
        """,
        (metric_key, entity_type),
    )


@st.cache_data(show_spinner=False)
def load_quality() -> tuple[pd.DataFrame, pd.DataFrame]:
    periods = query(
        """
        SELECT period_end, COUNT(*) AS observations,
               COUNT(DISTINCT entity_key) AS entities,
               COUNT(DISTINCT metric_key) AS metrics
        FROM observations
        GROUP BY period_end ORDER BY period_end
        """
    )
    schema = query(
        """
        SELECT status, COUNT(*) AS pages
        FROM schema_audit GROUP BY status ORDER BY status
        """
    )
    return periods, schema


def number_tr(value: float, decimals: int = 0) -> str:
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def formatted_value(value: float | None, unit: str) -> str:
    if value is None or pd.isna(value):
        return "—"
    if unit == "%":
        return f"%{number_tr(value, 2)}"
    return f"{number_tr(value)} mn TL"


def percentage_change(start_value: float | None, end_value: float | None) -> float | None:
    if start_value is None or end_value is None:
        return None
    if pd.isna(start_value) or pd.isna(end_value) or start_value == 0:
        return None
    return (end_value / start_value - 1) * 100


st.set_page_config(page_title="TBB Banka Analizi", page_icon="🏦", layout="wide")
st.markdown(
    """
    <style>
    .stApp { background: #f6f8fb; }
    [data-testid="stSidebar"] { background: #0b1f3a; }
    [data-testid="stSidebar"] * { color: #f8fafc; }
    [data-testid="stMetric"] {
        background: white; border: 1px solid #e4e9f1; border-radius: 14px;
        padding: 18px 20px; box-shadow: 0 4px 16px rgba(11,31,58,.05);
    }
    h1, h2, h3 { color: #0b1f3a; }
    .subtle { color: #607089; margin-top: -10px; margin-bottom: 20px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("TBB Banka Analiz Paneli")
st.markdown(
    '<div class="subtle">Türkiye Bankalar Birliği solo banka verileri • '
    "Mart 2020’den itibaren çeyreklik analiz</div>",
    unsafe_allow_html=True,
)

if not DB_PATH.exists():
    st.error("Veritabanı bulunamadı. Önce veri indirme ve yükleme adımlarını çalıştırın.")
    st.code("python3 -m src.tbb_dashboard.download\npython3 -m src.tbb_dashboard.ingest")
    st.stop()

catalog = load_catalog()

with st.sidebar:
    st.header("Analiz filtreleri")
    if st.button("Veritabanını yenile"):
        st.cache_data.clear()
        st.rerun()
    sources = list(SOURCE_LABELS)
    source = st.selectbox(
        "Rapor grubu",
        sources,
        index=sources.index("aktifler"),
        format_func=lambda item: SOURCE_LABELS[item],
    )
    source_catalog = catalog[catalog["source_group"] == source]
    sheet_options = source_catalog["sheet_key"].drop_duplicates().tolist()
    default_sheet = "varliklar" if "varliklar" in sheet_options else sheet_options[0]
    sheet_lookup = (
        source_catalog.drop_duplicates("sheet_key").set_index("sheet_key")["sheet_name"].to_dict()
    )
    sheet = st.selectbox(
        "Tablo",
        sheet_options,
        index=sheet_options.index(default_sheet),
        format_func=lambda item: sheet_lookup[item],
    )
    metric_catalog = source_catalog[source_catalog["sheet_key"] == sheet]
    metric_options = metric_catalog["metric_key"].drop_duplicates().tolist()
    preferred = f"{source}.{sheet}.toplam_aktifler"
    default_metric = preferred if preferred in metric_options else metric_options[0]
    metric_lookup = (
        metric_catalog.drop_duplicates("metric_key").set_index("metric_key")["metric_path"].to_dict()
    )
    metric_key = st.selectbox(
        "Finansal metrik",
        metric_options,
        index=metric_options.index(default_metric),
        format_func=lambda item: metric_lookup[item],
    )
    entity_type = st.radio(
        "Karşılaştırma düzeyi",
        list(ENTITY_LABELS),
        format_func=lambda item: ENTITY_LABELS[item],
    )

data = load_series(metric_key, entity_type)
if data.empty:
    st.warning("Bu filtreler için gösterilecek veri bulunamadı.")
    st.stop()

data["period_end"] = pd.to_datetime(data["period_end"])
unit = data["unit"].mode().iloc[0]
periods = data[["period_end", "period_label"]].drop_duplicates().sort_values("period_end")
period_dates = periods["period_end"].tolist()
period_labels = dict(zip(periods["period_end"], periods["period_label"]))

with st.sidebar:
    st.subheader("Dönem karşılaştırması")
    start_date = st.selectbox(
        "Başlangıç dönemi (nereden)",
        period_dates[:-1],
        index=0,
        format_func=lambda item: period_labels[item],
    )
    end_options = [item for item in period_dates if item > start_date]
    end_date = st.selectbox(
        "Bitiş dönemi (nereye)",
        end_options,
        index=len(end_options) - 1,
        format_func=lambda item: period_labels[item],
    )
    latest_all = data[data["period_end"] == end_date].sort_values("value", ascending=False)
    all_entities = sorted(data["entity_name"].unique())
    default_entities = latest_all["entity_name"].head(5).tolist()
    entity_selection_labels = {
        "bank": "Gösterilecek bankalar",
        "group": "Gösterilecek banka grupları",
        "sector": "Gösterilecek sektör toplamları",
    }
    select_all_entities = st.checkbox(
        "Tüm bankaları/kurumları seç",
        key=f"select_all_{entity_type}",
    )
    if select_all_entities:
        entities = all_entities
        st.success(f"{len(entities)} banka/kurum seçildi.")
    else:
        entities = st.multiselect(
            entity_selection_labels[entity_type],
            all_entities,
            default=default_entities,
            placeholder="Kutuyu açıp banka/kurum seçin",
            help="İstediğiniz sayıda banka veya kurum seçebilirsiniz.",
        )
        st.caption(f"{len(entities)} banka/kurum seçildi. Seçim sınırı yoktur.")
    chart_type = st.radio(
        "Grafik türü",
        ["Çizgi", "Sütun", "Daire"],
        horizontal=True,
    )

filtered = data[
    data["entity_name"].isin(entities)
    & data["period_end"].between(start_date, end_date)
].copy()
visible_periods = [item for item in period_dates if start_date <= item <= end_date]

metric_name = metric_lookup[metric_key]
sheet_title = metric_catalog["report_title"].iloc[0]
st.caption(f"{SOURCE_LABELS[source]} / {sheet_lookup[sheet]} • {metric_name}")

calculator_catalog = catalog.drop_duplicates("metric_key").copy()
calculator_catalog["display_name"] = calculator_catalog.apply(
    lambda row: (
        f"{SOURCE_LABELS.get(row['source_group'], row['source_group'])} / "
        f"{row['sheet_name']} / {row['metric_path']}"
    ),
    axis=1,
)
calculator_catalog = calculator_catalog.sort_values("display_name")
calculator_options = calculator_catalog["metric_key"].tolist()
calculator_lookup = calculator_catalog.set_index("metric_key")["display_name"].to_dict()

primary_name = entities[0] if entities else None
primary = (
    filtered[filtered["entity_name"] == primary_name].sort_values("period_end")
    if primary_name
    else pd.DataFrame()
)
values = primary["value"] if not primary.empty else pd.Series(dtype=float)
start_rows = primary[primary["period_end"] == start_date]
end_rows = primary[primary["period_end"] == end_date]
start_value = start_rows["value"].iloc[0] if not start_rows.empty else None
end_value = end_rows["value"].iloc[0] if not end_rows.empty else None
period_change = percentage_change(start_value, end_value)

kpi1, kpi2, kpi3, kpi4 = st.columns(4)
kpi1.metric("Seçili banka/kurum", primary_name or "Seçim yapılmadı")
kpi2.metric(period_labels[start_date], formatted_value(start_value, unit))
kpi3.metric(period_labels[end_date], formatted_value(end_value, unit))
kpi4.metric(
    "Dönemler arası değişim",
    "—" if period_change is None else f"%{number_tr(period_change, 1)}",
)

comparison_tab, calculator_tab, trend_tab, ranking_tab, table_tab, quality_tab = st.tabs(
    [
        "Dönem karşılaştırması",
        "Metrik hesaplayıcı",
        "Zaman içindeki gelişim",
        "Bitiş dönemi sıralaması",
        "Veri tablosu",
        "Veri kalitesi",
    ]
)

with comparison_tab:
    if not entities:
        st.info("Karşılaştırma için en az bir banka veya kurum seçin.")
    else:
        endpoints = data[
            data["entity_name"].isin(entities)
            & data["period_end"].isin([start_date, end_date])
        ].copy()
        if chart_type == "Daire":
            start_pie, end_pie = st.columns(2)
            for column, period in ((start_pie, start_date), (end_pie, end_date)):
                snapshot = endpoints[endpoints["period_end"] == period].copy()
                snapshot["pie_value"] = snapshot["value"].abs()
                pie_figure = px.pie(
                    snapshot,
                    names="entity_name",
                    values="pie_value",
                    hole=0.38,
                    title=period_labels[period],
                    color_discrete_sequence=px.colors.qualitative.Safe,
                )
                pie_figure.update_layout(
                    height=480,
                    margin=dict(l=10, r=10, t=55, b=10),
                    legend_title_text="",
                )
                column.plotly_chart(pie_figure, width="stretch")
            st.caption("Daire grafiklerde dağılım, değerlerin mutlak büyüklüğüyle gösterilir.")
        else:
            comparison_chart_options = dict(
                data_frame=endpoints,
                x="period_end" if chart_type == "Çizgi" else "entity_name",
                y="value",
                color="entity_name" if chart_type == "Çizgi" else "period_label",
                labels={
                    "entity_name": "Banka / kurum",
                    "period_end": "Dönem",
                    "value": unit,
                    "period_label": "Dönem",
                },
                color_discrete_sequence=[
                    "#0f766e",
                    "#2563eb",
                    "#d97706",
                    "#7c3aed",
                    "#dc2626",
                    "#94a3b8",
                ],
            )
            if chart_type == "Çizgi":
                comparison_figure = px.line(**comparison_chart_options, markers=True)
            else:
                comparison_figure = px.bar(**comparison_chart_options, barmode="group")
            comparison_figure.update_layout(
                height=500,
                margin=dict(l=10, r=10, t=30, b=10),
                legend_title_text="",
                hovermode="x unified" if chart_type == "Çizgi" else "closest",
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            if chart_type == "Çizgi":
                comparison_figure.update_xaxes(
                    tickvals=[start_date, end_date],
                    ticktext=[period_labels[start_date], period_labels[end_date]],
                )
            else:
                comparison_figure.update_xaxes(tickangle=-25)
            st.plotly_chart(comparison_figure, width="stretch")

        comparison = endpoints.pivot_table(
            index="entity_name",
            columns="period_end",
            values="value",
            aggfunc="first",
        ).reindex(entities)
        for endpoint in (start_date, end_date):
            if endpoint not in comparison.columns:
                comparison[endpoint] = pd.NA
        comparison_table = pd.DataFrame(
            {
                "Banka / kurum": comparison.index,
                period_labels[start_date]: comparison[start_date].values,
                period_labels[end_date]: comparison[end_date].values,
            }
        )
        comparison_table["Tutar değişimi"] = (
            comparison_table[period_labels[end_date]]
            - comparison_table[period_labels[start_date]]
        )
        comparison_table["Değişim (%)"] = (
            comparison_table[period_labels[end_date]]
            .div(comparison_table[period_labels[start_date]])
            .sub(1)
            .mul(100)
        )
        st.dataframe(
            comparison_table,
            width="stretch",
            hide_index=True,
            column_config={
                "Değişim (%)": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )

with calculator_tab:
    st.subheader("Metrik hesaplayıcı")
    st.caption(
        "Farklı rapor ve tablolardan iki metrik seçerek oran veya fark hesaplayın. "
        "Hesaplama, sol menüde seçilen banka/kurum ve dönemlere uygulanır."
    )
    metric_a = st.selectbox(
        "Metrik A",
        calculator_options,
        index=calculator_options.index(metric_key),
        format_func=lambda item: calculator_lookup[item],
        key="calculator_metric_a",
    )
    preferred_b = "pasifler.yukumlulukler.toplam_yukumlulukler"
    default_b = preferred_b if preferred_b in calculator_options else calculator_options[0]
    metric_b = st.selectbox(
        "Metrik B",
        calculator_options,
        index=calculator_options.index(default_b),
        format_func=lambda item: calculator_lookup[item],
        key="calculator_metric_b",
    )
    operation = st.radio(
        "Hesaplama",
        ["Oran (A / B)", "Yüzde oranı (A / B × 100)", "Fark (A − B)"],
        horizontal=True,
        key="calculator_operation",
    )

    metric_a_data = load_series(metric_a, entity_type).rename(
        columns={"value": "metric_a_value", "unit": "metric_a_unit"}
    )
    metric_b_data = load_series(metric_b, entity_type).rename(
        columns={"value": "metric_b_value", "unit": "metric_b_unit"}
    )
    metric_a_data["period_end"] = pd.to_datetime(metric_a_data["period_end"])
    metric_b_data["period_end"] = pd.to_datetime(metric_b_data["period_end"])
    calculation = metric_a_data.merge(
        metric_b_data[
            ["period_end", "entity_name", "metric_b_value", "metric_b_unit"]
        ],
        on=["period_end", "entity_name"],
        how="inner",
    )
    calculation = calculation[
        calculation["entity_name"].isin(entities)
        & calculation["period_end"].between(start_date, end_date)
    ].copy()

    if calculation.empty:
        st.info("Seçili metrik, banka/kurum ve dönemler için ortak veri bulunamadı.")
    else:
        zero_denominators = int((calculation["metric_b_value"] == 0).sum())
        if operation == "Oran (A / B)":
            calculation = calculation[calculation["metric_b_value"] != 0].copy()
            calculation["result"] = (
                calculation["metric_a_value"] / calculation["metric_b_value"]
            )
            result_label = "A / B oranı"
            result_unit = "oran"
        elif operation == "Yüzde oranı (A / B × 100)":
            calculation = calculation[calculation["metric_b_value"] != 0].copy()
            calculation["result"] = (
                calculation["metric_a_value"] / calculation["metric_b_value"] * 100
            )
            result_label = "A / B (%)"
            result_unit = "%"
        else:
            calculation["result"] = (
                calculation["metric_a_value"] - calculation["metric_b_value"]
            )
            result_label = "A − B farkı"
            units_a = calculation["metric_a_unit"].dropna().unique()
            units_b = calculation["metric_b_unit"].dropna().unique()
            result_unit = units_a[0] if len(units_a) == 1 and set(units_a) == set(units_b) else "fark"

        if zero_denominators and operation != "Fark (A − B)":
            st.warning(
                f"Metrik B değeri sıfır olan {zero_denominators} satır oran hesabına alınmadı."
            )

        if calculation.empty:
            st.info("Sıfır paydalar çıkarıldıktan sonra hesaplanabilir satır kalmadı.")
        else:
            st.markdown(
                f"**A:** {calculator_lookup[metric_a]}  \n"
                f"**B:** {calculator_lookup[metric_b]}"
            )
            calculator_chart_options = dict(
                data_frame=calculation,
                x="period_end",
                y="result",
                color="entity_name",
                labels={
                    "period_end": "Dönem",
                    "result": result_unit,
                    "entity_name": "Banka / kurum",
                },
                color_discrete_sequence=[
                    "#0f766e",
                    "#2563eb",
                    "#d97706",
                    "#7c3aed",
                    "#dc2626",
                    "#0891b2",
                ],
            )
            if chart_type == "Çizgi":
                calculator_figure = px.line(**calculator_chart_options, markers=True)
            elif chart_type == "Sütun":
                calculator_figure = px.bar(**calculator_chart_options, barmode="group")
            else:
                calculator_snapshot = calculation[
                    calculation["period_end"] == end_date
                ].copy()
                calculator_snapshot["pie_value"] = calculator_snapshot["result"].abs()
                calculator_figure = px.pie(
                    calculator_snapshot,
                    names="entity_name",
                    values="pie_value",
                    hole=0.38,
                    title=f"{result_label} • {period_labels[end_date]}",
                    color_discrete_sequence=px.colors.qualitative.Safe,
                )
            calculator_figure.update_layout(
                height=500,
                margin=dict(l=10, r=10, t=45, b=10),
                legend_title_text="",
                hovermode="x unified" if chart_type == "Çizgi" else "closest",
                plot_bgcolor="white",
                paper_bgcolor="white",
                title=result_label if chart_type != "Daire" else None,
            )
            if chart_type != "Daire":
                calculator_figure.update_xaxes(
                    tickvals=visible_periods,
                    ticktext=[period_labels[item] for item in visible_periods],
                )
            else:
                st.caption(
                    "Daire grafik, bitiş dönemindeki hesaplama sonuçlarının "
                    "mutlak büyüklük dağılımını gösterir."
                )
            st.plotly_chart(calculator_figure, width="stretch")

            calculator_table = calculation[
                [
                    "period_label",
                    "entity_name",
                    "metric_a_value",
                    "metric_b_value",
                    "result",
                ]
            ].rename(
                columns={
                    "period_label": "Dönem",
                    "entity_name": "Banka / kurum",
                    "metric_a_value": "Metrik A",
                    "metric_b_value": "Metrik B",
                    "result": result_label,
                }
            )
            st.dataframe(calculator_table, width="stretch", hide_index=True)
            st.download_button(
                "Hesaplama sonucunu CSV indir",
                calculator_table.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_metrik_hesaplama.csv",
                mime="text/csv",
            )

with trend_tab:
    if filtered.empty:
        st.info("Grafik için en az bir kurum seçin.")
    else:
        chart_options = dict(
            data_frame=filtered,
            x="period_end",
            y="value",
            color="entity_name",
            labels={"period_end": "Dönem", "value": unit, "entity_name": "Kurum"},
            color_discrete_sequence=["#0f766e", "#2563eb", "#d97706", "#7c3aed", "#dc2626"],
        )
        if chart_type == "Çizgi":
            figure = px.line(**chart_options, markers=True)
        elif chart_type == "Sütun":
            figure = px.bar(**chart_options, barmode="group")
        else:
            trend_snapshot = filtered[filtered["period_end"] == end_date].copy()
            trend_snapshot["pie_value"] = trend_snapshot["value"].abs()
            figure = px.pie(
                trend_snapshot,
                names="entity_name",
                values="pie_value",
                hole=0.38,
                title=f"{period_labels[end_date]} dağılımı",
                color_discrete_sequence=px.colors.qualitative.Safe,
            )
        figure.update_layout(
            height=480,
            margin=dict(l=10, r=10, t=30, b=10),
            legend_title_text="",
            hovermode="x unified" if chart_type == "Çizgi" else "closest",
            plot_bgcolor="white",
            paper_bgcolor="white",
        )
        if chart_type != "Daire":
            figure.update_xaxes(
                tickvals=visible_periods,
                ticktext=[period_labels[item] for item in visible_periods],
            )
        else:
            st.caption(
                "Daire grafik, bitiş dönemindeki değerlerin mutlak büyüklük dağılımını gösterir."
            )
        st.plotly_chart(figure, width="stretch")

with ranking_tab:
    ranking = data[data["period_end"] == end_date].nlargest(15, "value").sort_values("value")
    if chart_type == "Daire":
        ranking = ranking.copy()
        ranking["pie_value"] = ranking["value"].abs()
        rank_figure = px.pie(
            ranking,
            names="entity_name",
            values="pie_value",
            hole=0.38,
            color_discrete_sequence=px.colors.qualitative.Safe,
        )
    elif chart_type == "Çizgi":
        rank_figure = px.line(
            ranking.sort_values("value", ascending=False),
            x="entity_name",
            y="value",
            markers=True,
            labels={"value": unit, "entity_name": ""},
            color_discrete_sequence=["#0f766e"],
        )
    else:
        rank_figure = px.bar(
            ranking,
            x="value",
            y="entity_name",
            orientation="h",
            labels={"value": unit, "entity_name": ""},
            color="value",
            color_continuous_scale=["#bfe8e3", "#0f766e"],
        )
    rank_figure.update_layout(
        height=560,
        margin=dict(l=10, r=10, t=30, b=10),
        coloraxis_showscale=False,
        plot_bgcolor="white",
        paper_bgcolor="white",
        title=f"{period_labels[end_date]} • En yüksek 15",
    )
    if chart_type == "Çizgi":
        rank_figure.update_xaxes(tickangle=-25)
    st.plotly_chart(rank_figure, width="stretch")

with table_tab:
    export = filtered[["period_label", "entity_name", "value", "unit"]].rename(
        columns={
            "period_label": "Dönem",
            "entity_name": "Kurum",
            "value": "Değer",
            "unit": "Birim",
        }
    )
    st.dataframe(export, width="stretch", hide_index=True)
    st.download_button(
        "Filtrelenmiş veriyi CSV indir",
        export.to_csv(index=False).encode("utf-8-sig"),
        file_name="tbb_filtrelenmis_veri.csv",
        mime="text/csv",
    )

with quality_tab:
    period_quality, schema_quality = load_quality()
    q1, q2, q3 = st.columns(3)
    q1.metric("Toplam gözlem", number_tr(period_quality["observations"].sum()))
    q2.metric("Yüklenen dönem", str(len(period_quality)))
    missing = int(schema_quality.loc[schema_quality["status"] == "missing", "pages"].sum())
    q3.metric("Gerçek eksik sayfa", str(missing))
    period_quality["period_end"] = pd.to_datetime(period_quality["period_end"])
    quality_figure = px.bar(
        period_quality,
        x="period_end",
        y="observations",
        labels={"period_end": "Dönem", "observations": "Gözlem sayısı"},
        color_discrete_sequence=["#2563eb"],
    )
    quality_figure.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(quality_figure, width="stretch")
    with st.expander("Kaynak tablo durumları"):
        st.dataframe(schema_quality, width="stretch", hide_index=True)
    st.caption(sheet_title)
