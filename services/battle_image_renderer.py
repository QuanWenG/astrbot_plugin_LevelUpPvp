"""Render a compact battle report with the shared LevelUpPvp light theme."""

if "." in (__package__ or ""):
    from .image_renderer import (
        CARD_PADDING,
        COLORS,
        PAGE_PADDING,
        SECTION_GAP,
        WIDTH,
        _draw_page_header,
        _draw_text,
        _fit_text,
        _line_height,
        _load_emoji_font,
        _load_font,
        _rounded_card,
        _text_units,
        _text_width,
        _wrap_text,
    )
else:
    from services.image_renderer import (
        CARD_PADDING,
        COLORS,
        PAGE_PADDING,
        SECTION_GAP,
        WIDTH,
        _draw_page_header,
        _draw_text,
        _fit_text,
        _line_height,
        _load_emoji_font,
        _load_font,
        _rounded_card,
        _text_units,
        _text_width,
        _wrap_text,
    )


RENDERER_REVISION = "astrbot-card-v3-light"


def _display_name(user) -> str:
    name = user.nickname or user.user_id
    if name == user.user_id and len(name) > 8:
        return f"{name[:3]}...{name[-2:]}"
    return name


def _draw_bar(
    draw,
    *,
    x: int,
    y: int,
    width: int,
    label: str,
    value: int,
    maximum: int,
    color,
    font,
) -> None:
    maximum = max(1, int(maximum))
    value = int(value)
    display_value = f"{value}/{maximum}"
    draw.text((x, y), label, font=font, fill=COLORS["text_2"])
    value_width = _text_width(draw, display_value, font)
    draw.text(
        (x + width - value_width, y),
        display_value,
        font=font,
        fill=COLORS["text"],
    )
    bar_y = y + 23
    draw.rounded_rectangle(
        (x, bar_y, x + width, bar_y + 8),
        radius=4,
        fill=COLORS["bar_bg"],
    )
    ratio = min(1.0, max(0.0, value / maximum))
    if ratio > 0:
        draw.rounded_rectangle(
            (x, bar_y, x + max(8, round(width * ratio)), bar_y + 8),
            radius=4,
            fill=color,
        )


