from __future__ import annotations

import ast
import hashlib
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Streamlit Community Cloud runs this file from its own directory, so the
# repository root is not guaranteed to be on Python's module search path.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tbb_dashboard.labels import SHEET_LABELS, metric_display_label
from src.tbb_dashboard.ingest import ensure_database


RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "processed" / "tbb.db"

SOURCE_LABELS = {
    "mali_bunye": "Mali Bünye",
    "aktifler": "Aktifler",
    "pasifler": "Pasifler",
    "gelir_gider": "Gelir – Gider",
    "nazim": "Nazım Hesaplar",
}
ENTITY_LABELS = {"bank": "Bankalar", "group": "Banka Grupları"}
COLORS = ["#2563EB", "#7C3AED", "#059669"]
CONTINUOUS_COLORS = ["#2563EB", "#7C3AED", "#059669"]
SOURCE_AVAILABILITY_NOTES = {
    ("pasifler", "ser_benz", "summary_available"): (
        "Ayrıntılı Ser.Benz. sayfası kaynak dönemde yayımlanmamış; toplam değer "
        "Yükümlülükler → Diğer Pasifler → Sermaye Benzeri Borçlanma Araçları "
        "metriğinde mevcuttur."
    ),
}

EQUITY_METRIC = "mali_bunye.sermaye_std_orani.ozkaynak_milyon_tl"
CAPITAL_ADEQUACY_METRIC = (
    "mali_bunye.sermaye_std_orani.sermaye_yeterliligi_orani"
)
GENERAL_SIZE_METRIC = "aktifler.varliklar.toplam_aktifler"
SYSTEMIC_BANK_GROUPS = (
    ("Türkiye Cumhuriyeti Ziraat Bankası A.Ş.",),
    ("Türkiye Halk Bankası A.Ş.",),
    ("Türkiye Vakıflar Bankası T.A.O.",),
    ("Akbank T.A.Ş.",),
    ("Türkiye Garanti Bankası A.Ş.",),
    ("Türkiye İş Bankası A.Ş.",),
    ("Yapı ve Kredi Bankası A.Ş.",),
    ("QNB Bank A.Ş.", "QNB Finansbank A.Ş."),
    ("Denizbank A.Ş.",),
)
READY_BANK_FILTERS = (
    "İlk 10 (seçili metrik)",
    "İlk 15 (seçili metrik)",
    "İlk 20 (seçili metrik)",
    "İlk 25 (seçili metrik)",
    "İlk 10 (genel banka büyüklüğü)",
    "İlk 15 (genel banka büyüklüğü)",
    "İlk 20 (genel banka büyüklüğü)",
    "İlk 25 (genel banka büyüklüğü)",
)


def systemic_entities(all_entities: list[str]) -> list[str]:
    """Return one available name for each systemic bank, in a stable order."""
    available = set(all_entities)
    return [
        match
        for aliases in SYSTEMIC_BANK_GROUPS
        if (match := next((name for name in aliases if name in available), None))
    ]


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
def load_schema_availability(source_group: str, sheet_key: str) -> pd.DataFrame:
    return query(
        """
        SELECT audit.period_end, MAX(obs.period_label) AS period_label, audit.status
        FROM schema_audit AS audit
        LEFT JOIN observations AS obs ON obs.period_end = audit.period_end
        WHERE audit.source_group = ? AND audit.sheet_key = ?
        GROUP BY audit.period_end, audit.status
        ORDER BY audit.period_end
        """,
        (source_group, sheet_key),
    )


def number_tr(value: float, decimals: int = 0) -> str:
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


class FormulaError(ValueError):
    pass


def evaluate_formula(
    expression: str,
    values: dict[str, pd.Series],
) -> tuple[pd.Series, int, set[str]]:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError("Formülün yazımı geçerli değil.") from exc

    zero_denominators = 0
    used_symbols: set[str] = set()

    def visit(node):
        nonlocal zero_denominators
        if isinstance(node, ast.Name):
            symbol = node.id.upper()
            if symbol not in values:
                raise FormulaError(f"{symbol} metriği seçili değil.")
            used_symbols.add(symbol)
            return values[symbol]
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = visit(node.operand)
            return operand if isinstance(node.op, ast.UAdd) else -operand
        if isinstance(node, ast.BinOp) and isinstance(
            node.op,
            (ast.Add, ast.Sub, ast.Mult, ast.Div),
        ):
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(right, pd.Series):
                zero_denominators += int((right == 0).sum())
                right = right.mask(right == 0)
            elif right == 0:
                raise FormulaError("Sıfıra bölme yapılamaz.")
            return left / right
        raise FormulaError("Yalnızca A–H, sayılar, parantez ve + − * / kullanılabilir.")

    result = visit(tree.body)
    if not used_symbols:
        raise FormulaError("Formülde en az bir metrik harfi kullanın.")
    if not isinstance(result, pd.Series):
        first_series = next(iter(values.values()))
        result = pd.Series(result, index=first_series.index)
    result = pd.to_numeric(result, errors="coerce").replace(
        [float("inf"), float("-inf")], pd.NA
    )
    return result, zero_denominators, used_symbols


def reset_filter_dependents(namespace: str) -> None:
    st.session_state[f"{namespace}_sheet"] = None
    st.session_state[f"{namespace}_metric"] = None


def reset_metric(namespace: str) -> None:
    st.session_state[f"{namespace}_metric"] = None
    st.session_state.pop(f"{namespace}_chart_type", None)


def reset_chart(namespace: str) -> None:
    st.session_state.pop(f"{namespace}_chart_type", None)


def reset_simulation_scope() -> None:
    for key in (
        "simulation_start_period",
        "simulation_end_period",
        "simulation_entity",
    ):
        st.session_state.pop(key, None)


def reset_simulation_end() -> None:
    st.session_state.pop("simulation_end_period", None)


def sync_entity_filter(
    selection_key: str,
    checkbox_key: str,
    entity_name: str,
) -> None:
    selected = set(st.session_state.get(selection_key, []))
    if st.session_state.get(checkbox_key, False):
        selected.add(entity_name)
    else:
        selected.discard(entity_name)
    st.session_state[selection_key] = list(selected)


def render_metric_filters(
    namespace: str,
    catalog: pd.DataFrame,
    default_source: str = "mali_bunye",
    default_sheet_key: str = "sermaye_std_orani",
    default_metric_key: str = EQUITY_METRIC,
) -> dict | None:
    with st.container(border=True):
        st.markdown("#### Analiz filtreleri")
        source_col, sheet_col, metric_col, level_col = st.columns([1, 1, 2, 1])
        sources = list(SOURCE_LABELS)
        source = source_col.selectbox(
            "Rapor grubu",
            sources,
            index=sources.index(default_source),
            format_func=lambda item: SOURCE_LABELS[item],
            key=f"{namespace}_source",
            on_change=reset_filter_dependents,
            args=(namespace,),
        )
        source_catalog = catalog[catalog["source_group"] == source]
        sheet_options = source_catalog["sheet_key"].drop_duplicates().tolist()
        default_sheet = (
            default_sheet_key
            if default_sheet_key in sheet_options
            else ("varliklar" if "varliklar" in sheet_options else sheet_options[0])
        )
        sheet_lookup = (
            source_catalog.drop_duplicates("sheet_key")
            .set_index("sheet_key")["sheet_name"]
            .to_dict()
        )
        sheet_lookup = {
            key: SHEET_LABELS.get((source, key), original_name)
            for key, original_name in sheet_lookup.items()
        }
        sheet_state_key = f"{namespace}_sheet"
        if (
            sheet_state_key in st.session_state
            and st.session_state[sheet_state_key] is not None
            and st.session_state[sheet_state_key] not in sheet_options
        ):
            st.session_state[sheet_state_key] = None
        sheet_index = (
            sheet_options.index(default_sheet)
            if sheet_state_key not in st.session_state
            else None
        )
        sheet = sheet_col.selectbox(
            "Sayfa adı",
            sheet_options,
            index=sheet_index,
            format_func=lambda item: sheet_lookup[item],
            key=sheet_state_key,
            on_change=reset_metric,
            args=(namespace,),
            placeholder="Veri seçiniz",
        )
        metric_state_key = f"{namespace}_metric"
        metric_catalog = source_catalog.iloc[0:0].copy()
        metric_lookup: dict[str, str] = {}
        if sheet is None:
            st.session_state[metric_state_key] = None
            metric_key = metric_col.selectbox(
                "Finansal metrik",
                [],
                index=None,
                key=metric_state_key,
                placeholder="Veri seçiniz",
                disabled=True,
            )
        else:
            metric_catalog = source_catalog[source_catalog["sheet_key"] == sheet]
            metric_options = metric_catalog["metric_key"].drop_duplicates().tolist()
            preferred = (
                default_metric_key
                if default_metric_key in metric_options
                else f"{source}.{sheet}.toplam_aktifler"
            )
            default_metric = (
                preferred if preferred in metric_options else metric_options[0]
            )
            metric_lookup = (
                metric_catalog.drop_duplicates("metric_key")
                .set_index("metric_key")["metric_path"]
                .to_dict()
            )
            metric_lookup = {
                key: metric_display_label(key, original_name)
                for key, original_name in metric_lookup.items()
            }
            if (
                metric_state_key in st.session_state
                and st.session_state[metric_state_key] is not None
                and st.session_state[metric_state_key] not in metric_options
            ):
                st.session_state[metric_state_key] = None
            metric_index = (
                metric_options.index(default_metric)
                if metric_state_key not in st.session_state
                else None
            )
            metric_key = metric_col.selectbox(
                "Finansal metrik",
                metric_options,
                index=metric_index,
                format_func=lambda item: metric_lookup[item],
                key=metric_state_key,
                placeholder="Veri seçiniz",
                on_change=reset_chart,
                args=(namespace,),
            )
        entity_type = level_col.radio(
            "Karşılaştırma düzeyi",
            list(ENTITY_LABELS),
            format_func=lambda item: ENTITY_LABELS[item],
            horizontal=True,
            key=f"{namespace}_entity_type",
        )
    if sheet is None:
        st.info("Devam etmek için sayfa adı seçin.")
        return None
    if metric_key is None:
        st.info("Devam etmek için finansal metrik seçin.")
        return None
    data = load_series(metric_key, entity_type)
    if data.empty:
        st.warning("Bu filtreler için gösterilecek veri bulunamadı.")
        return None
    data["period_end"] = pd.to_datetime(data["period_end"])
    periods = data[["period_end", "period_label"]].drop_duplicates().sort_values(
        "period_end"
    )
    period_dates = periods["period_end"].tolist()
    schema_availability = load_schema_availability(source, sheet)
    return {
        "source": source,
        "sheet": sheet,
        "sheet_name": sheet_lookup[sheet],
        "metric_key": metric_key,
        "metric_name": metric_lookup[metric_key],
        "report_title": metric_catalog["report_title"].iloc[0],
        "entity_type": entity_type,
        "data": data,
        "unit": data["unit"].mode().iloc[0],
        "period_dates": period_dates,
        "period_labels": dict(zip(periods["period_end"], periods["period_label"])),
        "all_entities": sorted(data["entity_name"].dropna().unique()),
        "schema_availability": schema_availability,
    }


