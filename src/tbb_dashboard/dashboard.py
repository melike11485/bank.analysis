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
COLORS = [
    "#2D68B8",  # gerçek mavi
    "#173B6C",  # lacivert
    "#78B7D1",  # gök mavisi
    "#4A86B8",  # çelik mavisi
    "#0E7C91",  # deniz mavisi
    "#586C9C",  # mat peygamber çiçeği mavisi
    "#A9C4E0",  # sisli mavi
    "#2155A3",  # safir mavisi
    "#6F8194",  # mavi gri
    "#2A8F98",  # mat turkuaz
    "#44527C",  # mor-lacivert
    "#8EAAC2",  # açık çelik mavisi
    "#254D73",  # okyanus mavisi
    "#3F6FA5",  # arduvaz mavisi
    "#5B8DB8",  # yumuşak mavi
    "#88AFCB",  # buz mavisi
    "#356E7D",  # koyu petrol mavisi
    "#4E7D8F",  # dumanlı turkuaz
    "#6B9EAA",  # açık petrol mavisi
    "#315C8C",  # kraliyet mavisi
    "#5670A6",  # mat indigo
    "#7F91B8",  # lavanta mavisi
    "#2F7F86",  # derin turkuaz
    "#6595C2",  # göl mavisi
    "#9AB6CC",  # soluk mavi
    "#344F70",  # gece mavisi
    "#527E9D",  # koyu gök mavisi
    "#79A6BE",  # puslu camgöbeği
]
SIMULATION_COLORS = COLORS[:2]
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
SYSTEMIC_BANK_COLOR_GROUPS = (
    ("Akbank T.A.Ş.",),
    ("Denizbank A.Ş.",),
    ("Türkiye Cumhuriyeti Ziraat Bankası A.Ş.",),
    ("Türkiye Garanti Bankası A.Ş.",),
    ("Türkiye Halk Bankası A.Ş.",),
    ("Türkiye Vakıflar Bankası T.A.O.",),
    ("Türkiye İş Bankası A.Ş.",),
    ("Yapı ve Kredi Bankası A.Ş.",),
    ("QNB Bank A.Ş.", "QNB Finansbank A.Ş."),
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


def entity_color_map(entities) -> dict[str, str]:
    """Return stable bank/institution colors from the shared blue palette."""
    colors = {
        alias: COLORS[index]
        for index, aliases in enumerate(SYSTEMIC_BANK_COLOR_GROUPS)
        for alias in aliases
    }
    available_colors = COLORS[len(SYSTEMIC_BANK_COLOR_GROUPS) :]
    used_colors = set(colors.values())
    names = sorted(
        dict.fromkeys(str(entity) for entity in entities if pd.notna(entity))
    )
    for name in names:
        if name in colors:
            continue
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()
        start = int(digest, 16) % len(available_colors)
        color = next(
            (
                available_colors[(start + offset) % len(available_colors)]
                for offset in range(len(available_colors))
                if available_colors[(start + offset) % len(available_colors)]
                not in used_colors
            ),
            available_colors[start],
        )
        colors[name] = color
        used_colors.add(color)
    return colors


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as connection:
        return pd.read_sql_query(sql, connection, params=params)


@st.cache_data(show_spinner=False, ttl=3600, max_entries=4)
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


@st.cache_data(show_spinner=False, ttl=3600, max_entries=64)
def load_series(metric_key: str, entity_type: str) -> pd.DataFrame:
    frame = query(
        """
        SELECT period_end, period_label, entity_name, value, unit
        FROM observations
        WHERE metric_key = ? AND entity_type = ?
        ORDER BY period_end, entity_name
        """,
        (metric_key, entity_type),
    )
    if not frame.empty:
        frame["period_end"] = pd.to_datetime(frame["period_end"])
        for column in ("period_label", "entity_name", "unit"):
            frame[column] = frame[column].astype("category")
    return frame


@st.cache_data(show_spinner=False, ttl=3600, max_entries=32)
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


def display_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Format numeric cells for display while leaving source data untouched."""
    formatted = frame.copy()
    for column in formatted.select_dtypes(include="number").columns:
        formatted[column] = formatted[column].map(
            lambda value: "" if pd.isna(value) else number_tr(value)
        )
    return formatted


def apply_chart_number_format(figure, decimals: int = 0) -> None:
    """Show rounded Turkish-formatted values directly on every chart."""
    figure.update_layout(separators=",.")
    x_value = f"%{{x:,.{decimals}f}}"
    y_value = f"%{{y:,.{decimals}f}}"
    pie_value = f"%{{value:,.{decimals}f}}"
    scatter_points = sum(
        len(trace.x) if trace.x is not None else 0
        for trace in figure.data
        if trace.type == "scatter"
    )
    # Çok serili çizgi grafiklerde her noktayı etiketlemek okunabilirliği bozar.
    # Tek banka veya az sayıda noktadan oluşan çizgilerde değerler doğrudan görünür.
    show_scatter_labels = scatter_points <= 48
    for trace in figure.data:
        if trace.type == "scatter":
            updates = {
                "hovertemplate": f"%{{fullData.name}}<br>%{{x}}<br>{y_value}<extra></extra>"
            }
            if show_scatter_labels:
                mode = trace.mode or "lines+markers"
                if "text" not in mode:
                    mode = f"{mode}+text"
                updates.update(
                    {
                        "mode": mode,
                        "texttemplate": y_value,
                        "textposition": "top center",
                        "cliponaxis": False,
                        "textfont": {"size": 10, "color": "#5B6B85"},
                    }
                )
            trace.update(**updates)
        elif trace.type == "bar":
            horizontal = getattr(trace, "orientation", None) == "h"
            trace.update(
                texttemplate=x_value if horizontal else y_value,
                textposition="outside",
                cliponaxis=False,
                hovertemplate=(
                    f"%{{fullData.name}}<br>%{{y}}<br>{x_value}<extra></extra>"
                    if horizontal
                    else f"%{{fullData.name}}<br>%{{x}}<br>{y_value}<extra></extra>"
                ),
            )
        elif trace.type == "pie":
            trace.update(
                texttemplate=f"{pie_value}<br>%{{percent:.0%}}",
                textposition="inside",
                hovertemplate=(
                    f"%{{label}}<br>{pie_value}<br>%{{percent:.0%}}<extra></extra>"
                ),
            )
        elif trace.type == "sunburst":
            trace.update(
                texttemplate=f"%{{label}}<br>{pie_value}",
                hovertemplate=f"%{{label}}<br>{pie_value}<extra></extra>",
            )


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
    reset_chart(namespace)


def reset_metric(namespace: str) -> None:
    st.session_state[f"{namespace}_metric"] = None
    reset_chart(namespace)


def reset_chart(namespace: str) -> None:
    prefix = f"{namespace}_chart_type"
    for key in tuple(st.session_state):
        if key == prefix or key.startswith(f"{prefix}_"):
            st.session_state.pop(key, None)


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


def activate_entity_selection_view(namespace: str) -> None:
    view_targets = {
        "period": ("period_view", "Seçilebilir bankalar"),
        "time": ("time_view", "Dönem seyri"),
    }
    target = view_targets.get(namespace)
    if target:
        key, value = target
        st.session_state[key] = value


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


def render_chart_selector(
    namespace: str,
    default: str = "Sütun",
    label: str = "Grafik türü",
) -> str:
    options = ["Çizgi", "Sütun", "Daire"]
    widget_key = f"{namespace}_chart_type"
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default
    return st.radio(
        label,
        options,
        horizontal=True,
        key=widget_key,
    )


def render_entity_filter(
    context: dict,
    namespace: str,
    period: pd.Timestamp,
    default_count: int = 5,
    exact_count: int | None = None,
    default_selection: str = "top",
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
            activate_entity_selection_view(namespace)
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
            activate_entity_selection_view(namespace)
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
    chart_colors = entity_color_map(chart_data["entity_name"])
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
            color="entity_name",
            color_discrete_map=chart_colors,
        )
        figure.update_traces(
            texttemplate="%{value:,.2f}<br>%{percent:.1%}",
            textposition="inside",
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
            color_discrete_map=chart_colors,
        )
        figure = (
            px.line(**options, markers=True)
            if chart_type == "Çizgi"
            else px.bar(**options, barmode="group")
        )
        if chart_type == "Çizgi":
            figure.update_traces(
                texttemplate="%{y:,.2f}",
                textposition="top center",
            )
        else:
            figure.update_traces(
                texttemplate="%{y:,.2f}",
                textposition="outside",
                cliponaxis=False,
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


def add_snapshot_value_labels(figure, chart_type: str) -> None:
    """Keep snapshot values visible without requiring hover interaction."""
    if chart_type == "Daire":
        figure.update_traces(
            texttemplate="%{value:,.2f}<br>%{percent:.1%}",
            textposition="inside",
        )
    elif chart_type == "Çizgi":
        figure.update_traces(
            texttemplate="%{y:,.2f}",
            textposition="top center",
        )
    else:
        figure.update_traces(
            texttemplate="%{y:,.2f}",
            textposition="outside",
            cliponaxis=False,
        )


def render_downloadable_chart(
    figure,
    export_data: pd.DataFrame,
    key: str,
    file_stem: str,
    value_decimals: int = 0,
) -> None:
    """Render a chart with consistent PNG, CSV and interactive HTML exports."""
    apply_chart_number_format(figure, decimals=value_decimals)
    st.plotly_chart(
        figure,
        use_container_width=True,
        key=key,
        config={
            "displaylogo": False,
            "toImageButtonOptions": {
                "format": "png",
                "filename": file_stem,
                "scale": 2,
            },
        },
    )
    st.caption(
        "Grafiği PNG olarak indirmek için sağ üstteki kamera simgesini kullanın."
    )
    st.download_button(
        "Grafik verisini CSV indir",
        export_data.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{file_stem}.csv",
        mime="text/csv",
        key=f"{key}_csv_download",
    )
    if st.checkbox(
        "Etkileşimli HTML indirme bağlantısını hazırla",
        key=f"{key}_prepare_html",
        help="Büyük Plotly HTML çıktısı belleği artırdığı için yalnızca gerektiğinde hazırlanır.",
    ):
        st.download_button(
            "Etkileşimli grafiği HTML indir",
            figure.to_html(full_html=True, include_plotlyjs="cdn"),
            file_name=f"{file_stem}.html",
            mime="text/html",
            key=f"{key}_html_download",
        )


def standard_export_frame(
    frame: pd.DataFrame,
    value_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Keep chart exports compact and use user-facing Turkish column names."""
    value_columns = value_columns or ["value"]
    columns = [
        column
        for column in ["period_label", "entity_name", *value_columns, "unit"]
        if column in frame.columns
    ]
    return frame[columns].rename(
        columns={
            "period_label": "Dönem",
            "entity_name": "Banka / kurum",
            "value": "Değer",
            "unit": "Birim",
            "quarterly_change": "Çeyreklik değişim (%)",
            "annual_change": "Yıllık değişim (%)",
            "result": "Formül sonucu",
        }
    )


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
    st.button(
        "Veritabanını yenile",
        use_container_width=True,
        on_click=st.cache_data.clear,
        help="Veritabanından okunan güncel verileri ve filtre kataloğunu yeniden yükler.",
    )
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

main_view = st.radio(
    "Analiz türü",
    [
        "Dönemsel analiz",
        "Zaman analizi",
        "Özelleştirilebilir metrikler",
        "Metrik simülasyonu",
    ],
    horizontal=True,
    key="main_view",
)


if main_view == "Dönemsel analiz":
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
                default_selection="top",
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
        period_view_options = [
            "Sistemik 9 banka",
            "Seçilebilir bankalar",
            "Veri tablosu",
            "Veri kalitesi",
        ]
        if st.session_state.get("period_view") not in period_view_options:
            st.session_state["period_view"] = "Sistemik 9 banka"
        period_view = st.radio(
            "Dönemsel görünüm",
            period_view_options,
            horizontal=True,
            key="period_view",
        )
        if period_view == "Sistemik 9 banka":
            systemic_names = systemic_entities(
                ranking_all["entity_name"].dropna().unique().tolist()
            )
            systemic_snapshot = ranking_all[
                ranking_all["entity_name"].isin(systemic_names)
            ].sort_values("value", ascending=False)
            systemic_order = systemic_snapshot["entity_name"].tolist()
            systemic_color_map = entity_color_map(systemic_names)
            if chart_type == "Daire":
                systemic_chart_data = systemic_snapshot.copy()
                systemic_chart_data["pie_value"] = systemic_chart_data["value"].abs()
                systemic_figure = px.pie(
                    systemic_chart_data,
                    names="entity_name",
                    values="pie_value",
                    hole=0.38,
                    color="entity_name",
                    color_discrete_map=systemic_color_map,
                )
            elif chart_type == "Çizgi":
                systemic_figure = px.line(
                    systemic_snapshot,
                    x="entity_name",
                    y="value",
                    color="entity_name",
                    markers=True,
                    labels={"entity_name": "Banka / kurum", "value": unit},
                    color_discrete_map=systemic_color_map,
                    category_orders={"entity_name": systemic_order},
                )
                systemic_figure.update_xaxes(tickangle=-25)
            else:
                systemic_figure = px.bar(
                    systemic_snapshot,
                    x="entity_name",
                    y="value",
                    labels={"entity_name": "Banka / kurum", "value": unit},
                    color="entity_name",
                    color_discrete_map=systemic_color_map,
                    category_orders={"entity_name": systemic_order},
                )
                systemic_figure.update_xaxes(tickangle=-25)
            add_snapshot_value_labels(systemic_figure, chart_type)
            systemic_figure.update_layout(
                height=520,
                margin=dict(l=10, r=70, t=55, b=10),
                title=(
                    f"{context['period_labels'][analysis_date]} • Sistemik öneme "
                    "sahip 9 banka"
                ),
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            render_downloadable_chart(
                systemic_figure,
                standard_export_frame(systemic_snapshot),
                "period_systemic_chart",
                "tbb_donemsel_sistemik_9_banka",
            )
        elif period_view == "Seçilebilir bankalar":
            if snapshot.empty:
                st.info("Grafik için en az bir banka veya kurum seçin.")
            else:
                selected_color_map = entity_color_map(snapshot["entity_name"])
                selected_order = snapshot["entity_name"].tolist()
                if chart_type == "Daire":
                    chart_data = snapshot.copy()
                    chart_data["pie_value"] = chart_data["value"].abs()
                    figure = px.pie(
                        chart_data,
                        names="entity_name",
                        values="pie_value",
                        hole=0.38,
                        color="entity_name",
                        color_discrete_map=selected_color_map,
                    )
                elif chart_type == "Çizgi":
                    figure = px.line(
                        snapshot,
                        x="entity_name",
                        y="value",
                        color="entity_name",
                        markers=True,
                        labels={"entity_name": "Banka / kurum", "value": unit},
                        color_discrete_map=selected_color_map,
                        category_orders={"entity_name": selected_order},
                    )
                    figure.update_xaxes(tickangle=-25)
                else:
                    figure = px.bar(
                        snapshot,
                        x="entity_name",
                        y="value",
                        labels={"entity_name": "Banka / kurum", "value": unit},
                        color="entity_name",
                        color_discrete_map=selected_color_map,
                        category_orders={"entity_name": selected_order},
                    )
                    figure.update_xaxes(tickangle=-25)
                if chart_type == "Sütun":
                    figure.update_traces(
                        texttemplate="%{y:,.2f}",
                        textposition="outside",
                        cliponaxis=False,
                    )
                else:
                    add_snapshot_value_labels(figure, chart_type)
                figure.update_layout(
                    height=500,
                    margin=dict(l=10, r=35, t=35, b=10),
                    plot_bgcolor="white",
                    paper_bgcolor="white",
                )
                render_downloadable_chart(
                    figure,
                    standard_export_frame(snapshot),
                    "period_single_chart",
                    "tbb_donemsel_secilen_bankalar",
                )
        period_table = ranking_all[["Sıra", "entity_name", "value", "unit"]].rename(
            columns={"entity_name": "Banka / kurum", "value": "Değer", "unit": "Birim"}
        )
        if period_view == "Veri tablosu":
            st.dataframe(display_table(period_table), width="stretch", hide_index=True)
            st.download_button(
                "Dönemsel analiz verisini CSV indir",
                period_table.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_donemsel_analiz.csv",
                mime="text/csv",
            )
        elif period_view == "Veri kalitesi":
            render_quality(
                ranking_all,
                [analysis_date],
                context["all_entities"],
                context["period_labels"],
                "Bu dönemde tüm banka/kurumlar için veri mevcut.",
                "period",
            )
            render_source_availability_notes(context)


elif main_view == "Zaman analizi":
    st.subheader("Zaman analizi")
    context = render_metric_filters("time", catalog)
    if context:
        dates = context["period_dates"]
        time_view_options = [
            "Sistemik 9 banka",
            "Dönem seyri",
            "Başlangıç–bitiş",
            "Çeyreklik",
            "Yıllık",
            "Veri tablosu",
            "Veri kalitesi",
        ]
        if st.session_state.get("time_view") not in time_view_options:
            st.session_state["time_view"] = "Sistemik 9 banka"
        is_change_view = st.session_state["time_view"] in {"Çeyreklik", "Yıllık"}
        if is_change_view:
            c1, c2, c3 = st.columns([1, 1, 3])
            c4 = None
        else:
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
        chart_type = None
        if c4 is not None:
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
        time_view = st.radio(
            "Zaman görünümü",
            time_view_options,
            horizontal=True,
            key="time_view",
        )
        if time_view == "Sistemik 9 banka":
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
                render_downloadable_chart(
                    systemic_figure,
                    standard_export_frame(systemic_data),
                    "time_systemic_chart",
                    "tbb_zaman_sistemik_9_banka",
                )
        elif time_view == "Dönem seyri":
            if comparison_data.empty:
                st.info("Grafik için en az bir banka veya kurum seçin.")
            elif chart_type == "Daire":
                chart_data = comparison_data.copy()
                chart_data[["period_label", "entity_name"]] = chart_data[
                    ["period_label", "entity_name"]
                ].astype("string")
                chart_data["chart_value"] = chart_data["value"].abs()
                figure = px.sunburst(
                    chart_data,
                    path=["period_label", "entity_name"],
                    values="chart_value",
                    color="period_label",
                    color_discrete_sequence=COLORS,
                )
                figure.update_traces(
                    texttemplate="%{label}<br>%{value:,.2f}",
                )
                figure.update_layout(height=580)
                render_downloadable_chart(
                    figure,
                    standard_export_frame(comparison_data),
                    "time_trend_chart",
                    "tbb_zaman_donem_seyri",
                )
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
                render_downloadable_chart(
                    figure,
                    standard_export_frame(comparison_data),
                    "time_trend_chart",
                    "tbb_zaman_donem_seyri",
                )
        if time_view in {"Çeyreklik", "Yıllık"}:
            change_view_config = {
                "Çeyreklik": (
                    "Çeyreklik",
                    "quarterly_change",
                    "Çeyreklik değişim (%)",
                    "Çeyreklik değişim hesaplanamadı.",
                    False,
                ),
                "Yıllık": (
                    "Yıllık",
                    "annual_change",
                    "Yıllık değişim (Aralık–Aralık, %)",
                    "Aralık–Aralık yıllık değişim için önceki yılın Aralık verisi gerekir.",
                    True,
                ),
            }
            period_name, change_column, change_label, empty_text, annual_only = change_view_config[time_view]
            chart_frame = analysis_data.copy()
            chart_periods = comparison_periods.copy()
            if annual_only:
                chart_frame = analysis_data[
                    analysis_data["period_end"].dt.month == 12
                ].copy()
                chart_periods = [
                    period for period in comparison_periods if period.month == 12
                ]

            value_title_col, value_selector_col = st.columns([3, 2])
            with value_title_col:
                st.subheader(f"{period_name} değer")
            with value_selector_col:
                value_chart_type = render_chart_selector(
                    f"time_{period_name.lower()}_value",
                    default="Sütun",
                    label="Değer grafiği",
                )
            value_figure = make_time_figure(
                chart_frame,
                "value",
                context["unit"],
                value_chart_type,
                chart_periods,
                context["period_labels"],
                end_date,
            )
            if value_figure is None:
                st.info(f"{period_name} değer grafiği için veri bulunamadı.")
            else:
                render_downloadable_chart(
                    value_figure,
                    standard_export_frame(chart_frame),
                    f"time_{period_name.lower()}_value_chart_{value_chart_type}",
                    f"tbb_zaman_{period_name.lower()}_deger",
                )

            st.divider()
            change_title_col, change_selector_col = st.columns([3, 2])
            with change_title_col:
                st.subheader(f"{period_name} değişim")
            with change_selector_col:
                change_chart_type = render_chart_selector(
                    f"time_{period_name.lower()}_change",
                    default="Çizgi",
                    label="Değişim grafiği",
                )
            change_figure = make_time_figure(
                chart_frame,
                change_column,
                change_label,
                change_chart_type,
                chart_periods,
                context["period_labels"],
                end_date,
            )
            if change_figure is None:
                st.info(empty_text)
            else:
                render_downloadable_chart(
                    change_figure,
                    standard_export_frame(chart_frame, [change_column]),
                    f"time_{change_column}_chart_{change_chart_type}",
                    f"tbb_zaman_{change_column}",
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
        if time_view == "Başlangıç–bitiş":
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
                render_downloadable_chart(
                    endpoint_figure,
                    standard_export_frame(endpoints),
                    "time_endpoint_chart",
                    "tbb_zaman_baslangic_bitis",
                )
            st.dataframe(display_table(summary), width="stretch", hide_index=True)
            st.download_button(
                "Başlangıç–bitiş tablosunu CSV indir",
                summary.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_zaman_baslangic_bitis_ozeti.csv",
                mime="text/csv",
                key="time_endpoint_table_download",
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
        if time_view == "Veri tablosu":
            st.markdown("**Ara dönemler dahil tüm kayıtlar**")
            st.dataframe(display_table(detail), width="stretch", hide_index=True)
            st.download_button(
                "Zaman analizi verisini CSV indir",
                detail.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_zaman_analizi.csv",
                mime="text/csv",
            )
        elif time_view == "Veri kalitesi":
            render_quality(
                analysis_data,
                comparison_periods,
                entities,
                context["period_labels"],
                "Seçilen zaman aralığında eksik banka/kurum-dönem kaydı yok.",
                "time",
            )
            render_source_availability_notes(context)


elif main_view == "Özelleştirilebilir metrikler":
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

        calculator_view = st.radio(
            "Hesaplama görünümü",
            ["Grafik", "Veri tablosu", "Veri kalitesi"],
            horizontal=True,
            key="calculator_view",
        )
        if calculator_view == "Grafik":
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
                    render_downloadable_chart(
                        figure,
                        standard_export_frame(
                            calculation,
                            [*selected_metrics.keys(), "result"],
                        ),
                        "calculator_result_chart",
                        "tbb_ozellestirilebilir_metrik_grafigi",
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
        if calculator_view == "Veri tablosu":
            if formula_error:
                st.error(formula_error)
            st.dataframe(
                display_table(calculator_table), width="stretch", hide_index=True
            )
            st.download_button(
                "Hesaplama sonucunu CSV indir",
                calculator_table.to_csv(index=False).encode("utf-8-sig"),
                file_name="tbb_ozellestirilebilir_metrik.csv",
                mime="text/csv",
            )

        elif calculator_view == "Veri kalitesi":
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


elif main_view == "Metrik simülasyonu":
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
        simulation_metric_a_name = calculator_lookup[simulation_metric_a]
        simulation_metric_b_name = calculator_lookup[simulation_metric_b]
        simulation_entity_type = s3.radio(
            "Karşılaştırma düzeyi",
            list(ENTITY_LABELS),
            format_func=lambda item: ENTITY_LABELS[item],
            horizontal=True,
            key="simulation_entity_type",
            on_change=reset_simulation_scope,
        )
        st.caption(
            f"Pay: {simulation_metric_a_name} • Payda: {simulation_metric_b_name}"
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
            ["Yüzde oran ((A / B) × 100)", "Oran (A / B)"],
            index=0,
            horizontal=True,
            key="simulation_operation",
        )
        simulation_chart_type = chart_col.radio(
            "Grafik türü",
            ["Çizgi", "Sütun"],
            index=1,
            horizontal=True,
            key="simulation_chart_type",
        )

        shock_a_col, shock_b_col = st.columns(2)
        shock_a = shock_a_col.number_input(
            f"{simulation_metric_a_name} değişimi (%)",
            min_value=-100.0,
            max_value=1000.0,
            value=0.0,
            step=1.0,
            help="Örneğin yüzde 10 artış için 10, yüzde 5 düşüş için -5 yazın.",
            key="simulation_shock_a",
        )
        shock_b = shock_b_col.number_input(
            f"{simulation_metric_b_name} değişimi (%)",
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
            chart_data["Sonuç etiketi"] = chart_data["Sonuç"].map(
                lambda value: "" if pd.isna(value) else number_tr(value, 4)
            )
            simulation_entity_color_map = entity_color_map(
                chart_data["entity_name"] if multiple else []
            )
            if simulation_chart_type == "Çizgi":
                if multiple:
                    figure = px.line(
                        chart_data,
                        x="period_label",
                        y="Sonuç",
                        text="Sonuç etiketi",
                        color="entity_name",
                        line_dash="Senaryo",
                        markers=True,
                        color_discrete_map=simulation_entity_color_map,
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
                        text="Sonuç etiketi",
                        color="Senaryo",
                        markers=True,
                        color_discrete_sequence=SIMULATION_COLORS,
                        labels={"period_label": "Dönem", "Sonuç": operation},
                    )
            elif multiple:
                figure = px.bar(
                    chart_data,
                    x="period_label",
                    y="Sonuç",
                    text="Sonuç etiketi",
                    color="entity_name",
                    pattern_shape="Senaryo",
                    barmode="group",
                    color_discrete_map=simulation_entity_color_map,
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
                    text="Sonuç etiketi",
                    color="Senaryo",
                    barmode="group",
                    color_discrete_sequence=SIMULATION_COLORS,
                    labels={"period_label": "Dönem", "Sonuç": operation},
                )
            figure.update_layout(
                height=440,
                plot_bgcolor="white",
                paper_bgcolor="white",
                legend_title_text="",
            )
            if simulation_chart_type == "Çizgi":
                figure.update_traces(
                    texttemplate="%{text}",
                    textposition="top center",
                )
            else:
                figure.update_traces(
                    texttemplate="%{text}",
                    textposition="outside",
                    cliponaxis=False,
                )
            simulation_export = frame[
                [
                    "period_label",
                    "entity_name",
                    "Metrik A",
                    "Simüle Metrik A",
                    "Metrik B",
                    "Simüle Metrik B",
                    "Mevcut",
                    "Simülasyon",
                    "Değişim (%)",
                ]
            ].rename(
                columns={
                    "period_label": "Dönem",
                    "entity_name": "Banka / kurum",
                    "Metrik A": f"Pay — {simulation_metric_a_name} (mevcut)",
                    "Simüle Metrik A": f"Pay — {simulation_metric_a_name} (simüle)",
                    "Metrik B": f"Payda — {simulation_metric_b_name} (mevcut)",
                    "Simüle Metrik B": f"Payda — {simulation_metric_b_name} (simüle)",
                }
            )
            render_downloadable_chart(
                figure,
                simulation_export,
                f"{key}_{simulation_chart_type}",
                (
                    "tbb_metrik_simulasyonu_coklu_banka"
                    if multiple
                    else "tbb_metrik_simulasyonu_tek_banka"
                ),
                value_decimals=4,
            )

        st.markdown("#### Banka/kurum filtresi")
        selected_entities = render_entity_filter(
            simulation_context,
            "simulation",
            simulation_end,
            default_selection="halkbank",
        )
        selected_scenario = valid_scenarios[
            valid_scenarios["entity_name"].isin(selected_entities)
        ]
        simulation_view_options = [
            "Simülasyon grafiği",
            "Veri tablosu",
            "Veri kalitesi",
        ]
        if st.session_state.get("simulation_view") not in simulation_view_options:
            st.session_state["simulation_view"] = "Simülasyon grafiği"
        simulation_view = st.radio(
            "Simülasyon görünümü",
            simulation_view_options,
            horizontal=True,
            key="simulation_view",
        )
        if simulation_view == "Simülasyon grafiği":
            if selected_scenario.empty:
                st.warning(
                    "Grafik için en az bir banka/kurum seçin; seçili dönemlerde "
                    "iki metriğin ve sıfırdan farklı paydanın bulunduğundan emin olun."
                )
            else:
                if len(selected_entities) == 1:
                    last_row = selected_scenario.sort_values("period_end").iloc[-1]
                    r1, r2, r3 = st.columns(3)
                    r1.metric(
                        simulation_metric_a_name,
                        number_tr(last_row["Simüle Metrik A"]),
                    )
                    r2.metric(
                        simulation_metric_b_name,
                        number_tr(last_row["Simüle Metrik B"]),
                    )
                    r3.metric(
                        "Simüle oran",
                        (
                            f"%{number_tr(last_row['Simülasyon'], 4)}"
                            if multiplier == 100
                            else number_tr(last_row["Simülasyon"], 4)
                        ),
                        delta=f"%{number_tr(last_row['Değişim (%)'], 4)}",
                    )
                simulation_chart(
                    selected_scenario,
                    "metric_simulation_chart",
                    multiple=len(selected_entities) > 1,
                )

        elif simulation_view == "Veri tablosu":
            if not selected_entities:
                st.info("Veri tablosu için en az bir banka/kurum seçin.")
            else:
                simulation_table = selected_scenario.rename(
                    columns={
                        "period_label": "Dönem",
                        "entity_name": "Banka / kurum",
                        "Metrik A": f"Pay — {simulation_metric_a_name} (mevcut)",
                        "Simüle Metrik A": f"Pay — {simulation_metric_a_name} (simüle)",
                        "Metrik B": f"Payda — {simulation_metric_b_name} (mevcut)",
                        "Simüle Metrik B": f"Payda — {simulation_metric_b_name} (simüle)",
                    }
                )[
                    [
                        "Dönem",
                        "Banka / kurum",
                        f"Pay — {simulation_metric_a_name} (mevcut)",
                        f"Pay — {simulation_metric_a_name} (simüle)",
                        f"Payda — {simulation_metric_b_name} (mevcut)",
                        f"Payda — {simulation_metric_b_name} (simüle)",
                        "Mevcut",
                        "Simülasyon",
                        "Değişim (%)",
                    ]
                ]
                simulation_table_display = display_table(simulation_table)
                for column in ("Mevcut", "Simülasyon", "Değişim (%)"):
                    simulation_table_display[column] = simulation_table[column].map(
                        lambda value: "" if pd.isna(value) else number_tr(value, 4)
                    )
                st.dataframe(
                    simulation_table_display, width="stretch", hide_index=True
                )
                st.download_button(
                    "Simülasyon verisini CSV indir",
                    simulation_table.to_csv(index=False).encode("utf-8-sig"),
                    file_name="tbb_metrik_simulasyonu.csv",
                    mime="text/csv",
                    key="simulation_table_download",
                )

        elif simulation_view == "Veri kalitesi":
            if not selected_entities:
                st.info("Veri kalitesini ölçmek için en az bir banka/kurum seçin.")
            selected_periods = [
                period
                for period in simulation_periods
                if simulation_start <= period <= simulation_end
            ]
            expected = pd.MultiIndex.from_product(
                [selected_periods, selected_entities],
                names=["period_end", "entity_name"],
            )
            missing_parts = []
            for metric_label, metric_frame, value_column in (
                ("Metrik A", scope_a, "Metrik A"),
                ("Metrik B", scope_b, "Metrik B"),
            ):
                actual = pd.MultiIndex.from_frame(
                    metric_frame[
                        metric_frame["entity_name"].isin(selected_entities)
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
                all_scenarios["entity_name"].isin(selected_entities)
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
            elif selected_entities:
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
