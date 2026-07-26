"""Shared light-theme image primitives for LevelUpPvp replies."""

import os

try:
    from astrbot.core.utils.t2i.local_strategy import FontManager
except ImportError:
    FontManager = None


WIDTH = 760
PAGE_PADDING = 28
CARD_PADDING = 20
SECTION_GAP = 16
SUPPORTED_EMOJIS = ("❤️‍🔥", "⚔️", "🛡️", "⏱️", "🏃", "💥", "✨", "🏆")

COLORS = {
    "page": (255, 255, 255),
    "page_top": (255, 255, 255),
    "page_bottom": (255, 255, 255),
    "card": (248, 250, 252),
    "card_alt": (245, 247, 250),
    "border": (218, 225, 234),
    "divider": (229, 233, 240),
    "text": (31, 41, 55),
    "text_2": (91, 103, 120),
    "text_3": (132, 145, 162),
    "brand": (111, 126, 239),
    "brand_strong": (79, 100, 232),
    "brand_soft": (235, 238, 255),
    "attacker": (24, 119, 181),
    "defender": (190, 91, 31),
    "winner": (168, 112, 0),
    "hp": (34, 158, 111),
    "mp": (63, 116, 224),
    "sp": (205, 146, 28),
    "bar_bg": (226, 231, 238),
    "positive": (22, 134, 92),
    "negative": (190, 54, 66),
}


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except (OSError, ValueError):
                pass
    if FontManager is not None:
        try:
            font = FontManager.get_font(size)
            if font is not None:
                return font
        except Exception:
            pass
    return ImageFont.load_default()


def _load_emoji_font(size: int):
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/seguiemj.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
        "/usr/share/fonts/noto/NotoEmoji-Regular.ttf",
        "NotoEmoji-Regular.ttf",
        "Symbola.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return None


def _text_units(text: str) -> list[str]:
    """Keep supported multi-codepoint emoji sequences intact."""
    units = []
    index = 0
    while index < len(text):
        emoji = next(
            (item for item in SUPPORTED_EMOJIS if text.startswith(item, index)),
            None,
        )
        if emoji:
            units.append(emoji)
            index += len(emoji)
        else:
            units.append(text[index])
            index += 1
    return units


def _text_width(draw, text: str, font, emoji_font=None) -> int:
    width = 0
    for unit in _text_units(str(text)):
        unit_font = emoji_font if emoji_font and unit in SUPPORTED_EMOJIS else font
        bbox = draw.textbbox((0, 0), unit, font=unit_font)
        width += bbox[2] - bbox[0]
    return width


def _line_height(font) -> int:
    bbox = font.getbbox("测Ag")
    return max(1, bbox[3] - bbox[1])


def _draw_text(draw, position, text: str, font, fill, emoji_font=None) -> None:
    x, y = position
    for unit in _text_units(str(text)):
        is_emoji = emoji_font is not None and unit in SUPPORTED_EMOJIS
        unit_font = emoji_font if is_emoji else font
        kwargs = {"font": unit_font, "fill": fill}
        if is_emoji:
            kwargs["embedded_color"] = True
        try:
            draw.text((x, y), unit, **kwargs)
        except (OSError, ValueError):
            kwargs.pop("embedded_color", None)
            kwargs["font"] = font
            draw.text((x, y), unit, **kwargs)
            unit_font = font
        bbox = draw.textbbox((0, 0), unit, font=unit_font)
        x += bbox[2] - bbox[0]


def _wrap_text(draw, text: str, font, max_width: int, emoji_font=None) -> list[str]:
    lines = []
    current = ""
    for unit in _text_units(str(text)):
        candidate = current + unit
        if current and _text_width(draw, candidate, font, emoji_font) > max_width:
            lines.append(current)
            current = unit
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _fit_text(draw, text: str, font, max_width: int) -> str:
    text = str(text)
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "…"
    while text and _text_width(draw, text + suffix, font) > max_width:
        text = text[:-1]
    return text + suffix


def _rounded_card(draw, box, fill=None) -> None:
    draw.rounded_rectangle(
        box,
        radius=12,
        fill=fill or COLORS["card"],
        outline=COLORS["border"],
        width=1,
    )


def _draw_page_header(draw, *, title: str, label: str, height: int = 68) -> None:
    draw.rectangle((0, 0, WIDTH, height), fill=COLORS["page"])
    draw.line(
        (0, height - 1, WIDTH, height - 1),
        fill=COLORS["divider"],
        width=1,
    )
    font_nav = _load_font(16, bold=True)
    font_small = _load_font(14)
    draw.rounded_rectangle(
        (PAGE_PADDING, 18, PAGE_PADDING + 32, 50),
        radius=8,
        fill=COLORS["brand_strong"],
    )
    draw.text(
        (PAGE_PADDING + 10, 22),
        "L",
        font=font_nav,
        fill=(255, 255, 255),
    )
    draw.text(
        (PAGE_PADDING + 44, 23),
        title,
        font=font_nav,
        fill=COLORS["text"],
    )
    label_width = _text_width(draw, label, font_small)
    draw.text(
        (WIDTH - PAGE_PADDING - label_width, 25),
        label,
        font=font_small,
        fill=COLORS["text_2"],
    )


def render_text_card(text: str, title: str = "LevelUpPvp"):
    """Render arbitrary reply text as one complete light-theme image."""
    from PIL import Image, ImageDraw

    text = str(text)
    font_body = _load_font(18)
    font_emoji = _load_emoji_font(18)
    measure_image = Image.new("RGB", (1, 1))
    measure = ImageDraw.Draw(measure_image)
    text_width = WIDTH - PAGE_PADDING * 2 - CARD_PADDING * 2
    wrapped_lines = []
    for source_line in text.split("\n"):
        wrapped_lines.extend(
            _wrap_text(measure, source_line, font_body, text_width, font_emoji)
        )

    line_height = _line_height(font_body) + 10
    nav_height = 68
    body_height = max(72, CARD_PADDING * 2 + len(wrapped_lines) * line_height)
    total_height = nav_height + PAGE_PADDING + body_height + PAGE_PADDING
    image = Image.new("RGB", (WIDTH, total_height), COLORS["page"])
    draw = ImageDraw.Draw(image)
    _draw_page_header(draw, title=title, label="文字回复", height=nav_height)

    card_top = nav_height + PAGE_PADDING
    _rounded_card(
        draw,
        (PAGE_PADDING, card_top, WIDTH - PAGE_PADDING, card_top + body_height),
    )
    y = card_top + CARD_PADDING
    for line in wrapped_lines:
        _draw_text(
            draw,
            (PAGE_PADDING + CARD_PADDING, y),
            line,
            font_body,
            COLORS["text"],
            font_emoji,
        )
        y += line_height
    return image