def render_chart_selector(namespace: str, default: str = "Çizgi") -> str:
    options = ["Çizgi", "Sütun", "Daire"]
    return st.radio(
        "Grafik türü",
        options,
        index=options.index(default),
        horizontal=True,
        key=f"{namespace}_chart_type",
    )


def render_entity_filter(
    context: dict,
    namespace: str,
    period: pd.Timestamp,
    default_count: int = 5,
    exact_count: int | None = None,
    default_selection: str = "all",
) -> list[str]:
    data = context["data"]
    all_entities = context["all_entities"]
    metric_key = context["metric_key"]
    entity_type = context["entity_type"]
    snapshot = data[data["period_end"] == period].sort_values("value", ascending=False)
    value_order = snapshot["entity_name"].drop_duplicates().tolist()
    value_order.extend(name for name in all_entities if name not in set(value_order))
    ascending_value_order = (
        snapshot.sort_values("value", ascending=True)["entity_name"]
        .drop_duplicates()
        .tolist()
    )
    ascending_value_order.extend(
        name for name in all_entities if name not in set(ascending_value_order)
    )
    if default_selection == "all":
        default_entities = all_entities
    elif default_selection == "halkbank":
        default_entities = [
            name for name in all_entities if name == "Türkiye Halk Bankası A.Ş."
        ]
    else:
        default_entities = value_order[:default_count]
    selection_key = f"{namespace}_entity_selection_{entity_type}"
    legacy_selection_key = (
        f"{namespace}_entity_selection_{entity_type}_{metric_key}"
    )
    if selection_key not in st.session_state:
        st.session_state[selection_key] = list(
            st.session_state.get(legacy_selection_key, default_entities)
        )
    st.session_state[selection_key] = list(
        dict.fromkeys(st.session_state[selection_key])
    )

    def checkbox_key(entity_name: str) -> str:
        entity_id = hashlib.sha1(entity_name.encode("utf-8")).hexdigest()[:12]
        return f"{namespace}_entity_checkbox_{entity_type}_{entity_id}"

    selected_count = len(st.session_state[selection_key])
    label = f"Banka/kurum filtresi • {selected_count} seçili"
    with st.popover(label, use_container_width=True):
        st.markdown("**Banka/kurum seçimi**")
        search = st.text_input(
            "Ara",
            placeholder="Banka veya kurum adını yazın",
            key=f"{namespace}_entity_search_{entity_type}",
        )
        sort_choice = st.selectbox(
            "Sırala",
            [
                "Değere göre (yüksekten düşüğe)",
                "Değere göre (düşükten yükseğe)",
                "A–Z",
                "Z–A",
            ],
            key=f"{namespace}_entity_sort_{entity_type}",
        )
        size_data = load_series(GENERAL_SIZE_METRIC, entity_type)
        if not size_data.empty:
            size_data["period_end"] = pd.to_datetime(size_data["period_end"])
            size_snapshot = size_data[size_data["period_end"] == period]
            if size_snapshot.empty:
                earlier = size_data[size_data["period_end"] <= period]
                if not earlier.empty:
                    size_snapshot = earlier[
                        earlier["period_end"] == earlier["period_end"].max()
                    ]
            size_order = (
                size_snapshot.sort_values("value", ascending=False)["entity_name"]
                .drop_duplicates()
                .tolist()
            )
        else:
            size_order = []
        size_order.extend(name for name in all_entities if name not in set(size_order))
        preset = st.selectbox(
            "Hazır banka filtresi",
            READY_BANK_FILTERS,
            index=None,
            placeholder="Hazır filtre seçin",
            key=f"{namespace}_entity_preset_{entity_type}",
            help=(
                "Seçili metrik filtreleri mevcut finansal metriğe; genel banka "
                "büyüklüğü filtreleri Toplam Aktifler değerine göre sıralanır."
            ),
        )
        apply_preset = st.button(
            "Hazır filtreyi uygula",
            key=f"{namespace}_entity_preset_apply_{entity_type}",
            use_container_width=True,
            disabled=preset is None,
        )
        if apply_preset and preset:
            count = int(preset.split()[1])
            ranking = size_order if "genel banka büyüklüğü" in preset else value_order
            new_selection = ranking[:count]
            st.session_state[selection_key] = list(new_selection)
            selected_for_preset = set(new_selection)
            for entity_name in all_entities:
                st.session_state[checkbox_key(entity_name)] = (
                    entity_name in selected_for_preset
                )
            st.rerun()
        action_a, action_b = st.columns(2)
        primary_label = f"İlk {exact_count}'yi seç" if exact_count else "Tümünü seç"
        primary_clicked = action_a.button(
            primary_label,
            key=f"{namespace}_entity_primary_{entity_type}",
            use_container_width=True,
        )
        clear_clicked = action_b.button(
            "Seçimi temizle",
            key=f"{namespace}_entity_clear_{entity_type}",
            use_container_width=True,
        )
        if primary_clicked or clear_clicked:
            new_selection = (
                value_order[:exact_count]
                if primary_clicked and exact_count
                else (all_entities if primary_clicked else [])
            )
            st.session_state[selection_key] = list(new_selection)
            selected_for_action = set(new_selection)
            for entity_name in all_entities:
                st.session_state[checkbox_key(entity_name)] = (
                    entity_name in selected_for_action
                )
            st.rerun()
        if sort_choice == "A–Z":
            displayed = sorted(all_entities)
        elif sort_choice == "Z–A":
            displayed = sorted(all_entities, reverse=True)
        elif sort_choice == "Değere göre (düşükten yükseğe)":
            displayed = ascending_value_order
        else:
            displayed = value_order
        if search.strip():
            search_text = search.casefold().strip()
            displayed = [name for name in displayed if search_text in name.casefold()]
        selected_available = [
            name
            for name in st.session_state[selection_key]
            if name in set(all_entities)
        ]
        st.caption(
            f"{len(st.session_state[selection_key])} seçili • "
            f"{len(selected_available)} tanesi bu metrikte mevcut • "
            f"{len(displayed)} seçenek gösteriliyor"
        )
        with st.container(height=340):
            for entity_name in displayed:
                key = checkbox_key(entity_name)
                options = {}
                if key not in st.session_state:
                    options["value"] = entity_name in set(st.session_state[selection_key])
                st.checkbox(
                    entity_name,
                    key=key,
                    on_change=sync_entity_filter,
                    args=(selection_key, key, entity_name),
                    **options,
                )
    if sort_choice == "A–Z":
        ordered = sorted(all_entities)
    elif sort_choice == "Z–A":
        ordered = sorted(all_entities, reverse=True)
    elif sort_choice == "Değere göre (düşükten yükseğe)":
        ordered = ascending_value_order
    else:
        ordered = value_order
    selected = set(st.session_state[selection_key])
    entities = [name for name in ordered if name in selected]
    unavailable_count = len(selected) - len(entities)
    if unavailable_count:
        st.caption(
            f"Seçiminiz korundu; {unavailable_count} banka/kurum için "
            "bu metrikte veri bulunmuyor."
        )
    if exact_count and len(entities) != exact_count:
        st.warning(f"Bu analiz için tam olarak {exact_count} banka/kurum seçin.")
    else:
        st.caption(f"{len(entities)} banka/kurum seçildi.")
    return entities


