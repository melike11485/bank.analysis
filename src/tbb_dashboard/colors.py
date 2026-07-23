from __future__ import annotations

import colorsys
import hashlib


COLORS = [
    "#2D68B8",
    "#173B6C",
    "#78B7D1",
    "#4A86B8",
    "#0E7C91",
    "#586C9C",
    "#A9C4E0",
    "#2155A3",
    "#6F8194",
    "#2A8F98",
    "#44527C",
    "#8EAAC2",
    "#254D73",
    "#3F6FA5",
    "#5B8DB8",
    "#88AFCB",
    "#356E7D",
    "#4E7D8F",
    "#6B9EAA",
    "#315C8C",
    "#5670A6",
    "#7F91B8",
    "#2F7F86",
    "#6595C2",
    "#9AB6CC",
    "#344F70",
    "#527E9D",
    "#79A6BE",
]

HALKBANK_COLOR = "#235592"

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


def _generated_blue(name: str, attempt: int = 0) -> str:
    """Generate a stable blue-family color for entities outside the base palette."""
    digest = hashlib.sha1(f"{name}:{attempt}".encode("utf-8")).digest()
    hue = (190 + digest[0] / 255 * 35) / 360
    saturation = 0.42 + digest[1] / 255 * 0.28
    lightness = 0.31 + digest[2] / 255 * 0.34
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def entity_color_map(entities) -> dict[str, str]:
    """Return stable, non-colliding entity colors from a blue-family palette."""
    colors = {
        alias: COLORS[index]
        for index, aliases in enumerate(SYSTEMIC_BANK_COLOR_GROUPS)
        for alias in aliases
    }
    colors["Türkiye Halk Bankası A.Ş."] = HALKBANK_COLOR

    used_colors = set(colors.values())
    names = sorted(
        dict.fromkeys(
            str(entity)
            for entity in entities
            if entity is not None and entity == entity
        )
    )
    for name in names:
        if name in colors:
            continue
        attempt = 0
        color = _generated_blue(name, attempt)
        while color in used_colors:
            attempt += 1
            color = _generated_blue(name, attempt)
        colors[name] = color
        used_colors.add(color)
    return colors
