"""Pure level-scaling rules shared by equipment generation and resolution."""

from __future__ import annotations

from collections.abc import Iterable


def skill_level_affix_cap(level: int) -> int:
    """Return the per-affix skill bonus cap for a character or item level."""
    level = int(level)
    if level <= 0:
        return 1
    if level <= 20:
        return 2
    if level <= 40:
        return 4
    if level <= 60:
        return 7
    if level <= 80:
        return 9
    return 11


def inherent_affix_level_ratio(character_level: int, item_level: int) -> float:
    """Return the numeric inherent-affix multiplier, clamped to [0, 1]."""
    item_level = int(item_level)
    if item_level <= 0:
        return 1.0
    return min(1.0, max(0.0, int(character_level) / item_level))


def effective_inherent_affix_value(
    affix: dict,
    character_level: int,
    item_level: int,
) -> int | float:
    """Resolve one inherent affix without exceeding its stored magnitude."""
    raw_value = affix.get("value", 0)
    if int(item_level) <= 0 or int(character_level) >= int(item_level):
        return raw_value

    if str(affix.get("type", "")) == "skill_level":
        cap = skill_level_affix_cap(character_level)
        numeric_value = int(raw_value)
        if numeric_value >= 0:
            return min(numeric_value, cap)
        return max(numeric_value, -cap)

    return float(raw_value) * inherent_affix_level_ratio(
        character_level,
        item_level,
    )


def effective_inherent_affixes(
    affixes: Iterable[dict],
    character_level: int,
    item_level: int,
) -> tuple[dict, ...]:
    """Return build/display-ready copies with level-adjusted values."""
    return tuple(
        {
            **affix,
            "value": effective_inherent_affix_value(
                affix,
                character_level,
                item_level,
            ),
        }
        for affix in affixes
    )