def render_quality(
    frame: pd.DataFrame,
    periods: list[pd.Timestamp],
    entities: list[str],
    period_labels: dict,
    success_text: str,
    quality_key: str,
) -> None:
    if not periods or not entities:
        st.info("Veri kalitesini ölçmek için en az bir dönem ve banka/kurum seçin.")
        return

    scoped = frame[
        frame["period_end"].isin(periods) & frame["entity_name"].isin(entities)
    ].copy()
    expected_index = pd.MultiIndex.from_product(
        [periods, list(dict.fromkeys(entities))],
        names=["period_end", "entity_name"],
    )
    actual_index = pd.MultiIndex.from_frame(
        scoped.dropna(subset=["value"])[
            ["period_end", "entity_name"]
        ].drop_duplicates()
    )
    missing_index = expected_index.difference(actual_index)
    expected_count = len(expected_index)
    actual_count = len(expected_index.intersection(actual_index))
    coverage = actual_count / expected_count * 100 if expected_count else 0
    duplicates = int(scoped.duplicated(["period_end", "entity_name"]).sum())
    null_values = int(scoped["value"].isna().sum())
    q1, q2, q3, q4 = st.columns(4)
    q1.metric("Beklenen kayıt", number_tr(expected_count))
    q2.metric("Mevcut kayıt", number_tr(actual_count))
    q3.metric("Kapsama oranı", f"%{number_tr(coverage, 1)}")
    q4.metric("Eksik kayıt", number_tr(len(missing_index)))
    st.caption(f"Tekrarlanan kayıt: {duplicates} • Boş değer: {null_values}")
    if duplicates:
        st.warning(f"Aynı dönem ve kurum için {duplicates:,} yinelenen kayıt bulundu.")
    if len(missing_index):
        missing_table = missing_index.to_frame(index=False)
        missing_table["Dönem"] = missing_table["period_end"].map(period_labels)
        missing_table = missing_table.rename(columns={"entity_name": "Banka / kurum"})[
            ["Dönem", "Banka / kurum"]
        ]
        st.warning("Seçilen kapsamda eksik banka/kurum-dönem kayıtları var.")
        st.markdown("##### Eksik kayıt listesi")
        st.dataframe(missing_table, width="stretch", hide_index=True)
        st.download_button(
            "Eksik kayıt listesini CSV indir",
            missing_table.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"tbb_{quality_key}_eksik_kayitlar.csv",
            mime="text/csv",
            key=f"{quality_key}_missing_download",
        )
    else:
        st.success(success_text)
        st.caption("Eksik kayıt listesi boş: seçilen kapsam eksiksiz.")


def render_source_availability_notes(context: dict) -> None:
    availability = context["schema_availability"]
    notes = availability[availability["status"] == "summary_available"]
    if notes.empty:
        return
    st.markdown("##### Kaynak kapsam notları")
    note = SOURCE_AVAILABILITY_NOTES.get(
        (context["source"], context["sheet"], "summary_available"),
        "Ayrıntılı sayfa yayımlanmamış; ilgili özet metrik başka bir kaynak "
        "sayfada mevcuttur.",
    )
    for row in notes.itertuples(index=False):
        st.info(f"{row.period_label}: {note}")