def _resource_values(result):
    sim = result.simulation
    if sim is None:
        attacker_max_hp = max(1, int(getattr(result.attacker, "hp", 50)) * 10)
        defender_max_hp = max(1, int(getattr(result.defender, "hp", 50)) * 10)
        return (
            (attacker_max_hp // 2, attacker_max_hp, 20, 100, 50, 100, 0),
            (defender_max_hp // 2, defender_max_hp, 20, 100, 50, 100, 0),
        )

    def values(snapshot, hp, mp, sp, damage):
        max_hp = max(1, int(getattr(snapshot, "max_hp", max(1, hp))))
        max_mp = max(1, int(getattr(snapshot, "max_mp", max(1, mp))))
        max_sp = max(1, int(getattr(snapshot, "max_sp", max(1, sp))))
        return (hp, max_hp, mp, max_mp, sp, max_sp, damage)

    return (
        values(
            sim.attacker,
            sim.attacker_remaining_hp,
            sim.attacker_remaining_mana,
            sim.attacker_remaining_stamina,
            sim.attacker_damage_dealt,
        ),
        values(
            sim.defender,
            sim.defender_remaining_hp,
            sim.defender_remaining_mana,
            sim.defender_remaining_stamina,
            sim.defender_damage_dealt,
        ),
    )


def render_battle_report(result, max_log_lines: int = 8):
    """Build an AstrBot-style battle report as a Pillow image."""
    from PIL import Image, ImageDraw

    attacker_name = _display_name(result.attacker)
    defender_name = _display_name(result.defender)
    winner_name = _display_name(result.winner)
    loser_name = _display_name(result.loser)
    log_lines = [
        str(line).strip()
        for line in (result.battle_log or [])[:max_log_lines]
        if str(line).strip()
    ]
    if not log_lines:
        log_lines = [f"{attacker_name} 与 {defender_name} 的战斗已经结束。"]

    font_nav = _load_font(16, bold=True)
    font_title = _load_font(28, bold=True)
    font_name = _load_font(22, bold=True)
    font_section = _load_font(18, bold=True)
    font_body = _load_font(16)
    font_small = _load_font(14)
    font_body_emoji = _load_emoji_font(16)

    measure_image = Image.new("RGB", (1, 1))
    measure = ImageDraw.Draw(measure_image)
    content_width = WIDTH - PAGE_PADDING * 2
    log_text_width = content_width - CARD_PADDING * 2 - 18
    wrapped_logs = [
        _wrap_text(measure, line, font_body, log_text_width, font_body_emoji)
        for line in log_lines
    ]
    log_line_height = _line_height(font_body) + 8
    log_height = (
        54
        + sum(len(lines) * log_line_height + 4 for lines in wrapped_logs)
        + 14
    )

    level_items = []
    for item in result.level_ups or []:
        level_items.append((f"升级 Lv.{item.from_level} → Lv.{item.to_level}", COLORS["positive"]))
    for item in result.level_downs or []:
        level_items.append((f"冻结 Lv.{item.from_level} → Lv.{item.to_level}", COLORS["negative"]))

    nav_height = 68
    match_height = 142
    resource_height = 205
    result_height = (
        134
        + (24 if getattr(result, "is_counterattack", False) else 0)
        + len(level_items) * 24
    )
    total_height = (
        nav_height
        + PAGE_PADDING
        + match_height
        + SECTION_GAP
        + log_height
        + SECTION_GAP
        + resource_height
        + SECTION_GAP
        + result_height
        + PAGE_PADDING
    )

    image = Image.new("RGB", (WIDTH, total_height), COLORS["page"])
    draw = ImageDraw.Draw(image)
    _draw_page_header(
        draw,
        title="LevelUpPvp",
        label="战斗结算 · V3",
        height=nav_height,
    )

    y = nav_height + PAGE_PADDING
    _rounded_card(draw, (PAGE_PADDING, y, WIDTH - PAGE_PADDING, y + match_height))
    title = "对 战"
    title_width = _text_width(draw, title, font_small)
    draw.text(
        ((WIDTH - title_width) / 2, y + 17),
        title,
        font=font_small,
        fill=COLORS["text_3"],
    )
    left_center = WIDTH * 0.27
    right_center = WIDTH * 0.73
    vs_center = WIDTH * 0.5
    attacker_display = _fit_text(draw, attacker_name, font_name, 215)
    defender_display = _fit_text(draw, defender_name, font_name, 215)
    attacker_width = _text_width(draw, attacker_display, font_name)
    defender_width = _text_width(draw, defender_display, font_name)
    draw.text(
        (left_center - attacker_width / 2, y + 48),
        attacker_display,
        font=font_name,
        fill=COLORS["attacker"],
    )
    draw.rounded_rectangle(
        (vs_center - 27, y + 43, vs_center + 27, y + 81),
        radius=19,
        fill=COLORS["brand_strong"],
    )
    vs_width = _text_width(draw, "VS", font_nav)
    draw.text(
        (vs_center - vs_width / 2, y + 51),
        "VS",
        font=font_nav,
        fill=(255, 255, 255),
    )
    draw.text(
        (right_center - defender_width / 2, y + 48),
        defender_display,
        font=font_name,
        fill=COLORS["defender"],
    )
    attacker_strategy = _fit_text(
        draw,
        result.attacker_strategy
        + ("（随机）" if getattr(result, "attacker_strategy_random", False) else ""),
        font_small,
        270,
    )
    defender_strategy = _fit_text(
        draw,
        result.defender_strategy
        + ("（随机）" if getattr(result, "defender_strategy_random", False) else ""),
        font_small,
        270,
    )
    attacker_strategy_width = _text_width(draw, attacker_strategy, font_small)
    defender_strategy_width = _text_width(draw, defender_strategy, font_small)
    draw.text(
        (left_center - attacker_strategy_width / 2, y + 96),
        attacker_strategy,
        font=font_small,
        fill=COLORS["text_2"],
    )
    draw.text(
        (right_center - defender_strategy_width / 2, y + 96),
        defender_strategy,
        font=font_small,
        fill=COLORS["text_2"],
    )

    y += match_height + SECTION_GAP
    _rounded_card(draw, (PAGE_PADDING, y, WIDTH - PAGE_PADDING, y + log_height))
    draw.text(
        (PAGE_PADDING + CARD_PADDING, y + 18),
        "战况经过",
        font=font_section,
        fill=COLORS["text"],
    )
    draw.line(
        (
            PAGE_PADDING + CARD_PADDING,
            y + 49,
            WIDTH - PAGE_PADDING - CARD_PADDING,
            y + 49,
        ),
        fill=COLORS["divider"],
    )
    log_y = y + 64
    for wrapped in wrapped_logs:
        draw.ellipse(
            (
                PAGE_PADDING + CARD_PADDING,
                log_y + 7,
                PAGE_PADDING + CARD_PADDING + 6,
                log_y + 13,
            ),
            fill=COLORS["brand"],
        )
        for index, line in enumerate(wrapped):
            _draw_text(
                draw,
                (PAGE_PADDING + CARD_PADDING + 18, log_y),
                line,
                font_body,
                COLORS["text"],
                font_body_emoji,
            )
            log_y += log_line_height
        log_y += 4

    y += log_height + SECTION_GAP
    card_gap = 14
    fighter_card_width = (content_width - card_gap) // 2
    attacker_x = PAGE_PADDING
    defender_x = PAGE_PADDING + fighter_card_width + card_gap
    resource_sets = _resource_values(result)
    for card_x, name, name_color, values in (
        (attacker_x, attacker_name, COLORS["attacker"], resource_sets[0]),
        (defender_x, defender_name, COLORS["defender"], resource_sets[1]),
    ):
        card_right = card_x + fighter_card_width
        _rounded_card(
            draw,
            (card_x, y, card_right, y + resource_height),
            COLORS["card_alt"],
        )
        hp, max_hp, mp, max_mp, sp, max_sp, damage = values
        damage_text = f"输出 {damage}"
        damage_width = _text_width(draw, damage_text, font_small)
        name_display = _fit_text(
            draw,
            name,
            font_section,
            fighter_card_width - CARD_PADDING * 2 - damage_width - 14,
        )
        draw.text(
            (card_x + CARD_PADDING, y + 16),
            name_display,
            font=font_section,
            fill=name_color,
        )
        draw.text(
            (card_right - CARD_PADDING - damage_width, y + 19),
            damage_text,
            font=font_small,
            fill=COLORS["text_3"],
        )
        bar_x = card_x + CARD_PADDING
        bar_width = fighter_card_width - CARD_PADDING * 2
        bar_y = y + 56
        for label, value, maximum, color in (
            ("生命", hp, max_hp, COLORS["hp"]),
            ("魔力", mp, max_mp, COLORS["mp"]),
            ("耐力", sp, max_sp, COLORS["sp"]),
        ):
            _draw_bar(
                draw,
                x=bar_x,
                y=bar_y,
                width=bar_width,
                label=label,
                value=value,
                maximum=maximum,
                color=color,
                font=font_small,
            )
            bar_y += 43

    y += resource_height + SECTION_GAP
    _rounded_card(draw, (PAGE_PADDING, y, WIDTH - PAGE_PADDING, y + result_height))
    draw.text(
        (PAGE_PADDING + CARD_PADDING, y + 18),
        "战斗结果",
        font=font_section,
        fill=COLORS["text"],
    )
    winner_text = f"{winner_name} 获胜"
    winner_width = _text_width(draw, winner_text, font_title)
    draw.text(
        ((WIDTH - winner_width) / 2, y + 48),
        winner_text,
        font=font_title,
        fill=COLORS["winner"],
    )
    winner_exp = f"{winner_name}  +{result.winner_exp_gain} 经验"
    loser_exp = f"{loser_name}  -{result.loser_exp_loss} 经验"
    draw.text(
        (PAGE_PADDING + CARD_PADDING, y + 93),
        winner_exp,
        font=font_small,
        fill=COLORS["positive"],
    )
    loser_exp_width = _text_width(draw, loser_exp, font_small)
    draw.text(
        (WIDTH - PAGE_PADDING - CARD_PADDING - loser_exp_width, y + 93),
        loser_exp,
        font=font_small,
        fill=COLORS["negative"],
    )
    result_y = y + 119
    if getattr(result, "is_counterattack", False):
        draw.text(
            (PAGE_PADDING + CARD_PADDING, result_y),
            "反击战，不消耗主动挑战次数",
            font=font_small,
            fill=COLORS["text_2"],
        )
        result_y += 24
    for text, color in level_items:
        draw.text(
            (PAGE_PADDING + CARD_PADDING, result_y),
            text,
            font=font_small,
            fill=color,
        )
        result_y += 24

    return image