def make_time_figure(
    frame: pd.DataFrame,
    value_column: str,
    value_label: str,
    chart_type: str,
    periods: list[pd.Timestamp],
    period_labels: dict,
    snapshot_period: pd.Timestamp,
):
    chart_data = frame.dropna(subset=[value_column]).copy()
    if chart_data.empty:
        return None
    if chart_type == "Daire":
        chart_data = chart_data[chart_data["period_end"] == snapshot_period].copy()
        if chart_data.empty:
            return None
        chart_data["pie_value"] = chart_data[value_column].abs()
        figure = px.pie(
            chart_data,
            names="entity_name",
            values="pie_value",
            hole=0.38,
            color_discrete_sequence=COLORS,
        )
    else:
        options = dict(
            data_frame=chart_data,
            x="period_end",
            y=value_column,
            color="entity_name",
            labels={
                "period_end": "Dönem",
                value_column: value_label,
                "entity_name": "Banka / kurum",
            },
            color_discrete_sequence=COLORS,
        )
        figure = (
            px.line(**options, markers=True)
            if chart_type == "Çizgi"
            else px.bar(**options, barmode="group")
        )
        figure.update_xaxes(
            tickvals=periods,
            ticktext=[period_labels[item] for item in periods],
            tickangle=-25,
        )
    figure.update_layout(
        height=500,
        margin=dict(l=10, r=10, t=35, b=10),
        legend_title_text="",
        hovermode="x unified" if chart_type == "Çizgi" else "closest",
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    return figure


st.set_page_config(
    page_title="TBB Banka Analizi",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(
    """
    <style>
    :root {
        --navy-950: #061b33;
        --navy-900: #082f57;
        --navy-800: #0b3d6d;
        --blue-700: #0f4c81;
        --blue-600: #1769aa;
        --blue-500: #2f80d1;
        --blue-100: #dcecf9;
        --blue-050: #eff6fc;
        --primary-color: #1769aa;
    }
    .stApp {
        background:
            radial-gradient(circle at 90% 0%, rgba(47,128,209,.13), transparent 30rem),
            linear-gradient(180deg, #edf5fc 0, #f8fbfe 19rem, #f4f8fc 100%);
        color: #16324f;
    }
    [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none; }
    .block-container { max-width: 1680px; padding-top: 1.5rem; padding-bottom: 3rem; }
    h1, h2, h3, h4 { color: var(--navy-950); letter-spacing: -.02em; }
    h1 { font-weight: 800; }
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(255,255,255,.9);
        border: 1px solid #d7e5f2 !important;
        border-radius: 16px;
        box-shadow: 0 8px 25px rgba(8,47,87,.07);
    }
    [data-testid="stMetric"] {
        background: linear-gradient(145deg, #ffffff, #eef6fd);
        border: 1px solid #d2e3f2; border-left: 4px solid var(--blue-600);
        border-radius: 14px; padding: 18px 20px;
        box-shadow: 0 7px 20px rgba(8,47,87,.07);
    }
    [data-testid="stMetricLabel"] p { color: #526d87; font-weight: 650; }
    [data-testid="stMetricValue"] { color: var(--navy-900); }
    .stTabs [data-baseweb="tab"] {
        min-height: 46px; padding: .55rem 1rem; border-radius: 10px 10px 0 0;
        color: #405b75 !important; font-weight: 650; background: transparent !important;
    }
    .stTabs [data-baseweb="tab"]:hover {
        background: #e3f0fb !important; color: var(--navy-900) !important;
    }
    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        background: #dcecf9 !important; color: var(--navy-900) !important;
    }
    .stTabs [data-baseweb="tab"][aria-selected="true"] p {
        color: var(--navy-900) !important; font-weight: 750;
    }
    .stTabs [data-baseweb="tab-highlight"] { background-color: var(--blue-600) !important; }
    .stTabs [data-baseweb="tab-border"] { background-color: #c9dceb !important; }
    div.stButton > button, [data-testid="stDownloadButton"] > button {
        border: 1px solid #b9d2e8; border-radius: 10px;
        color: var(--navy-900); background: #f8fbfe; font-weight: 650;
        transition: all .18s ease;
    }
    div.stButton > button:hover, [data-testid="stDownloadButton"] > button:hover {
        border-color: var(--blue-600); color: white;
        background: linear-gradient(135deg, var(--navy-900), var(--blue-600));
        box-shadow: 0 7px 16px rgba(15,76,129,.18); transform: translateY(-1px);
    }
    [data-baseweb="select"] > div, [data-testid="stTextInput"] input {
        border-color: #bcd3e7; border-radius: 10px; background: #fbfdff;
    }
    [data-baseweb="select"] > div:focus-within, [data-testid="stTextInput"] input:focus {
        border-color: var(--blue-500); box-shadow: 0 0 0 2px rgba(47,128,209,.14);
    }
    /* Seçim kutusundaki yanıp sönen arama imlecini ve açılır liste
       geçişlerini kapat. Klavyeyle arama ve seçim davranışı korunur. */
    input[role="combobox"] {
        caret-color: transparent !important;
        animation: none !important;
        transition: none !important;
    }
    [data-testid="stSelectboxVirtualDropdown"],
    [data-testid="stSelectboxVirtualDropdown"] *,
    [data-baseweb="popover"] {
        animation: none !important;
        transition: none !important;
    }
    /* Uzun açılır liste seçeneklerini kesmeden yatay kaydırmayla göster. */
    [data-testid="stSelectboxVirtualDropdown"] {
        overflow-x: scroll !important;
        overflow-y: scroll !important;
        scrollbar-gutter: stable both-edges !important;
        scrollbar-width: auto;
        scrollbar-color: var(--blue-500) #e6f0f8;
    }
    [data-testid="stSelectboxVirtualDropdown"] [role="listbox"] {
        overflow-x: auto !important;
        scrollbar-gutter: stable both-edges !important;
        scrollbar-color: var(--blue-500) #e6f0f8;
    }
    [data-testid="stSelectboxVirtualDropdown"] [role="listbox"] > [role="presentation"] {
        min-width: 100% !important;
        width: max-content !important;
    }
    [data-testid="stSelectboxVirtualDropdown"] [role="listbox"] > [role="presentation"] > [role="presentation"] {
        width: max-content !important;
        min-width: 100% !important;
        contain: layout style !important;
    }
    [data-testid="stSelectboxVirtualDropdown"] [role="option"] {
        width: max-content !important;
        min-width: max-content !important;
        white-space: nowrap !important;
    }
    [data-testid="stSelectboxVirtualDropdown"] [role="option"] * {
        white-space: nowrap !important;
        overflow: visible !important;
        text-overflow: clip !important;
    }
    [data-testid="stSelectboxVirtualDropdown"]::-webkit-scrollbar,
    [data-testid="stSelectboxVirtualDropdown"] *::-webkit-scrollbar {
        width: 10px !important;
        height: 10px !important;
        background: #e6f0f8 !important;
    }
    [data-testid="stSelectboxVirtualDropdown"]::-webkit-scrollbar-track,
    [data-testid="stSelectboxVirtualDropdown"] *::-webkit-scrollbar-track {
        background: #e6f0f8 !important;
        border-radius: 999px !important;
    }
    [data-testid="stSelectboxVirtualDropdown"]::-webkit-scrollbar-thumb,
    [data-testid="stSelectboxVirtualDropdown"] *::-webkit-scrollbar-thumb {
        background: var(--blue-500) !important;
        border: 2px solid #e6f0f8 !important;
        border-radius: 999px !important;
    }
    [data-testid="stSelectboxVirtualDropdown"]::-webkit-scrollbar-corner,
    [data-testid="stSelectboxVirtualDropdown"] *::-webkit-scrollbar-corner {
        background: #e6f0f8 !important;
    }
    [data-testid="stPopover"] > button {
        width: 100%; min-height: 44px; justify-content: space-between;
        border: 1px solid #bcd3e7; border-radius: 10px; background: #fbfdff;
        color: var(--navy-900); font-weight: 650;
    }
    [data-testid="stPopoverBody"] [data-testid="stCheckbox"] {
        padding: 3px 0; border-bottom: 1px solid #e8f0f7;
    }
    [data-testid="stDataFrame"] {
        border: 1px solid #d4e3f0; border-radius: 12px; overflow: hidden;
        box-shadow: 0 5px 16px rgba(8,47,87,.05);
    }
    [data-testid="stAlert"] { border-radius: 12px; }
    [data-testid="stRadio"] label, [data-testid="stCheckbox"] label { color: #294966; }
    input[type="radio"], input[type="checkbox"] { accent-color: var(--blue-600) !important; }
    [data-baseweb="radio"]:has(input:checked) > div:first-child,
    [data-baseweb="radio"]:has(input:checked) > div:first-child > div {
        border-color: var(--blue-600) !important;
        background-color: var(--blue-600) !important;
    }
    [data-testid="stCheckbox"] label:has(input:checked) span {
        border-color: var(--blue-600) !important;
    }
    hr { border-color: #d8e6f2; }
    .subtle { color: #58718a; margin-top: -10px; margin-bottom: 20px; font-weight: 500; }
    </style>
    """,
    unsafe_allow_html=True,
)

title_col, refresh_col = st.columns([7, 1], vertical_alignment="center")
with title_col:
    st.title("TBB Banka Analiz Paneli")
with refresh_col:
    if st.button("Veritabanını yenile", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
st.markdown(
    '<div class="subtle">Türkiye Bankalar Birliği solo banka verileri • '
    "Mart 2020’den itibaren çeyreklik analiz</div>",
    unsafe_allow_html=True,
)

if not DB_PATH.exists():
    with st.spinner(
        "TBB veritabanı ilk kullanım için hazırlanıyor. Bu işlem yaklaşık 20 saniye sürer..."
    ):
        try:
            ensure_database(RAW_DIR, DB_PATH)
        except Exception as exc:
            st.error("Veritabanı kaynak TBB dosyalarından oluşturulamadı.")
            st.exception(exc)
            st.stop()

catalog = load_catalog()
calculator_catalog = catalog.drop_duplicates("metric_key").copy()
calculator_catalog["display_name"] = calculator_catalog.apply(
    lambda row: (
        f"{SOURCE_LABELS.get(row['source_group'], row['source_group'])} / "
        f"{SHEET_LABELS.get((row['source_group'], row['sheet_key']), row['sheet_name'])} / "
        f"{metric_display_label(row['metric_key'], row['metric_path'])}"
    ),
    axis=1,
)
calculator_catalog = calculator_catalog.sort_values("display_name")
calculator_options = calculator_catalog["metric_key"].tolist()
calculator_lookup = calculator_catalog.set_index("metric_key")["display_name"].to_dict()

period_tab, time_tab, calculator_tab, simulation_tab = st.tabs(
    [
        "Dönemsel analiz",
        "Zaman analizi",
        "Özelleştirilebilir metrikler",
        "Metrik simülasyonu",
    ]
)


with period_tab:
    st.subheader("Dönemsel analiz")
    context = render_metric_filters("period", catalog)
    if context:
        period_col, bank_filter_col, chart_col = st.columns([1, 2, 1])
        with period_col:
            analysis_date = st.selectbox(
                "Analiz dönemi",
                context["period_dates"],
                index=len(context["period_dates"]) - 1,
                format_func=lambda item: context["period_labels"][item],
                key="period_analysis_date",
            )
        with bank_filter_col:
            entities = render_entity_filter(
                context,
                "period",
                analysis_date,
                default_selection="all",
            )
        with chart_col:
            chart_type = render_chart_selector(
                "period",
                default=(
                    "Çizgi"
                    if context["metric_key"] == CAPITAL_ADEQUACY_METRIC
                    else "Sütun"
                ),
            )
        data = context["data"]
        unit = context["unit"]
        snapshot = data[
            (data["period_end"] == analysis_date) & data["entity_name"].isin(entities)
        ].sort_values("value", ascending=False)
        ranking_all = data[data["period_end"] == analysis_date].sort_values(
            "value", ascending=False
        ).copy()
        ranking_all["Sıra"] = range(1, len(ranking_all) + 1)
        systemic_tab, single_chart_tab, table_tab, quality_tab = st.tabs(
            ["Sistemik 9 banka", "Seçilebilir bankalar", "Veri tablosu", "Veri kalitesi"]
        )
        with systemic_tab:
            systemic_names = systemic_entities(
                ranking_all["entity_name"].dropna().unique().tolist()
            )
            systemic_snapshot = ranking_all[
                ranking_all["entity_name"].isin(systemic_names)
            ].sort_values("value")
            systemic_figure = px.bar(
                systemic_snapshot,
                x="value",
                y="entity_name",
                orientation="h",
                labels={"entity_name": "", "value": unit},
                color="value",
                color_continuous_scale=CONTINUOUS_COLORS,
            )
            systemic_figure.update_layout(
                height=520,
                coloraxis_showscale=False,
                title=(
                    f"{context['period_labels'][analysis_date]} • Sistemik öneme "
                    "sahip 9 banka"
                ),
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            st.plotly_chart(
                systemic_figure,
                width="stretch",
                key="period_systemic_chart",
            )
        with single_chart_tab:
            if snapshot.empty:
                st.info("Grafik için en az bir banka veya kurum seçin.")
            else:
                if chart_type == "Daire":
                    chart_data = snapshot.copy()
                    chart_data["pie_value"] = chart_data["value"].abs()
                    figure = px.pie(
                        chart_data,
                        names="entity_name",
                        values="pie_value",
                        hole=0.38,
                        color_discrete_sequence=COLORS,
                    )
                elif chart_type == "Çizgi":
                    figure = px.line(
                        snapshot,
                        x="entity_name",
                        y="value",
                        markers=True,
                        labels={"entity_name": "Banka / kurum", "value": unit},
                        color_discrete_sequence=[COLORS[0]],
                    )
                    figure.update_xaxes(tickangle=-25)
                else:
                    figure = px.bar(
                        snapshot,
                        x="entity_name",
                        y="value",
                        labels={"entity_name": "Banka / kurum", "value": unit},
                        color="value",
                        color_continuous_scale=CONTINUOUS_COLORS,
                    )
                    figure.update_xaxes(tickangle=-25)
                    figure.update_layout(coloraxis_showscale=False)
                figure.update_layout(height=500, plot_bgcolor="white", paper_bgcolor="white")
                st.plotly_chart(figure, width="stretch", key="period_single_chart")
        period_table = ranking_all[["Sıra", "entity_name", "value", "unit"]].rename(
            columns={"entity_name": "Banka / kurum", "value": "Değer", "unit": "Birim"}
        )
        with table_tab:
            st.dataframe(period_table, width="stretch", hide_index=True)
            st.download_button(
                "Dönemsel analiz verisini CSV indir",
                period_table.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_donemsel_analiz.csv",
                mime="text/csv",
            )
        with quality_tab:
            render_quality(
                ranking_all,
                [analysis_date],
                context["all_entities"],
                context["period_labels"],
                "Bu dönemde tüm banka/kurumlar için veri mevcut.",
                "period",
            )
            render_source_availability_notes(context)


with time_tab:
    st.subheader("Zaman analizi")
    context = render_metric_filters("time", catalog)
    if context:
        dates = context["period_dates"]
        c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
        with c1:
            december_2023 = next(
                (
                    item
                    for item in dates[:-1]
                    if item.year == 2023 and item.month == 12
                ),
                dates[0],
            )
            start_date = st.selectbox(
                "Başlangıç dönemi",
                dates[:-1],
                index=dates[:-1].index(december_2023),
                format_func=lambda item: context["period_labels"][item],
                key="time_start",
            )
        end_options = [item for item in dates if item > start_date]
        with c2:
            december_end_options = [item for item in end_options if item.month == 12]
            default_end = (
                december_end_options[-1] if december_end_options else end_options[-1]
            )
            end_date = st.selectbox(
                "Bitiş dönemi",
                end_options,
                index=end_options.index(default_end),
                format_func=lambda item: context["period_labels"][item],
                key=f"time_end_{start_date.date()}",
            )
        with c3:
            entities = render_entity_filter(
                context,
                "time",
                end_date,
                default_selection="halkbank",
            )
        with c4:
            chart_type = render_chart_selector(
                "time",
                default=(
                    "Çizgi"
                    if context["metric_key"] == CAPITAL_ADEQUACY_METRIC
                    else "Sütun"
                ),
            )
        data = context["data"]
        comparison_periods = [item for item in dates if start_date <= item <= end_date]
        comparison_data = data[
            data["entity_name"].isin(entities)
            & data["period_end"].between(start_date, end_date)
        ].copy()
        history = data[
            data["entity_name"].isin(entities) & (data["period_end"] <= end_date)
        ].sort_values(["entity_name", "period_end"])
        history = history.copy()
        history["quarterly_change"] = (
            history.groupby("entity_name")["value"].pct_change(fill_method=None) * 100
        )
        history["annual_change"] = (
            history.groupby("entity_name")["value"].pct_change(4, fill_method=None) * 100
        )
        analysis_data = history[
            history["period_end"].between(start_date, end_date)
        ].copy()
        endpoints = comparison_data[
            comparison_data["period_end"].isin([start_date, end_date])
        ].copy()
        systemic_tab, trend_tab, endpoint_tab, quarterly_tab, annual_tab, table_tab, quality_tab = st.tabs(
            [
                "Sistemik 9 banka",
                "Dönem seyri",
                "Başlangıç–bitiş",
                "Çeyreklik değişim",
                "Yıllık değişim",
                "Veri tablosu",
                "Veri kalitesi",
            ]
        )
        with systemic_tab:
            systemic_names = systemic_entities(context["all_entities"])
            systemic_data = data[
                data["entity_name"].isin(systemic_names)
                & data["period_end"].between(start_date, end_date)
            ].copy()
            systemic_figure = make_time_figure(
                systemic_data,
                "value",
                context["unit"],
                chart_type,
                comparison_periods,
                context["period_labels"],
                end_date,
            )
            if systemic_figure is None:
                st.info("Sistemik 9 banka için seçili kapsamda veri bulunamadı.")
            else:
                st.plotly_chart(
                    systemic_figure,
                    width="stretch",
                    key="time_systemic_chart",
                )
        with trend_tab:
            if comparison_data.empty:
                st.info("Grafik için en az bir banka veya kurum seçin.")
            elif chart_type == "Daire":
                chart_data = comparison_data.copy()
                chart_data["chart_value"] = chart_data["value"].abs()
                figure = px.sunburst(
                    chart_data,
                    path=["period_label", "entity_name"],
                    values="chart_value",
                    color="period_label",
                    color_discrete_sequence=COLORS,
                )
                figure.update_layout(height=580)
                st.plotly_chart(figure, width="stretch", key="time_trend_chart")
            else:
                figure = make_time_figure(
                    comparison_data,
                    "value",
                    context["unit"],
                    chart_type,
                    comparison_periods,
                    context["period_labels"],
                    end_date,
                )
                st.plotly_chart(figure, width="stretch", key="time_trend_chart")
        for tab, column, label, empty_text in (
            (
                quarterly_tab,
                "quarterly_change",
                "Çeyreklik değişim (%)",
                "Çeyreklik değişim hesaplanamadı.",
            ),
            (
                annual_tab,
                "annual_change",
                "Yıllık değişim (%)",
                "Yıllık değişim için dört önceki çeyrek gerekir.",
            ),
        ):
            with tab:
                figure = make_time_figure(
                    analysis_data,
                    column,
                    label,
                    chart_type,
                    comparison_periods,
                    context["period_labels"],
                    end_date,
                )
                if figure is None:
                    st.info(empty_text)
                else:
                    st.plotly_chart(
                        figure,
                        width="stretch",
                        key=f"time_{column}_chart",
                    )
        comparison = endpoints.pivot_table(
            index="entity_name", columns="period_end", values="value", aggfunc="first"
        ).reindex(entities)
        for endpoint in (start_date, end_date):
            if endpoint not in comparison.columns:
                comparison[endpoint] = pd.NA
        summary = pd.DataFrame(
            {
                "Banka / kurum": comparison.index,
                context["period_labels"][start_date]: comparison[start_date].values,
                context["period_labels"][end_date]: comparison[end_date].values,
            }
        )
        summary["Tutar değişimi"] = (
            summary[context["period_labels"][end_date]]
            - summary[context["period_labels"][start_date]]
        )
        summary["Değişim (%)"] = (
            summary[context["period_labels"][end_date]]
            .div(summary[context["period_labels"][start_date]])
            .sub(1)
            .mul(100)
        )
        with endpoint_tab:
            endpoint_figure = make_time_figure(
                endpoints,
                "value",
                context["unit"],
                chart_type,
                [start_date, end_date],
                context["period_labels"],
                end_date,
            )
            if endpoint_figure is None:
                st.info("Başlangıç–bitiş karşılaştırması için veri bulunamadı.")
            else:
                st.plotly_chart(
                    endpoint_figure,
                    width="stretch",
                    key="time_endpoint_chart",
                )
            st.dataframe(
                summary,
                width="stretch",
                hide_index=True,
                column_config={
                    "Değişim (%)": st.column_config.NumberColumn(format="%.2f%%")
                },
            )
        detail = analysis_data[
            [
                "period_label",
                "entity_name",
                "value",
                "quarterly_change",
                "annual_change",
                "unit",
            ]
        ].rename(
            columns={
                "period_label": "Dönem",
                "entity_name": "Banka / kurum",
                "value": "Değer",
                "quarterly_change": "Çeyreklik değişim (%)",
                "annual_change": "Yıllık değişim (%)",
                "unit": "Birim",
            }
        )
        with table_tab:
            st.markdown("**Ara dönemler dahil tüm kayıtlar**")
            st.dataframe(
                detail,
                width="stretch",
                hide_index=True,
                column_config={
                    "Çeyreklik değişim (%)": st.column_config.NumberColumn(format="%.2f%%"),
                    "Yıllık değişim (%)": st.column_config.NumberColumn(format="%.2f%%"),
                },
            )
            st.download_button(
                "Zaman analizi verisini CSV indir",
                detail.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_zaman_analizi.csv",
                mime="text/csv",
            )
        with quality_tab:
            render_quality(
                analysis_data,
                comparison_periods,
                entities,
                context["period_labels"],
                "Seçilen zaman aralığında eksik banka/kurum-dönem kaydı yok.",
                "time",
            )
            render_source_availability_notes(context)


with calculator_tab:
    st.subheader("Özelleştirilebilir metrikler")
    with st.container(border=True):
        st.markdown("#### Formül bileşenleri")
        c1, c2, c3 = st.columns([2, 2, 1])
        preferred_a = "aktifler.varliklar.toplam_aktifler"
        metric_a = c1.selectbox(
            "Metrik A",
            calculator_options,
            index=(
                calculator_options.index(preferred_a)
                if preferred_a in calculator_options
                else 0
            ),
            format_func=lambda item: calculator_lookup[item],
            key="calculator_metric_a",
        )
        preferred_b = "pasifler.yukumlulukler.toplam_yukumlulukler"
        metric_b = c2.selectbox(
            "Metrik B",
            calculator_options,
            index=(
                calculator_options.index(preferred_b)
                if preferred_b in calculator_options
                else min(1, len(calculator_options) - 1)
            ),
            format_func=lambda item: calculator_lookup[item],
            key="calculator_metric_b",
        )
        entity_type = c3.radio(
            "Karşılaştırma düzeyi",
            list(ENTITY_LABELS),
            format_func=lambda item: ENTITY_LABELS[item],
            horizontal=True,
            key="calculator_entity_type",
        )
        selected_metrics = {"A": metric_a, "B": metric_b}
        with st.expander("Ek metrikler (C–H)", expanded=False):
            metric_count = int(
                st.number_input(
                    "Toplam metrik sayısı",
                    min_value=2,
                    max_value=8,
                    value=2,
                    step=1,
                    key="calculator_metric_count",
                )
            )
            st.caption("C–H seçimleri yalnızca ihtiyaç duyduğunuzda formüle eklenir.")
            extra_columns = st.columns(2)
            for position, alias in enumerate("CDEFGH"[: metric_count - 2]):
                default_index = min(position + 2, len(calculator_options) - 1)
                selected_metrics[alias] = extra_columns[position % 2].selectbox(
                    f"Metrik {alias}",
                    calculator_options,
                    index=default_index,
                    format_func=lambda item: calculator_lookup[item],
                    key=f"calculator_metric_{alias.lower()}",
                )
        formula = st.text_input(
            "Formül",
            value="(A / B) * 100",
            placeholder="Örnek: (A + B) / (C - D)",
            key="calculator_formula",
        )
        st.caption(
            "Kullanılabilir işlemler: toplama (+), çıkarma (−), çarpma (*), "
            "bölme/oran (/) ve yüzde oran için ×100. Örnekler: A+B, A-B, "
            "A*B, A/B, (A/B)*100, (A+B)/(C-D)."
        )
        with st.expander("Metrik harflerinin karşılıkları", expanded=False):
            for alias, metric_key in selected_metrics.items():
                st.markdown(f"**{alias}** — {calculator_lookup[metric_key]}")

    metric_frames: dict[str, pd.DataFrame] = {}
    for alias, metric_key in selected_metrics.items():
        metric_frame = load_series(metric_key, entity_type).copy()
        metric_frame["period_end"] = pd.to_datetime(metric_frame["period_end"])
        metric_frames[alias] = metric_frame

    period_sets = [
        set(frame["period_end"]) for frame in metric_frames.values() if not frame.empty
    ]
    common_periods = (
        sorted(set.intersection(*period_sets))
        if len(period_sets) == len(metric_frames) and period_sets
        else []
    )
    if len(common_periods) < 2:
        st.warning("Seçilen metriklerin ortak en az iki dönemi bulunamadı.")
    else:
        period_labels = (
            pd.concat(
                [
                    frame[["period_end", "period_label"]]
                    for frame in metric_frames.values()
                ]
            )
            .drop_duplicates("period_end")
            .set_index("period_end")["period_label"]
            .to_dict()
        )
        c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
        with c1:
            calculator_start = st.selectbox(
                "Başlangıç dönemi",
                common_periods[:-1],
                index=0,
                format_func=lambda item: period_labels[item],
                key="calculator_start",
            )
        calculator_end_options = [
            item for item in common_periods if item > calculator_start
        ]
        with c2:
            calculator_end = st.selectbox(
                "Bitiş dönemi",
                calculator_end_options,
                index=len(calculator_end_options) - 1,
                format_func=lambda item: period_labels[item],
                key=f"calculator_end_{calculator_start.date()}",
            )
        primary_frame = metric_frames["A"]
        calculator_context = {
            "data": primary_frame,
            "all_entities": sorted(
                primary_frame["entity_name"].dropna().unique()
            ),
            "metric_key": "__".join(selected_metrics.values()),
            "entity_type": entity_type,
        }
        with c3:
            entities = render_entity_filter(
                calculator_context,
                "calculator",
                calculator_end,
                default_selection="halkbank",
            )
        with c4:
            chart_type = render_chart_selector("calculator")

        periods = [
            item
            for item in common_periods
            if calculator_start <= item <= calculator_end
        ]
        expected = pd.MultiIndex.from_product(
            [periods, list(dict.fromkeys(entities))],
            names=["period_end", "entity_name"],
        )
        calculation = expected.to_frame(index=False)
        actual_indexes: dict[str, pd.MultiIndex] = {}
        for alias, frame in metric_frames.items():
            scope = frame[
                frame["entity_name"].isin(entities)
                & frame["period_end"].between(calculator_start, calculator_end)
            ][["period_end", "entity_name", "value"]].drop_duplicates(
                ["period_end", "entity_name"]
            )
            actual_indexes[alias] = pd.MultiIndex.from_frame(
                scope.dropna(subset=["value"])[["period_end", "entity_name"]]
            )
            calculation = calculation.merge(
                scope.rename(columns={"value": alias}),
                on=["period_end", "entity_name"],
                how="left",
            )
        calculation["period_label"] = calculation["period_end"].map(period_labels)

        formula_error = None
        zero_denominators = 0
        used_symbols: set[str] = set()
        try:
            result, zero_denominators, used_symbols = evaluate_formula(
                formula,
                {alias: calculation[alias] for alias in selected_metrics},
            )
            calculation["result"] = result
        except FormulaError as exc:
            formula_error = str(exc)
            calculation["result"] = pd.NA

        graph_tab, table_tab, quality_tab = st.tabs(
            ["Grafik", "Veri tablosu", "Veri kalitesi"]
        )
        with graph_tab:
            if formula_error:
                st.error(formula_error)
            else:
                st.markdown(f"**Uygulanan formül:** `{formula}`")
                figure = make_time_figure(
                    calculation,
                    "result",
                    "Hesaplanan değer",
                    chart_type,
                    periods,
                    period_labels,
                    calculator_end,
                )
                if figure is None:
                    st.info("Seçili kapsamda hesaplanabilir veri bulunamadı.")
                else:
                    if zero_denominators:
                        st.warning(
                            f"Sıfır paydalı {zero_denominators} hesaplama boş bırakıldı."
                        )
                    st.plotly_chart(
                        figure,
                        width="stretch",
                        key="calculator_result_chart",
                    )

        calculator_columns = [
            "period_label",
            "entity_name",
            *selected_metrics.keys(),
            "result",
        ]
        calculator_table = calculation[calculator_columns].rename(
            columns={
                "period_label": "Dönem",
                "entity_name": "Banka / kurum",
                "result": "Formül sonucu",
            }
        )
        with table_tab:
            if formula_error:
                st.error(formula_error)
            st.dataframe(calculator_table, width="stretch", hide_index=True)
            st.download_button(
                "Hesaplama sonucunu CSV indir",
                calculator_table.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_ozellestirilebilir_metrik.csv",
                mime="text/csv",
            )

        with quality_tab:
            complete_rows = int(
                calculation[list(selected_metrics)].notna().all(axis=1).sum()
            )
            coverage = (
                complete_rows / len(expected) * 100 if len(expected) else 0
            )
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Beklenen kayıt", number_tr(len(expected)))
            q2.metric("Tam metrik seti", number_tr(complete_rows))
            q3.metric("Kapsama oranı", f"%{number_tr(coverage, 1)}")
            q4.metric("Sıfır payda", number_tr(zero_denominators))
            if formula_error:
                st.error(f"Formül kontrolü: {formula_error}")
            else:
                st.success(
                    "Formül geçerli • kullanılan metrikler: "
                    + ", ".join(sorted(used_symbols))
                )

            missing_frames = []
            for alias, actual_index in actual_indexes.items():
                missing_index = expected.difference(actual_index)
                if len(missing_index):
                    missing_part = missing_index.to_frame(index=False)
                    missing_part["Eksik metrik"] = alias
                    missing_part["Metrik adı"] = calculator_lookup[
                        selected_metrics[alias]
                    ]
                    missing_frames.append(missing_part)
            if missing_frames:
                missing_table = pd.concat(missing_frames, ignore_index=True)
                missing_table["Dönem"] = missing_table["period_end"].map(
                    period_labels
                )
                missing_table = missing_table.rename(
                    columns={"entity_name": "Banka / kurum"}
                )[
                    [
                        "Eksik metrik",
                        "Metrik adı",
                        "Dönem",
                        "Banka / kurum",
                    ]
                ]
                st.warning("Hesaplama kapsamında eksik metrik-dönem kayıtları var.")
                st.markdown("##### Eksik kayıt listesi")
                st.dataframe(missing_table, width="stretch", hide_index=True)
                st.download_button(
                    "Eksik kayıt listesini CSV indir",
                    missing_table.to_csv(index=False).encode("utf-8-sig"),
                    file_name="tbb_ozellestirilebilir_metrik_eksikleri.csv",
                    mime="text/csv",
                    key="calculator_missing_download",
                )
            else:
                st.success(
                    "Seçilen bütün metriklerde banka/kurum-dönem eksiği yok."
                )
                st.caption("Eksik kayıt listesi boş: seçilen kapsam eksiksiz.")


with simulation_tab:
    st.subheader("Metrik simülasyonu")
    st.caption(
        "İki metriğin oranını seçin; metriklere yüzdesel şok uygulayarak "
        "senaryo sonucunu birden fazla dönem boyunca karşılaştırın."
    )
    with st.container(border=True):
        st.markdown("#### Simülasyon ayarları")
        s1, s2, s3 = st.columns([2, 2, 1])
        simulation_metric_a = s1.selectbox(
            "Metrik A (pay)",
            calculator_options,
            index=(
                calculator_options.index(EQUITY_METRIC)
                if EQUITY_METRIC in calculator_options
                else 0
            ),
            format_func=lambda item: calculator_lookup[item],
            key="simulation_metric_a",
            on_change=reset_simulation_scope,
        )
        simulation_metric_b = s2.selectbox(
            "Metrik B (payda)",
            calculator_options,
            index=(
                calculator_options.index(GENERAL_SIZE_METRIC)
                if GENERAL_SIZE_METRIC in calculator_options
                else min(1, len(calculator_options) - 1)
            ),
            format_func=lambda item: calculator_lookup[item],
            key="simulation_metric_b",
            on_change=reset_simulation_scope,
        )
        simulation_entity_type = s3.radio(
            "Karşılaştırma düzeyi",
            list(ENTITY_LABELS),
            format_func=lambda item: ENTITY_LABELS[item],
            horizontal=True,
            key="simulation_entity_type",
            on_change=reset_simulation_scope,
        )

    simulation_a = load_series(simulation_metric_a, simulation_entity_type).copy()
    simulation_b = load_series(simulation_metric_b, simulation_entity_type).copy()
    for frame in (simulation_a, simulation_b):
        frame["period_end"] = pd.to_datetime(frame["period_end"])
    simulation_periods = sorted(
        set(simulation_a["period_end"]).intersection(simulation_b["period_end"])
    )
    simulation_entities = sorted(
        set(simulation_a["entity_name"]).intersection(simulation_b["entity_name"])
    )
    if not simulation_periods or not simulation_entities:
        st.warning("Seçilen iki metrik için ortak banka/kurum ve dönem bulunamadı.")
    else:
        simulation_labels = (
            pd.concat(
                [
                    simulation_a[["period_end", "period_label"]],
                    simulation_b[["period_end", "period_label"]],
                ]
            )
            .drop_duplicates("period_end")
            .set_index("period_end")["period_label"]
            .to_dict()
        )
        december_2023 = pd.Timestamp("2023-12-31")
        default_start_index = (
            simulation_periods.index(december_2023)
            if december_2023 in simulation_periods
            else 0
        )
        f1, f2 = st.columns(2)
        simulation_start = f1.selectbox(
            "Başlangıç dönemi",
            simulation_periods,
            index=default_start_index,
            format_func=lambda item: simulation_labels[item],
            key="simulation_start_period",
            on_change=reset_simulation_end,
        )
        end_periods = [period for period in simulation_periods if period >= simulation_start]
        simulation_end = f2.selectbox(
            "Bitiş dönemi",
            end_periods,
            index=len(end_periods) - 1,
            format_func=lambda item: simulation_labels[item],
            key="simulation_end_period",
        )
        operation_col, chart_col = st.columns([2, 1])
        operation = operation_col.radio(
            "Hesaplama",
            ["Oran (A / B)", "Yüzde oran ((A / B) × 100)"],
            horizontal=True,
            key="simulation_operation",
        )
        simulation_chart_type = chart_col.radio(
            "Grafik türü",
            ["Çizgi", "Sütun"],
            horizontal=True,
            key="simulation_chart_type",
        )

        shock_a_col, shock_b_col = st.columns(2)
        shock_a = shock_a_col.number_input(
            "Metrik A değişimi (%)",
            min_value=-100.0,
            max_value=1000.0,
            value=0.0,
            step=1.0,
            help="Örneğin yüzde 10 artış için 10, yüzde 5 düşüş için -5 yazın.",
            key="simulation_shock_a",
        )
        shock_b = shock_b_col.number_input(
            "Metrik B değişimi (%)",
            min_value=-100.0,
            max_value=1000.0,
            value=0.0,
            step=1.0,
            help="Örneğin yüzde 10 artış için 10, yüzde 5 düşüş için -5 yazın.",
            key="simulation_shock_b",
        )

        multiplier = 100 if operation.startswith("Yüzde") else 1
        scope_a = simulation_a[
            (simulation_a["period_end"] >= simulation_start)
            & (simulation_a["period_end"] <= simulation_end)
            & (simulation_a["entity_name"].isin(simulation_entities))
        ][["period_end", "period_label", "entity_name", "value"]].rename(
            columns={"value": "Metrik A"}
        )
        scope_b = simulation_b[
            (simulation_b["period_end"] >= simulation_start)
            & (simulation_b["period_end"] <= simulation_end)
            & (simulation_b["entity_name"].isin(simulation_entities))
        ][["period_end", "entity_name", "value"]].rename(
            columns={"value": "Metrik B"}
        )
        all_scenarios = scope_a.merge(
            scope_b,
            on=["period_end", "entity_name"],
            how="inner",
        ).sort_values(["period_end", "entity_name"])
        all_scenarios["Simüle Metrik A"] = all_scenarios["Metrik A"] * (
            1 + shock_a / 100
        )
        all_scenarios["Simüle Metrik B"] = all_scenarios["Metrik B"] * (
            1 + shock_b / 100
        )
        valid_scenarios = all_scenarios[
            (all_scenarios["Metrik B"] != 0)
            & (all_scenarios["Simüle Metrik B"] != 0)
        ].copy()
        valid_scenarios["Mevcut"] = (
            valid_scenarios["Metrik A"]
            / valid_scenarios["Metrik B"]
            * multiplier
        )
        valid_scenarios["Simülasyon"] = (
            valid_scenarios["Simüle Metrik A"]
            / valid_scenarios["Simüle Metrik B"]
            * multiplier
        )
        valid_scenarios["Değişim (%)"] = (
            valid_scenarios["Simülasyon"] / valid_scenarios["Mevcut"] - 1
        ) * 100

        ranking_data = valid_scenarios[
            ["period_end", "period_label", "entity_name", "Mevcut"]
        ].rename(columns={"Mevcut": "value"})
        simulation_context = {
            "data": ranking_data,
            "all_entities": simulation_entities,
            "metric_key": f"simulation.{simulation_metric_a}.{simulation_metric_b}",
            "entity_type": simulation_entity_type,
        }

        def simulation_chart(frame: pd.DataFrame, key: str, multiple: bool) -> None:
            id_columns = ["period_end", "period_label"]
            if multiple:
                id_columns.append("entity_name")
            chart_data = frame.melt(
                id_vars=id_columns,
                value_vars=["Mevcut", "Simülasyon"],
                var_name="Senaryo",
                value_name="Sonuç",
            )
            if simulation_chart_type == "Çizgi":
                if multiple:
                    figure = px.line(
                        chart_data,
                        x="period_label",
                        y="Sonuç",
                        color="entity_name",
                        line_dash="Senaryo",
                        markers=True,
                        color_discrete_sequence=COLORS,
                        labels={
                            "period_label": "Dönem",
                            "Sonuç": operation,
                            "entity_name": "Banka / kurum",
                        },
                    )
                else:
                    figure = px.line(
                        chart_data,
                        x="period_label",
                        y="Sonuç",
                        color="Senaryo",
                        markers=True,
                        color_discrete_sequence=COLORS[:2],
                        labels={"period_label": "Dönem", "Sonuç": operation},
                    )
            elif multiple:
                figure = px.bar(
                    chart_data,
                    x="period_label",
                    y="Sonuç",
                    color="entity_name",
                    pattern_shape="Senaryo",
                    barmode="group",
                    color_discrete_sequence=COLORS,
                    labels={
                        "period_label": "Dönem",
                        "Sonuç": operation,
                        "entity_name": "Banka / kurum",
                    },
                )
            else:
                figure = px.bar(
                    chart_data,
                    x="period_label",
                    y="Sonuç",
                    color="Senaryo",
                    barmode="group",
                    color_discrete_sequence=COLORS[:2],
                    labels={"period_label": "Dönem", "Sonuç": operation},
                )
            figure.update_layout(
                height=440,
                plot_bgcolor="white",
                paper_bgcolor="white",
                legend_title_text="",
            )
            st.plotly_chart(figure, width="stretch", key=key)

        single_tab, multi_tab, simulation_table_tab, simulation_quality_tab = st.tabs(
            ["Tek banka", "Birden fazla banka", "Veri tablosu", "Veri kalitesi"]
        )
        default_entity = (
            "Türkiye Halk Bankası A.Ş."
            if "Türkiye Halk Bankası A.Ş." in simulation_entities
            else simulation_entities[0]
        )
        with single_tab:
            simulation_entity = st.selectbox(
                "Banka / kurum",
                simulation_entities,
                index=simulation_entities.index(default_entity),
                key="simulation_entity",
            )
            single_scenario = valid_scenarios[
                valid_scenarios["entity_name"] == simulation_entity
            ]
            if single_scenario.empty:
                st.warning(
                    "Seçilen banka ve dönem aralığında iki metrik birlikte "
                    "bulunmuyor veya payda sıfır."
                )
            else:
                last_row = single_scenario.iloc[-1]
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("Bitiş dönemi", last_row["period_label"])
                r2.metric(
                    "Metrik A (simüle)", number_tr(last_row["Simüle Metrik A"], 2)
                )
                r3.metric(
                    "Metrik B (simüle)", number_tr(last_row["Simüle Metrik B"], 2)
                )
                r4.metric(
                    "Simüle oran",
                    (
                        f"%{number_tr(last_row['Simülasyon'], 2)}"
                        if multiplier == 100
                        else number_tr(last_row["Simülasyon"], 4)
                    ),
                    delta=f"%{number_tr(last_row['Değişim (%)'], 2)}",
                )
                simulation_chart(
                    single_scenario,
                    "metric_simulation_single_chart",
                    multiple=False,
                )

        with multi_tab:
            st.markdown("#### Banka/kurum filtresi")
            multi_entities = render_entity_filter(
                simulation_context,
                "simulation_multi",
                simulation_end,
                default_selection="halkbank",
            )
            multi_scenario = valid_scenarios[
                valid_scenarios["entity_name"].isin(multi_entities)
            ]
            if multi_scenario.empty:
                st.warning("Çoklu simülasyon için en az bir banka/kurum seçin.")
            else:
                simulation_chart(
                    multi_scenario,
                    "metric_simulation_multi_chart",
                    multiple=True,
                )

        with simulation_table_tab:
            st.caption(
                "Tablo, ‘Birden fazla banka’ sekmesindeki banka/kurum seçimini kullanır."
            )
            if not multi_entities:
                st.info("Veri tablosu için çoklu banka sekmesinden seçim yapın.")
            else:
                simulation_table = multi_scenario.rename(
                    columns={
                        "period_label": "Dönem",
                        "entity_name": "Banka / kurum",
                        "Metrik A": "Metrik A (mevcut)",
                        "Metrik B": "Metrik B (mevcut)",
                    }
                )[
                    [
                        "Dönem",
                        "Banka / kurum",
                        "Metrik A (mevcut)",
                        "Simüle Metrik A",
                        "Metrik B (mevcut)",
                        "Simüle Metrik B",
                        "Mevcut",
                        "Simülasyon",
                        "Değişim (%)",
                    ]
                ]
                st.dataframe(simulation_table, width="stretch", hide_index=True)
                st.download_button(
                    "Simülasyon verisini CSV indir",
                    simulation_table.to_csv(index=False).encode("utf-8-sig"),
                    file_name="tbb_metrik_simulasyonu.csv",
                    mime="text/csv",
                    key="simulation_table_download",
                )

        with simulation_quality_tab:
            st.caption(
                "Kontrol, ‘Birden fazla banka’ sekmesindeki banka/kurum seçimini kullanır."
            )
            if not multi_entities:
                st.info("Veri kalitesini ölçmek için çoklu banka sekmesinden seçim yapın.")
            selected_periods = [
                period
                for period in simulation_periods
                if simulation_start <= period <= simulation_end
            ]
            expected = pd.MultiIndex.from_product(
                [selected_periods, multi_entities],
                names=["period_end", "entity_name"],
            )
            missing_parts = []
            for metric_label, metric_frame, value_column in (
                ("Metrik A", scope_a, "Metrik A"),
                ("Metrik B", scope_b, "Metrik B"),
            ):
                actual = pd.MultiIndex.from_frame(
                    metric_frame[
                        metric_frame["entity_name"].isin(multi_entities)
                    ].dropna(subset=[value_column])[
                        ["period_end", "entity_name"]
                    ].drop_duplicates()
                )
                missing = expected.difference(actual)
                if len(missing):
                    part = missing.to_frame(index=False)
                    part["Eksik alan"] = metric_label
                    missing_parts.append(part)
            missing_count = sum(len(part) for part in missing_parts)
            expected_count = len(expected) * 2
            coverage = (
                (expected_count - missing_count) / expected_count * 100
                if expected_count
                else 0
            )
            zero_denominators = all_scenarios[
                all_scenarios["entity_name"].isin(multi_entities)
                & (
                    (all_scenarios["Metrik B"] == 0)
                    | (all_scenarios["Simüle Metrik B"] == 0)
                )
            ][["period_end", "entity_name"]].drop_duplicates()
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Beklenen değer", number_tr(expected_count))
            q2.metric("Mevcut değer", number_tr(expected_count - missing_count))
            q3.metric("Kapsama oranı", f"%{number_tr(coverage, 1)}")
            q4.metric("Sıfır payda", number_tr(len(zero_denominators)))
            if missing_parts:
                missing_table = pd.concat(missing_parts, ignore_index=True)
                missing_table["Dönem"] = missing_table["period_end"].map(
                    simulation_labels
                )
                missing_table = missing_table.rename(
                    columns={"entity_name": "Banka / kurum"}
                )[["Dönem", "Banka / kurum", "Eksik alan"]]
                st.warning("Simülasyon kapsamında eksik metrik kayıtları var.")
                st.dataframe(missing_table, width="stretch", hide_index=True)
                st.download_button(
                    "Eksik kayıt listesini CSV indir",
                    missing_table.to_csv(index=False).encode("utf-8-sig"),
                    file_name="tbb_metrik_simulasyonu_eksikleri.csv",
                    mime="text/csv",
                    key="simulation_missing_download",
                )
            elif len(zero_denominators):
                st.warning("Eksik kayıt yok; ancak oranı engelleyen sıfır paydalar var.")
            elif multi_entities:
                st.success("Seçilen simülasyon kapsamında eksik kayıt yok.")
            if len(zero_denominators):
                zero_table = zero_denominators.copy()
                zero_table["Dönem"] = zero_table["period_end"].map(simulation_labels)
                zero_table = zero_table.rename(
                    columns={"entity_name": "Banka / kurum"}
                )[["Dönem", "Banka / kurum"]]
                st.markdown("##### Sıfır payda listesi")
                st.dataframe(zero_table, width="stretch", hide_index=True)
                st.download_button(
                    "Sıfır payda listesini CSV indir",
                    zero_table.to_csv(index=False).encode("utf-8-sig"),
                    file_name="tbb_metrik_simulasyonu_sifir_payda.csv",
                    mime="text/csv",
                    key="simulation_zero_denominator_download",
                )
