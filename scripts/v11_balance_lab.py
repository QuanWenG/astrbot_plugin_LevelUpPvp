"""Evidence-scoped Monte Carlo laboratory for the v11 combat design.

This is deliberately a *diagnostic*, not an acceptance test.  It builds
characters from the same stat-point budget, progression formulas, equipment
catalog, equipment factory, build resolver, and derived-stat service used by
production.  Constructing a legal full loadout proves component compatibility,
not that a player would acquire that inventory from a realistic number of
drops.  The report states that limitation explicitly, keeps every observed
metric, and adds red flags; it never folds the result into a broad ``pass``
boolean.

Examples::

    python scripts/v11_balance_lab.py --seeds 100 --levels 10,25,50,75,100
    python scripts/v11_balance_lab.py --seeds 250 --workers 5 --output lab.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.ability import UserSpell
from models.attributes import PrimaryAttributes
from models.combat import AIProfile, FighterSnapshot, SimulationResult
from models.skill import SkillBuild, UserSkill
from models.user import User
from services import config
from services.ability_catalog import (
    ACTIVE_ABILITY_DEFINITIONS,
    SPELL_DEFINITIONS,
)
from services.attribute_service import (
    AttributeService,
    skill_level_cap,
    training_efficiency,
)
from services.build_service import CombatBuildService
from services.combat_ai import FAMILY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.equipment_catalog import (
    DEFAULT_EQUIPMENT_CATALOG,
    EquipmentCatalogEntry,
    EquipmentFactory,
)
from services.progression_rules import (
    LEVEL_RULESET_ID,
    SKILL_RULESET_ID,
    decay_skill_potential,
    decay_spell_potential,
    scaled_exp_gain,
    scaled_skill_exp_gain,
    skill_exp_required,
    spell_exp_required,
    spell_level_cap,
    target_days_for_next_level,
)
from services.skill_catalog import INITIAL_SKILLS, SKILL_DEFINITIONS
from services.tactic_rules import FAMILY_LABELS, TacticFamily


DEFAULT_LEVELS = (10, 25, 50, 75, 100)
DEFAULT_BASE_SEED = 0xE10A_110B
DEFAULT_EQUIPMENT_COHORTS = 10
TRAINING_ENCOUNTERS_PER_ACTIVE_DAY = 3.0
EQUIPMENT_LEVEL_BAND = 15
CATALOG_SOURCE = "assets/equipment_catalog.json"


@dataclass(frozen=True)
class ReachableBuildSpec:
    slug: str
    label: str
    identity: str
    native_family: TacticFamily
    stat_weights: tuple[float, float, float, float, float, float]
    weapon_type: str
    armor_preference: str
    skill_training: tuple[tuple[str, float], ...]
    use_shield: bool = False
    use_starter_offhand: bool = False
    spell_ids: tuple[str, ...] = ()
    use_generated_offhand: bool = False


# The six samples are intentionally ordinary catalog builds, not theoretical
# best-in-slot sets.  Every non-initial skill consumes one real level-granted
# skill point, and its training begins only after that point can have existed.
BUILD_SPECS: tuple[ReachableBuildSpec, ...] = (
    ReachableBuildSpec(
        "vanguard",
        "盾剑先锋",
        "格挡与持续压迫",
        TacticFamily.COUNTER,
        (0.34, 0.43, 0.05, 0.10, 0.00, 0.08),
        "longsword",
        "heavy",
        (
            ("longsword", 8), ("tactics", 7), ("shield", 5),
            ("heavy_armor", 4), ("weightlifting", 2), ("mind_eye", 3),
        ),
        use_shield=True,
    ),
    ReachableBuildSpec(
        "breaker",
        "重斧破阵者",
        "高力量近战",
        TacticFamily.PRESSURE,
        (0.55, 0.25, 0.07, 0.08, 0.00, 0.05),
        "axe",
        "heavy",
        (
            ("axe", 9), ("tactics", 7), ("two_handed", 4),
            ("heavy_armor", 4), ("weightlifting", 3), ("mind_eye", 3),
        ),
    ),
    ReachableBuildSpec(
        "duelist",
        "双短剑决斗家",
        "闪避与连击",
        TacticFamily.GAMBIT,
        (0.13, 0.10, 0.50, 0.22, 0.00, 0.05),
        "shortsword",
        "light",
        (
            ("shortsword", 8), ("tactics", 5), ("dual_wield", 7),
            ("light_armor", 4), ("dodge", 5), ("mind_eye", 3),
        ),
        use_generated_offhand=True,
    ),
    ReachableBuildSpec(
        "ranger",
        "长弓游侠",
        "射程与机动",
        TacticFamily.SKIRMISH,
        (0.05, 0.10, 0.40, 0.38, 0.00, 0.07),
        "bow",
        "light",
        (
            ("bow", 8), ("marksmanship", 7), ("light_armor", 4),
            ("dodge", 5), ("mind_eye", 4), ("weightlifting", 2),
        ),
    ),
    ReachableBuildSpec(
        "gunner",
        "火器猎手",
        "精准与中甲",
        TacticFamily.SUSTAIN,
        (0.03, 0.14, 0.20, 0.53, 0.00, 0.10),
        "firearm",
        "medium",
        (
            ("firearm", 9), ("marksmanship", 7), ("medium_armor", 5),
            ("mind_eye", 5), ("weightlifting", 2),
        ),
    ),
    ReachableBuildSpec(
        "arcanist",
        "元素术士",
        "魔法与控制",
        TacticFamily.CONTROL,
        (0.00, 0.10, 0.00, 0.15, 0.50, 0.25),
        "staff",
        "light",
        (
            ("staff", 3), ("magic_training", 8),
            ("elemental_guidance", 8), ("barrier", 5),
            ("meditation", 5),
            ("mana_limit", 3), ("light_armor", 3), ("dodge", 2),
        ),
        spell_ids=(
            "magic_arrow", "fire_ray", "armor_spell", "confusion_spell",
        ),
    ),
)


FAMILY_ORDER: tuple[TacticFamily, ...] = tuple(TacticFamily)


def _stable_seed(*parts: object) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(
        hashlib.blake2b(payload, digest_size=8).digest(), "big"
    )


def _rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _wilson_95_interval(successes: int, sample_count: int) -> dict[str, float]:
    """Return a conservative two-sided 95% Wilson binomial interval."""
    if sample_count <= 0:
        return {"lower": 0.0, "upper": 1.0}

    z = 1.959963984540054
    rate = float(successes) / float(sample_count)
    z_squared = z * z
    denominator = 1.0 + z_squared / sample_count
    center = (rate + z_squared / (2.0 * sample_count)) / denominator
    margin = (
        z
        * math.sqrt(
            rate * (1.0 - rate) / sample_count
            + z_squared / (4.0 * sample_count * sample_count)
        )
        / denominator
    )
    return {
        "lower": round(max(0.0, center - margin), 6),
        "upper": round(min(1.0, center + margin), 6),
    }


def _has_star_type(star_type: object) -> bool:
    if star_type is None:
        return False
    return str(star_type).strip().casefold() not in {"", "normal", "none"}


def _quantile(values: Sequence[int], probability: float) -> int | None:
    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(probability)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    interpolated = (
        ordered[lower] * (upper - position)
        + ordered[upper] * (position - lower)
    )
    return round(interpolated)


def _float_quantile(
    values: Sequence[int | float], probability: float
) -> float | None:
    """Return an interpolated quantile without erasing equipment variance."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * float(probability)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    interpolated = (
        ordered[lower] * (upper - position)
        + ordered[upper] * (position - lower)
    )
    return round(interpolated, 6)


def _float_distribution(
    values: Sequence[int | float],
) -> dict[str, float | None]:
    return {
        "p10": _float_quantile(values, 0.10),
        "p50": _float_quantile(values, 0.50),
        "p90": _float_quantile(values, 0.90),
        "min": round(min((float(value) for value in values), default=0.0), 6)
        if values else None,
        "max": round(max((float(value) for value in values), default=0.0), 6)
        if values else None,
    }


def _ttk(values: Sequence[int]) -> dict[str, int | None]:
    return {
        "p10": _quantile(values, 0.10),
        "p50": _quantile(values, 0.50),
        "p90": _quantile(values, 0.90),
    }


def _active_days_to_level(level: int) -> float:
    return sum(target_days_for_next_level(current) for current in range(1, level))


def _allocate_attributes(
    level: int,
    weights: tuple[float, float, float, float, float, float],
) -> tuple[PrimaryAttributes, dict[str, int]]:
    budget = max(0, (level - 1) * config.STAT_POINTS_PER_LEVEL)
    raw = [budget * weight for weight in weights]
    allocated = [math.floor(value) for value in raw]
    remainder = budget - sum(allocated)
    order = sorted(
        range(6),
        key=lambda index: (raw[index] - allocated[index], -index),
        reverse=True,
    )
    for index in order[:remainder]:
        allocated[index] += 1
    values = [config.INITIAL_STATS[name] + allocated[index]
              for index, name in enumerate(config.INITIAL_STATS)]
    return (
        PrimaryAttributes(*values),
        {
            name: allocated[index]
            for index, name in enumerate(config.INITIAL_STATS)
        },
    )


def _training_encounters(unlock_level: int, target_level: int) -> int:
    if unlock_level >= target_level:
        return 0
    days = sum(
        target_days_for_next_level(level)
        for level in range(max(1, unlock_level), target_level)
    )
    return math.floor(days * TRAINING_ENCOUNTERS_PER_ACTIVE_DAY)


def _grow_skill(
    skill_id: str,
    target_level: int,
    unlock_level: int,
    raw_per_encounter: float,
    attributes: PrimaryAttributes,
) -> tuple[UserSkill, dict[str, int | float | str]]:
    definition = SKILL_DEFINITIONS[skill_id]
    cap = skill_level_cap(
        attributes,
        definition.governing_attributes,
        skill_id,
    )
    encounters = _training_encounters(unlock_level, target_level)
    level = 1
    exp = 0
    potential = 100
    efficiency = training_efficiency(attributes.willpower)
    for _ in range(encounters):
        if level >= cap:
            break
        exp += scaled_skill_exp_gain(
            min(20.0, raw_per_encounter), potential, efficiency
        )
        while level < cap and exp >= skill_exp_required(level):
            exp -= skill_exp_required(level)
            level += 1
            potential = decay_skill_potential(potential)
    skill = UserSkill(skill_id, level, exp, potential)
    return skill, {
        "level": level,
        "exp": exp,
        "potential": potential,
        "level_cap": cap,
        "unlock_character_level": unlock_level,
        "training_encounters": encounters,
        "raw_usage_per_encounter": raw_per_encounter,
        "growth_ruleset": SKILL_RULESET_ID,
    }


def _grow_spell(
    spell_id: str,
    school_level: int,
    target_level: int,
    unlock_level: int,
    willpower: int,
) -> tuple[UserSpell, dict[str, int | float | str]]:
    cap = spell_level_cap(school_level)
    encounters = _training_encounters(unlock_level, target_level)
    level = 1
    exp = 0
    potential = 100
    efficiency = training_efficiency(willpower)
    # Production grants three raw spell XP for one successful cast.  The lab
    # assumes one successful use per rewarded encounter, not constant casting.
    for _ in range(encounters):
        if level >= cap:
            break
        exp += scaled_exp_gain(3, potential, efficiency)
        while level < cap and exp >= spell_exp_required(level):
            exp -= spell_exp_required(level)
            level += 1
            potential = decay_spell_potential(potential)
    spell = UserSpell(spell_id, level, exp, potential)
    return spell, {
        "level": level,
        "exp": exp,
        "potential": potential,
        "level_cap": cap,
        "unlock_character_level": unlock_level,
        "training_encounters": encounters,
        "raw_usage_per_encounter": 3,
        "growth_formula": "services.spell_service.apply_growth_in_db",
    }


def _candidate_entries(
    *,
    item_type: str | None = None,
    equip_slot: str | None = None,
    weapon_type: str | None = None,
    armor_type: str | None = None,
) -> tuple[EquipmentCatalogEntry, ...]:
    result = []
    for entry in DEFAULT_EQUIPMENT_CATALOG.snapshot.entries:
        template = entry.template
        if entry.mode != "generated":
            continue
        if item_type is not None and template.item_type != item_type:
            continue
        if equip_slot is not None and template.equip_slot != equip_slot:
            continue
        if weapon_type is not None and template.weapon_type != weapon_type:
            continue
        if armor_type is not None and template.armor_type != armor_type:
            continue
        result.append(entry)
    return tuple(result)


def _pick_entry(
    candidates: Sequence[EquipmentCatalogEntry], *token: object
) -> EquipmentCatalogEntry:
    if not candidates:
        raise RuntimeError(f"no production catalog candidate for {token!r}")
    return candidates[_stable_seed(*token) % len(candidates)]


def _make_catalog_item(
    factory: EquipmentFactory,
    entry: EquipmentCatalogEntry,
    level: int,
    owner_pk: int,
    item_id: int,
    *token: object,
) -> tuple[object, dict[str, object]]:
    low = max(1, level - EQUIPMENT_LEVEL_BAND)
    for attempt in range(1, 5001):
        factory_seed = _stable_seed("equipment", level, *token, attempt)
        item = factory.create_from_catalog(owner_pk, entry, factory_seed)
        if low <= item.item_level <= level:
            item = replace(item, id=item_id)
            return item, {
                "catalog_id": entry.catalog_id,
                "template_id": entry.template.template_id,
                "catalog_mode": entry.mode,
                "factory_seed": factory_seed,
                "rejection_attempts": attempt - 1,
                "accepted_item_level_band": [low, level],
                "item_level": item.item_level,
                "quality": item.quality,
                "material": item.material,
                "star_type": item.star_type,
                "source": CATALOG_SOURCE,
            }
    raise RuntimeError(
        f"catalog rejection sampling exhausted for {entry.template.template_id}"
    )


def _make_fixed_starter_item(
    factory: EquipmentFactory,
    catalog_id: int,
    owner_pk: int,
    item_id: int,
) -> tuple[object, dict[str, object]]:
    entry = DEFAULT_EQUIPMENT_CATALOG.get(catalog_id)
    item = replace(
        factory.create_from_catalog(owner_pk, entry, 0),
        id=item_id,
    )
    return item, {
        "catalog_id": entry.catalog_id,
        "template_id": entry.template.template_id,
        "catalog_mode": entry.mode,
        "factory_seed": 0,
        "rejection_attempts": 0,
        "accepted_item_level_band": [0, 0],
        "item_level": item.item_level,
        "quality": item.quality,
        "material": item.material,
        "star_type": item.star_type,
        "source": CATALOG_SOURCE,
        "starter_grant": True,
    }


def _equipment_for_build(
    spec: ReachableBuildSpec,
    level: int,
    owner_pk: int,
    equipment_cohort: int = 0,
) -> tuple[list[object], dict[str, int], list[dict[str, object]]]:
    factory = EquipmentFactory()
    items: list[object] = []
    slots: dict[str, int] = {}
    provenance: list[dict[str, object]] = []

    def add_generated(
        entry: EquipmentCatalogEntry, logical_slot: str, serial: int
    ) -> None:
        item_id = owner_pk * 100 + serial
        item, source = _make_catalog_item(
            factory,
            entry,
            level,
            owner_pk,
            item_id,
            "constructed_target_loadout",
            equipment_cohort,
            logical_slot,
        )
        items.append(item)
        slots[logical_slot] = item_id
        provenance.append({"equipped_slot": logical_slot, **source})

    weapon = _pick_entry(
        _candidate_entries(item_type="weapon", weapon_type=spec.weapon_type),
        "constructed_target_loadout",
        equipment_cohort,
        "main_hand",
    )
    add_generated(weapon, "main_hand", 1)

    serial = 2
    if spec.use_shield:
        shield = _pick_entry(
            _candidate_entries(item_type="shield", equip_slot="off_hand"),
            "constructed_target_loadout",
            equipment_cohort,
            "off_hand",
        )
        add_generated(shield, "off_hand", serial)
        serial += 1
    elif spec.use_generated_offhand:
        offhand = _pick_entry(
            _candidate_entries(item_type="weapon", weapon_type=spec.weapon_type),
            "constructed_target_loadout",
            equipment_cohort,
            "off_hand",
        )
        add_generated(offhand, "off_hand", serial)
        serial += 1
    elif spec.use_starter_offhand:
        item_id = owner_pk * 100 + serial
        item, source = _make_fixed_starter_item(
            factory, 1004, owner_pk, item_id
        )
        items.append(item)
        slots["off_hand"] = item_id
        provenance.append({"equipped_slot": "off_hand", **source})
        serial += 1

    for slot in ("head", "back", "body", "wrist", "waist", "feet"):
        candidates = _candidate_entries(
            item_type="armor",
            equip_slot=slot,
            armor_type=spec.armor_preference,
        )
        if not candidates and spec.armor_preference == "medium":
            # The catalog has medium-labelled pieces for only three slots.
            # A production medium loadout therefore mixes in heavier pieces
            # until the weight-based resolver reaches the 15-35 band.
            candidates = _candidate_entries(
                item_type="armor", equip_slot=slot, armor_type="heavy"
            )
        if not candidates:
            continue
        entry = (
            max(candidates, key=lambda item: item.template.weight)
            if spec.armor_preference == "medium"
            else _pick_entry(
                candidates,
                "constructed_target_loadout",
                equipment_cohort,
                slot,
            )
        )
        add_generated(entry, slot, serial)
        serial += 1

    for slot in ("neck", "left_finger"):
        candidates = _candidate_entries(
            item_type="accessory", equip_slot=slot
        )
        if not candidates:
            continue
        add_generated(
            _pick_entry(
                candidates,
                "constructed_target_loadout",
                equipment_cohort,
                slot,
            ),
            slot,
            serial,
        )
        serial += 1
    return items, slots, provenance


class _SkillLevelBoundary:
    """Only the production constant needed by ``CombatBuildService``."""

    MAX_EFFECTIVE_LEVEL = 150


def _profile_for_family(family: TacticFamily) -> AIProfile:
    base = FAMILY_PROFILES[family]
    return replace(
        base,
        strategy_name=family.value,
        tactic_plan=(family.value, family.value, family.value),
    )


def _compatible_abilities(
    spec: ReachableBuildSpec,
    equipment,
    skills: Mapping[str, UserSkill],
    spells: Mapping[str, UserSpell],
) -> tuple[str, ...]:
    selected: list[str] = []
    selected_groups: set[str] = set()

    def append_if_compatible(ability_id: str) -> bool:
        """Add one slot while respecting the catalog's mutually exclusive groups."""
        if ability_id in selected:
            return False
        definition = ACTIVE_ABILITY_DEFINITIONS[ability_id]
        group = definition.exclusive_group
        if group and group in selected_groups:
            return False
        selected.append(ability_id)
        if group:
            selected_groups.add(group)
        return True

    for spell_id in spec.spell_ids:
        definition = ACTIVE_ABILITY_DEFINITIONS.get(spell_id)
        if (
            definition
            and spell_id in spells
            and len(selected) < 4
        ):
            append_if_compatible(spell_id)

    candidates = []
    for ability_id, definition in ACTIVE_ABILITY_DEFINITIONS.items():
        if definition.ability_type == "spell":
            continue
        skill = skills.get(definition.unlock_skill_id)
        if not skill or skill.level < definition.unlock_level:
            continue
        if (
            definition.compatible_weapon_types
            and equipment.weapon_type not in definition.compatible_weapon_types
        ):
            continue
        if (
            definition.compatible_weapon_modes
            and equipment.weapon_mode not in definition.compatible_weapon_modes
        ):
            continue
        has_damage_effect = any(
            effect.effect_type in {"physical_damage", "magic_damage"}
            for effect in definition.effects
        )
        has_damage_tag = "damage" in definition.ai_tags
        candidates.append(
            (
                has_damage_effect or has_damage_tag,
                definition.unlock_level,
                ability_id,
            )
        )
    # Keep a real output action ahead of buffs/summons.  This matters for
    # ranged stances such as split_arrow/thorn_arrow: the former carries the
    # build's only direct hit, while both live in the same exclusive group.
    # Within the same role, higher unlocked tiers remain preferred and the id
    # keeps reports deterministic across Python versions.
    for _, _, ability_id in sorted(candidates, reverse=True):
        append_if_compatible(ability_id)
        if len(selected) == 4:
            break
    return tuple(selected[:4])


def build_reachable_snapshot(
    spec: ReachableBuildSpec,
    level: int,
    owner_pk: int,
    equipment_cohort: int = 0,
) -> tuple[FighterSnapshot, dict[str, object]]:
    attributes, allocations = _allocate_attributes(level, spec.stat_weights)
    training = dict(spec.skill_training)
    requested = list(training)
    desired_learned = [
        skill_id for skill_id in requested if skill_id not in INITIAL_SKILLS
    ]
    skill_point_budget = max(0, (level - 1) * config.SKILL_POINTS_PER_LEVEL)
    # At the requested v11 tiers every identity has its core package.  Keeping
    # this truncation also makes custom low-level CLI probes honest: a level-3
    # character receives only two of the desired non-starter skills.
    learned = desired_learned[:skill_point_budget]

    unlock_levels: dict[str, int] = {
        skill_id: 1 for skill_id in INITIAL_SKILLS
    }
    for index, skill_id in enumerate(learned):
        unlock_levels[skill_id] = index + 2

    all_skill_ids = list(INITIAL_SKILLS)
    all_skill_ids.extend(
        skill_id for skill_id in learned if skill_id not in all_skill_ids
    )
    skills: dict[str, UserSkill] = {}
    skill_sources: dict[str, dict[str, object]] = {}
    for skill_id in all_skill_ids:
        if skill_id not in SKILL_DEFINITIONS:
            continue
        raw = training.get(skill_id, 0.0)
        if raw > 0:
            skill, source = _grow_skill(
                skill_id,
                level,
                unlock_levels.get(skill_id, 1),
                raw,
                attributes,
            )
        else:
            skill = UserSkill(skill_id, 1, 0, 100)
            source = {
                "level": 1,
                "exp": 0,
                "potential": 100,
                "level_cap": skill_level_cap(
                    attributes,
                    SKILL_DEFINITIONS[skill_id].governing_attributes,
                    skill_id,
                ),
                "unlock_character_level": unlock_levels.get(skill_id, 1),
                "training_encounters": 0,
                "raw_usage_per_encounter": 0,
                "growth_ruleset": SKILL_RULESET_ID,
            }
        skills[skill_id] = skill
        skill_sources[skill_id] = source

    user = User(
        id=owner_pk,
        platform="balance_lab",
        group_id="v11",
        user_id=spec.slug,
        nickname=spec.label,
        level=level,
        exp=0,
        total_exp=0,
        stat_points=0,
        level_up_count=level - 1,
        hp=attributes.strength,
        atk=attributes.perception,
        defense=attributes.constitution,
        speed=attributes.dexterity,
        luck=attributes.magic,
        wins=0,
        losses=0,
        created_at="",
        updated_at="",
        skill_points=skill_point_budget - len(learned),
        willpower=attributes.willpower,
        life_growth=100,
        mana_growth=100,
        advanced_speed=100,
        advanced_luck=100,
    )

    items, slots, item_sources = _equipment_for_build(
        spec,
        level,
        owner_pk,
        equipment_cohort,
    )
    attribute_service = AttributeService()
    build_service = CombatBuildService(
        equipment_service=None,
        skill_service=_SkillLevelBoundary(),
        attribute_service=attribute_service,
    )
    equipment = build_service.resolve_equipment(user, slots, items, skills)
    effective = {
        skill_id: min(
            _SkillLevelBoundary.MAX_EFFECTIVE_LEVEL,
            skill.level + equipment.skill_modifiers.get(skill_id, 0),
        )
        for skill_id, skill in skills.items()
    }
    for skill_id, modifier in equipment.skill_modifiers.items():
        effective.setdefault(
            skill_id,
            min(_SkillLevelBoundary.MAX_EFFECTIVE_LEVEL, int(modifier)),
        )

    combat_attributes = attribute_service.attributes_for_user(
        user, equipment.stat_modifiers
    )
    advanced = attribute_service.advanced_attributes_for_user(
        user, equipment.advanced_stat_modifiers
    )
    derived = attribute_service.derive(
        level=level,
        attributes=combat_attributes,
        equipment=equipment,
        advanced=advanced,
        effective_skills=effective,
    )
    equipment = replace(
        equipment,
        max_stamina=derived.max_sp,
        action_speed=derived.action_speed,
    )

    spells: dict[str, UserSpell] = {}
    spell_sources: dict[str, dict[str, object]] = {}
    for index, spell_id in enumerate(spec.spell_ids):
        definition = SPELL_DEFINITIONS[spell_id]
        school_level = effective.get(definition.unlock_skill_id, 0)
        unlock_level = 5 + index * 2
        if level < unlock_level or school_level < definition.unlock_level:
            continue
        spell, source = _grow_spell(
            spell_id,
            school_level,
            level,
            unlock_level,
            combat_attributes.willpower,
        )
        spells[spell_id] = spell
        spell_sources[spell_id] = source

    active_ids = _compatible_abilities(
        spec, equipment, skills, spells
    )
    skill_build = SkillBuild(
        skills=skills,
        effective_levels=effective,
        active_skill_ids=active_ids,
        active_definitions={
            ability_id: ACTIVE_ABILITY_DEFINITIONS[ability_id]
            for ability_id in active_ids
        },
        level_caps={
            skill_id: int(source["level_cap"])
            for skill_id, source in skill_sources.items()
        },
        spells=spells,
    )
    snapshot = FighterSnapshot(
        user_pk=owner_pk,
        name=spec.label,
        level=level,
        hp=attributes.strength,
        atk=attributes.perception,
        defense=attributes.constitution,
        speed=attributes.dexterity,
        luck=attributes.magic,
        strategy=spec.native_family.value,
        equipment_modifiers=equipment.stat_modifiers,
        skill_ids=active_ids or ("basic_attack",),
        equipment=equipment,
        skills=skill_build,
        attributes=combat_attributes,
        advanced_attributes=advanced,
        derived=derived,
    )

    spent = sum(allocations.values())
    item_levels_reachable = all(
        bool(item.get("starter_grant")) or int(item["item_level"]) <= level
        for item in item_sources
    )
    factory_rejection_rolls = sum(
        int(item["rejection_attempts"]) + 1
        for item in item_sources
        if not item.get("starter_grant")
    )
    construction_constraints_satisfied = (
        spent == max(0, level - 1) * config.STAT_POINTS_PER_LEVEL
        and len(learned) <= skill_point_budget
        and item_levels_reachable
    )
    provenance = {
        "label": spec.label,
        "identity": spec.identity,
        "native_tactic_family": spec.native_family.value,
        "level": level,
        "equipment_cohort": equipment_cohort,
        "construction_evidence": {
            "construction_constraints_satisfied": (
                construction_constraints_satisfied
            ),
            "production_acquisition_reachability": "unverified",
            "evidence_scope": (
                "legal stat/skill budgets plus catalog and item-level "
                "compatibility; not a sampled production inventory"
            ),
            "confidence": {
                "stat_budget_legality": "high",
                "skill_training_projection": "medium",
                "equipment_catalog_and_item_level_legality": "high",
                "production_inventory_representativeness": "low",
                "population_balance_inference": "low",
            },
            "character_progression_ruleset": LEVEL_RULESET_ID,
            "active_days_to_level": round(_active_days_to_level(level), 3),
            "training_encounters_per_active_day": (
                TRAINING_ENCOUNTERS_PER_ACTIVE_DAY
            ),
            "stat_points_budget": (
                max(0, level - 1) * config.STAT_POINTS_PER_LEVEL
            ),
            "stat_points_spent": spent,
            "stat_allocations": allocations,
            "skill_points_budget": skill_point_budget,
            "skill_points_spent_to_learn": len(learned),
            "learned_skills": learned,
            "desired_skills_not_yet_learned": [
                skill_id for skill_id in desired_learned
                if skill_id not in learned
            ],
            "equipment_item_levels_reachable": item_levels_reachable,
            "equipment_catalog_schema_version": (
                DEFAULT_EQUIPMENT_CATALOG.snapshot.schema_version
            ),
            "factory_rejection_sampling_rolls": factory_rejection_rolls,
            "acquisition_opportunity_limitation": (
                "the laboratory directly assembles one desired weapon, each "
                "available armor slot, and two accessories, retrying factory "
                "rolls until the item-level band matches; it does not spend "
                "chat/Nefia drop opportunities or sample a player inventory"
            ),
            "resistance_source": "catalog_materials_and_affixes_only",
            "synthetic_level_scaled_resistance": False,
        },
        "permanent_attributes": attributes.to_dict(),
        "combat_attributes": combat_attributes.to_dict(),
        "advanced_attributes": advanced.to_dict(),
        "skills": skill_sources,
        "spells": spell_sources,
        "equipment": item_sources,
        "resolved_build": {
            "weapon_mode": equipment.weapon_mode,
            "weapon_type": equipment.weapon_type,
            "armor_style": equipment.armor_style,
            "total_weight": equipment.total_weight,
            "carry_capacity": equipment.carry_capacity,
            "overloaded": equipment.overloaded,
            "weapon_power": equipment.weapon_power,
            "armor_power": equipment.armor_power,
            "attack_power": derived.attack_power,
            "defense": derived.defense,
            "accuracy": derived.accuracy,
            "evasion": derived.evasion,
            "action_speed": derived.action_speed,
            "catalog_resistances": derived.resistances,
            "active_abilities": list(active_ids),
            "max_hp": derived.max_hp,
            "max_mp": derived.max_mp,
            "max_sp": derived.max_sp,
        },
    }
    return snapshot, provenance


def _is_timeout(result: SimulationResult) -> bool:
    return result.finish_reason == "timeout" or result.finish_reason.startswith(
        "timeout_"
    )


def _one_shot(result: SimulationResult) -> bool:
    first_damage_tick: dict[int, int] = {}
    damage_kinds = {
        "damage", "followup", "counter_damage", "zone_damage",
        "status_damage", "equipment_damage", "equipment_proc_damage",
        "summon_strike", "execute",
    }
    for event in result.events:
        if event.kind not in damage_kinds or event.target_pk is None:
            continue
        first_tick = first_damage_tick.setdefault(event.target_pk, event.tick)
        if event.tick != first_tick:
            continue
        if event.remaining_hp == 0:
            return True
    return False


def _observe(
    result: SimulationResult,
    attacker_key: str,
    defender_key: str,
    equipment_cohort: int | None = None,
) -> dict[str, object]:
    attacker_won = result.winner_pk == result.attacker.user_pk
    winner_key = attacker_key if attacker_won else defender_key
    spell_users = {
        event.actor_pk
        for event in result.events
        if event.kind == "spell_cast" and event.actor_pk is not None
    }
    stamina_depleted = int(
        result.attacker_remaining_stamina
        <= max(1, round(result.attacker.max_sp * 0.05))
    ) + int(
        result.defender_remaining_stamina
        <= max(1, round(result.defender.max_sp * 0.05))
    )
    mana_depleted = int(
        result.attacker.user_pk in spell_users
        and result.attacker_remaining_mana <= 0
    ) + int(
        result.defender.user_pk in spell_users
        and result.defender_remaining_mana <= 0
    )
    fortune_count = sum(
        event.kind == "fortune_swing" for event in result.events
    )
    overcast_users = {
        event.actor_pk
        for event in result.events
        if event.kind == "mana_backlash" and event.actor_pk is not None
    }
    return {
        "attacker": attacker_key,
        "defender": defender_key,
        "winner": winner_key,
        "equipment_cohort": equipment_cohort,
        # Keep the role result separately.  In a mirror both semantic keys are
        # intentionally identical, so comparing their labels would turn every
        # mirror into a fake attacker win.
        "attacker_won": attacker_won,
        "ticks": result.duration_ticks,
        "environment": result.environment_id,
        "timeout": _is_timeout(result),
        "one_shot": _one_shot(result),
        "stamina_depleted_participants": stamina_depleted,
        "mana_depleted_participants": mana_depleted,
        "overcast_participants": len(overcast_users),
        "fortune_triggers": fortune_count,
    }


def _directed_analysis(
    keys: Sequence[str], observations: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    wins = {key: 0 for key in keys}
    games = {key: 0 for key in keys}
    directed_wins: dict[tuple[str, str], int] = defaultdict(int)
    directed_games: dict[tuple[str, str], int] = defaultdict(int)
    for observation in observations:
        attacker = str(observation["attacker"])
        defender = str(observation["defender"])
        winner = str(observation["winner"])
        wins[winner] += 1
        games[attacker] += 1
        games[defender] += 1
        directed_games[(attacker, defender)] += 1
        directed_wins[(attacker, defender)] += int(
            bool(observation["attacker_won"])
        )

    directed = {
        f"{left}->{right}": _rate(
            directed_wins[(left, right)], directed_games[(left, right)]
        )
        for left in keys for right in keys if left != right
    }
    pairwise: dict[str, dict[str, float]] = {
        key: {other: 0.5 for other in keys} for key in keys
    }
    dominance: list[dict[str, object]] = []
    for left_index, left in enumerate(keys):
        for right in keys[left_index + 1:]:
            probability = (
                directed[f"{left}->{right}"]
                + 1.0 - directed[f"{right}->{left}"]
            ) / 2.0
            probability = round(probability, 6)
            pairwise[left][right] = probability
            pairwise[right][left] = round(1.0 - probability, 6)
            if probability >= 0.60:
                dominance.append({
                    "dominant": left,
                    "subordinate": right,
                    "orientation_adjusted_win_rate": probability,
                    "severity": "red" if probability >= 0.70 else "amber",
                })
            elif probability <= 0.40:
                dominance.append({
                    "dominant": right,
                    "subordinate": left,
                    "orientation_adjusted_win_rate": round(
                        1.0 - probability, 6
                    ),
                    "severity": "red" if probability <= 0.30 else "amber",
                })

    usage = _project_meta_usage(keys, pairwise)
    dominated_by: dict[str, list[str]] = {key: [] for key in keys}
    for edge in dominance:
        dominated_by[str(edge["subordinate"])].append(
            str(edge["dominant"])
        )
    entities = {
        key: {
            "win_rate": _rate(wins[key], games[key]),
            "wins": wins[key],
            "games": games[key],
            "projected_meta_usage_rate": usage[key],
            "dominated_by": sorted(dominated_by[key]),
        }
        for key in keys
    }
    return {
        "entities": entities,
        "directed_attacker_win_rates": directed,
        "orientation_adjusted_pairwise": pairwise,
        "dominance_edges": dominance,
        "usage_projection": {
            "method": (
                "replicator dynamics from orientation-adjusted pairwise "
                "win rates; 1% mutation floor, not observed player demand"
            ),
            "rates": usage,
        },
    }


def _project_meta_usage(
    keys: Sequence[str],
    pairwise: Mapping[str, Mapping[str, float]],
) -> dict[str, float]:
    if not keys:
        return {}
    count = len(keys)
    shares = {key: 1.0 / count for key in keys}
    history: list[dict[str, float]] = []
    for step in range(240):
        fitness = {
            key: sum(
                shares[opponent] * pairwise[key][opponent]
                for opponent in keys
            )
            for key in keys
        }
        mean = sum(shares[key] * fitness[key] for key in keys)
        updated = {
            key: shares[key] * math.exp(0.8 * (fitness[key] - mean))
            for key in keys
        }
        total = sum(updated.values())
        shares = {
            key: 0.99 * updated[key] / total + 0.01 / count
            for key in keys
        }
        if step >= 190:
            history.append(dict(shares))
    averaged = {
        key: sum(item[key] for item in history) / len(history)
        for key in keys
    }
    total = sum(averaged.values())
    return {key: round(averaged[key] / total, 6) for key in keys}


def _runtime_metrics(
    observations: Sequence[Mapping[str, object]],
    build_analysis: Mapping[str, object],
    environments: Iterable[str],
    *,
    environment_observations: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    count = len(observations)
    ticks = [int(item["ticks"]) for item in observations]
    timeout_count = sum(bool(item["timeout"]) for item in observations)
    one_shot_count = sum(bool(item["one_shot"]) for item in observations)
    stamina_depleted = sum(
        int(item["stamina_depleted_participants"]) for item in observations
    )
    mana_depleted = sum(
        int(item["mana_depleted_participants"]) for item in observations
    )
    overcast = sum(int(item["overcast_participants"]) for item in observations)
    fortune = sum(int(item["fortune_triggers"]) for item in observations)
    global_build_rates = {
        key: float(value["win_rate"])
        for key, value in build_analysis["entities"].items()
    }
    overall_ttk = _ttk(ticks)
    environment_samples = (
        observations
        if environment_observations is None
        else environment_observations
    )

    environment_report: dict[str, object] = {}
    for environment in environments:
        subset = [
            item for item in environment_samples
            if item["environment"] == environment
        ]
        env_count = len(subset)
        env_wins: dict[str, int] = defaultdict(int)
        env_games: dict[str, int] = defaultdict(int)
        for item in subset:
            env_wins[str(item["winner"])] += 1
            env_games[str(item["attacker"])] += 1
            env_games[str(item["defender"])] += 1
        env_build_rates = {
            key: _rate(env_wins[key], env_games[key])
            for key in global_build_rates if env_games[key]
        }
        largest_build_delta = max(
            (
                abs(env_build_rates[key] - global_build_rates[key])
                for key in env_build_rates
            ),
            default=0.0,
        )
        env_ttk = _ttk([int(item["ticks"]) for item in subset])
        environment_report[environment] = {
            "sample_count": env_count,
            "sample_share": _rate(env_count, len(environment_samples)),
            "attacker_win_rate": _rate(
                sum(bool(item["attacker_won"]) for item in subset),
                env_count,
            ) if env_count else None,
            "ttk": env_ttk,
            "ttk_p50_delta_from_overall": (
                env_ttk["p50"] - overall_ttk["p50"]
                if env_ttk["p50"] is not None
                and overall_ttk["p50"] is not None
                else None
            ),
            "timeout_rate": _rate(
                sum(bool(item["timeout"]) for item in subset), env_count
            ) if env_count else None,
            "one_shot_rate": _rate(
                sum(bool(item["one_shot"]) for item in subset), env_count
            ) if env_count else None,
            "build_win_rates": env_build_rates,
            "largest_absolute_build_win_rate_delta": round(
                largest_build_delta, 6
            ),
        }

    return {
        "sample_count": count,
        "ttk": overall_ttk,
        "timeout": {
            "count": timeout_count,
            "rate": _rate(timeout_count, count),
        },
        "one_shot": {
            "definition": "the target dies during its first recorded damage tick, including follow-ups, procs, zones and summons",
            "count": one_shot_count,
            "rate": _rate(one_shot_count, count),
        },
        "resource_exhaustion": {
            "stamina_depleted_participants": stamina_depleted,
            "stamina_participant_rate": _rate(stamina_depleted, count * 2),
            "mana_depleted_spellcasters": mana_depleted,
            "mana_participant_rate": _rate(mana_depleted, count * 2),
            "overcast_participants": overcast,
            "overcast_participant_rate": _rate(overcast, count * 2),
        },
        "fortune": {
            "trigger_count": fortune,
            "triggers_per_fight": _rate(fortune, count),
            "fights_with_trigger_rate": _rate(
                sum(int(item["fortune_triggers"]) > 0 for item in observations),
                count,
            ),
        },
        "environment_impact": environment_report,
    }


def _mirror_analysis(
    observations: Sequence[Mapping[str, object]],
    build_keys: Sequence[str],
) -> dict[str, object]:
    per_build = {}
    total_wins = 0
    total_games = 0
    for key in build_keys:
        subset = [item for item in observations if item["attacker"] == key]
        wins = sum(bool(item["attacker_won"]) for item in subset)
        total_wins += wins
        total_games += len(subset)
        rate = _rate(wins, len(subset))
        per_build[key] = {
            "sample_count": len(subset),
            "attacker_win_rate": rate,
            "attacker_win_rate_wilson_95_ci": _wilson_95_interval(
                wins, len(subset)
            ),
            "absolute_side_bias": round(abs(rate - 0.5), 6),
        }
    aggregate = _rate(total_wins, total_games)
    return {
        "definition": (
            "identical constructed build and tactic on both sides; only side, "
            "actor id, and keyed entropy role differ"
        ),
        "per_build": per_build,
        "aggregate_attacker_win_rate": aggregate,
        "aggregate_attacker_win_rate_wilson_95_ci": _wilson_95_interval(
            total_wins, total_games
        ),
        "aggregate_absolute_side_bias": round(abs(aggregate - 0.5), 6),
    }


def _equipment_cohort_analysis(
    provenance: Mapping[str, Sequence[Mapping[str, object]]],
    observations: Sequence[Mapping[str, object]],
    build_analysis: Mapping[str, object],
    cohort_count: int,
    seed_count: int,
) -> dict[str, object]:
    """Describe variance among directly constructed catalog loadouts.

    These are not inventory cohorts.  The laboratory selects a desired item
    category for every logical slot before rolling item properties, so this
    evidence can isolate template/stat variance but cannot estimate production
    acquisition frequency.
    """
    seeds_per_cohort = [
        sum(offset % cohort_count == cohort for offset in range(seed_count))
        for cohort in range(cohort_count)
    ]
    build_reports: dict[str, object] = {}
    numeric_fields = (
        "weapon_power",
        "armor_power",
        "total_weight",
        "carry_capacity",
        "attack_power",
        "defense",
        "accuracy",
        "evasion",
        "action_speed",
        "max_hp",
        "max_mp",
        "max_sp",
    )
    for build_key, cohorts in provenance.items():
        cohort_rows: list[dict[str, object]] = []
        for cohort_index, source in enumerate(cohorts):
            subset = [
                item for item in observations
                if int(item["equipment_cohort"]) == cohort_index
                and build_key in (item["attacker"], item["defender"])
            ]
            wins = sum(item["winner"] == build_key for item in subset)
            resolved = source["resolved_build"]
            equipment = source["equipment"]
            item_levels = [
                int(item["item_level"])
                for item in equipment
                if not item.get("starter_grant")
            ]
            quality_counts: dict[str, int] = defaultdict(int)
            material_counts: dict[str, int] = defaultdict(int)
            for item in equipment:
                quality_counts[str(item["quality"])] += 1
                material_counts[str(item["material"])] += 1
            cohort_rows.append({
                "cohort": cohort_index,
                "games": len(subset),
                "win_rate": _rate(wins, len(subset)),
                "resolved_build": {
                    field: resolved[field] for field in numeric_fields
                } | {
                    "overloaded": bool(resolved["overloaded"]),
                    "active_ability_count": len(
                        resolved["active_abilities"]
                    ),
                },
                "constructed_catalog_roll": {
                    "generated_item_level_mean": round(
                        sum(item_levels) / len(item_levels), 6
                    ) if item_levels else None,
                    "quality_counts": dict(sorted(quality_counts.items())),
                    "material_counts": dict(sorted(material_counts.items())),
                    "star_item_count": sum(
                        _has_star_type(item.get("star_type"))
                        for item in equipment
                    ),
                    "factory_rejection_sampling_rolls": source[
                        "construction_evidence"
                    ][
                        "factory_rejection_sampling_rolls"
                    ],
                },
            })

        stat_distributions = {
            field: _float_distribution([
                row["resolved_build"][field] for row in cohort_rows
            ])
            for field in numeric_fields
        }
        stat_distributions["generated_item_level_mean"] = (
            _float_distribution([
                row["constructed_catalog_roll"]["generated_item_level_mean"]
                for row in cohort_rows
                if row["constructed_catalog_roll"]["generated_item_level_mean"]
                is not None
            ])
        )
        build_reports[build_key] = {
            "cohort_win_rate_distribution": _float_distribution([
                row["win_rate"] for row in cohort_rows
            ]),
            "overload_cohort_rate": _rate(
                sum(row["resolved_build"]["overloaded"] for row in cohort_rows),
                len(cohort_rows),
            ),
            "stat_distributions": stat_distributions,
            "cohorts": cohort_rows,
        }

    return {
        "cohort_count": cohort_count,
        "combat_seeds_per_directed_matchup": seed_count,
        "combat_seeds_per_cohort": {
            "min": min(seeds_per_cohort),
            "max": max(seeds_per_cohort),
            "counts": seeds_per_cohort,
        },
        "cohort_semantics": "constructed_target_loadout_not_player_inventory",
        "production_inventory_representativeness": "low",
        "acquisition_opportunity_limitation": (
            "desired weapon/armor categories and logical slots are selected "
            "before item properties are rolled; no chat/Nefia drop budget, "
            "duplicate inventory, auto-equip decision, or workshop history is "
            "sampled"
        ),
        "matching_rule": (
            "both sides use the same cohort ordinal; catalog choice and item "
            "factory entropy are keyed by level + cohort + logical slot, so "
            "the comparison receives paired construction entropy while "
            "retaining different weapon/armor requirements"
        ),
        "paired_construction_system_curve": {
            "build_win_rates": {
                key: value["win_rate"]
                for key, value in build_analysis["entities"].items()
            },
            "orientation_adjusted_pairwise": build_analysis[
                "orientation_adjusted_pairwise"
            ],
        },
        "constructed_catalog_roll_variance": build_reports,
    }


def _diagnostics(
    mirror: Mapping[str, object],
    builds: Mapping[str, object],
    strategies: Mapping[str, object],
    runtime: Mapping[str, object],
    target_ttk: tuple[int, int],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []

    def add(
        severity: str,
        code: str,
        observed: object,
        reference: object,
        explanation: str,
    ) -> None:
        findings.append({
            "severity": severity,
            "code": code,
            "observed": observed,
            "design_reference": reference,
            "explanation": explanation,
        })

    add(
        "red",
        "acquisition_opportunity_unmodeled",
        {
            "cohort_semantics": "constructed_target_loadout",
            "production_inventory_representativeness": "low",
        },
        "sample player inventories from bounded production drop opportunities",
        (
            "当前装备 cohort 直接拼装目标槽位，只能回答构筑规则问题；"
            "不能据此声称聊天、奈菲亚和工坊玩家群体会拥有这些装备。"
        ),
    )

    mirror_tolerance_low = 0.45
    mirror_tolerance_high = 0.55
    for key, metric in mirror["per_build"].items():
        confidence_interval = metric.get(
            "attacker_win_rate_wilson_95_ci"
        )
        if not isinstance(confidence_interval, Mapping):
            continue
        lower = confidence_interval.get("lower")
        upper = confidence_interval.get("upper")
        if (
            isinstance(lower, (int, float))
            and isinstance(upper, (int, float))
            and (
                float(upper) < mirror_tolerance_low
                or float(lower) > mirror_tolerance_high
            )
        ):
            add(
                "red", "mirror_side_bias", {key: metric},
                "95% Wilson CI intersects 0.45 .. 0.55",
                "镜像胜率的置信区间整体落在设计容忍带之外。",
            )

    for key, metric in builds["entities"].items():
        if metric["win_rate"] < 0.35 or metric["win_rate"] > 0.65:
            add(
                "red", "build_global_win_rate", {key: metric["win_rate"]},
                "0.35 .. 0.65 diagnostic band",
                "真实可达构筑在均匀对局池中明显过弱或过强。",
            )
        if metric["projected_meta_usage_rate"] > 0.35:
            add(
                "red", "projected_meta_concentration",
                {key: metric["projected_meta_usage_rate"]},
                "no single projected share above 0.35",
                "胜率矩阵会把模拟玩家群体推向单一构筑。",
            )
        elif metric["projected_meta_usage_rate"] < 0.05:
            add(
                "amber", "projected_build_abandonment",
                {key: metric["projected_meta_usage_rate"]},
                "projected share >= 0.05",
                "该构筑在基于胜率的元博弈中几乎没有选择理由。",
            )

    for key, metric in strategies["entities"].items():
        if metric["win_rate"] < 0.38 or metric["win_rate"] > 0.62:
            add(
                "red", "tactic_family_global_win_rate",
                {key: metric["win_rate"]},
                "0.38 .. 0.62 diagnostic band",
                "在相同构筑上，战术族本身造成了过大的全局优势。",
            )

    median = runtime["ttk"]["p50"]
    ttk_low, ttk_high = target_ttk
    if median is not None and not ttk_low <= median <= ttk_high:
        add(
            "red",
            "ttk_median",
            median,
            f"{ttk_low} .. {ttk_high} ticks",
            "战斗节奏偏离 v11 的可读叙事窗口。",
        )
    if runtime["timeout"]["rate"] > 0.03:
        add(
            "red", "timeout_rate", runtime["timeout"]["rate"], "<= 0.03",
            "过多胜负由超时评分而非战斗过程决定。",
        )
    if runtime["one_shot"]["rate"] > 0.01:
        add(
            "red", "one_shot_rate", runtime["one_shot"]["rate"], "<= 0.01",
            "角色没有获得反应或战术切换机会。",
        )
    if runtime["fortune"]["trigger_count"] == 0 and runtime["sample_count"] >= 50:
        add(
            "red", "fortune_inert", 0, "visible triggers in >= 50 fights",
            "幸运保险机制在真实构筑样本中没有实际出现。",
        )

    for environment, metric in runtime["environment_impact"].items():
        if metric["sample_count"] < 10:
            continue
        ttk_delta = metric["ttk_p50_delta_from_overall"]
        if ttk_delta is not None and abs(ttk_delta) > 20:
            add(
                "red", "environment_ttk_distortion",
                {environment: ttk_delta}, "absolute median delta <= 20 ticks",
                "环境对战斗时长的影响已盖过构筑和战术选择。",
            )
        if metric["largest_absolute_build_win_rate_delta"] > 0.15:
            add(
                "red", "environment_build_distortion",
                {environment: metric["largest_absolute_build_win_rate_delta"]},
                "largest build delta <= 0.15",
                "该环境显著改写了构筑胜率，需要检查是否形成抽签胜负。",
            )
    return findings


def _run_level(
    level: int,
    seed_count: int,
    base_seed: int,
    equipment_cohort_count: int,
) -> dict[str, object]:
    engine = SideviewCombatEngine("sideview-v11")
    supported_environment_ids = tuple(
        item[0] for item in engine.SUPPORTED_ENVIRONMENTS
    )
    cohort_count = min(equipment_cohort_count, seed_count)
    builds: dict[str, list[FighterSnapshot]] = {}
    provenance: dict[str, list[dict[str, object]]] = {}
    for index, spec in enumerate(BUILD_SPECS, start=1):
        builds[spec.slug] = []
        provenance[spec.slug] = []
        for equipment_cohort in range(cohort_count):
            snapshot, source = build_reachable_snapshot(
                spec,
                level,
                1000 + index,
                equipment_cohort,
            )
            builds[spec.slug].append(snapshot)
            provenance[spec.slug].append(source)

    neutral_profile = _profile_for_family(TacticFamily.SUSTAIN)
    native_profiles = {
        spec.slug: _profile_for_family(spec.native_family)
        for spec in BUILD_SPECS
    }
    build_observations: list[dict[str, object]] = []
    neutral_build_observations: list[dict[str, object]] = []
    environment_observations: list[dict[str, object]] = []
    build_keys = tuple(builds)
    for left_index, left in enumerate(build_keys):
        for right_index, right in enumerate(build_keys):
            if left == right:
                continue
            for offset in range(seed_count):
                equipment_cohort = offset % cohort_count
                seed = _stable_seed(
                    base_seed,
                    level,
                    "build",
                    left,
                    right,
                    equipment_cohort,
                    offset,
                )
                result = engine.simulate(
                    replace(
                        builds[left][equipment_cohort],
                        user_pk=1,
                        name=left,
                    ),
                    replace(
                        builds[right][equipment_cohort],
                        user_pk=2,
                        name=right,
                    ),
                    native_profiles[left],
                    native_profiles[right],
                    seed,
                )
                build_observations.append(
                    _observe(result, left, right, equipment_cohort)
                )
                neutral_seed = _stable_seed(
                    base_seed,
                    level,
                    "build-neutral",
                    left,
                    right,
                    equipment_cohort,
                    offset,
                )
                neutral_result = engine.simulate(
                    replace(
                        builds[left][equipment_cohort],
                        user_pk=1,
                        name=left,
                        strategy=TacticFamily.SUSTAIN.value,
                    ),
                    replace(
                        builds[right][equipment_cohort],
                        user_pk=2,
                        name=right,
                        strategy=TacticFamily.SUSTAIN.value,
                    ),
                    neutral_profile,
                    neutral_profile,
                    neutral_seed,
                )
                neutral_build_observations.append(
                    _observe(
                        neutral_result,
                        left,
                        right,
                        equipment_cohort,
                    )
                )
                # Keep environment diagnostics independent of the rated
                # matrix, but distribute one forced environment per sample.
                # The round-robin guarantees full coverage even in the
                # one-seed contract test and approaches equal counts exactly.
                environment_id = supported_environment_ids[
                    len(environment_observations)
                    % len(supported_environment_ids)
                ]
                environment_seed = _stable_seed(
                    base_seed,
                    level,
                    "environment",
                    left,
                    right,
                    equipment_cohort,
                    offset,
                    environment_id,
                )
                environment_result = engine.simulate(
                    replace(
                        builds[left][equipment_cohort],
                        user_pk=1,
                        name=left,
                    ),
                    replace(
                        builds[right][equipment_cohort],
                        user_pk=2,
                        name=right,
                    ),
                    native_profiles[left],
                    native_profiles[right],
                    environment_seed,
                    environment_id=environment_id,
                )
                environment_observations.append(
                    _observe(
                        environment_result,
                        left,
                        right,
                        equipment_cohort,
                    )
                )

    mirror_observations: list[dict[str, object]] = []
    for key in build_keys:
        for offset in range(seed_count):
            equipment_cohort = offset % cohort_count
            seed = _stable_seed(
                base_seed,
                level,
                "mirror",
                key,
                equipment_cohort,
                offset,
            )
            result = engine.simulate(
                replace(
                    builds[key][equipment_cohort],
                    user_pk=1,
                    name=f"{key}:left",
                ),
                replace(
                    builds[key][equipment_cohort],
                    user_pk=2,
                    name=f"{key}:right",
                ),
                native_profiles[key],
                native_profiles[key],
                seed,
            )
            mirror_observations.append(
                _observe(result, key, key, equipment_cohort)
            )

    family_keys = tuple(family.value for family in FAMILY_ORDER)
    strategy_observations: list[dict[str, object]] = []
    for left_index, left_family in enumerate(FAMILY_ORDER):
        for right_index, right_family in enumerate(FAMILY_ORDER):
            if left_family == right_family:
                continue
            for offset in range(seed_count):
                # Identical builds on both sides isolate tactic effects.  The
                # reference build rotates so one gear identity cannot define
                # the entire strategy result.
                build_key = build_keys[
                    (offset + left_index + right_index) % len(build_keys)
                ]
                equipment_cohort = offset % cohort_count
                seed = _stable_seed(
                    base_seed,
                    level,
                    "strategy",
                    left_family.value,
                    right_family.value,
                    build_key,
                    equipment_cohort,
                    offset,
                )
                source = builds[build_key][equipment_cohort]
                result = engine.simulate(
                    replace(
                        source,
                        user_pk=1,
                        name=left_family.value,
                        strategy=left_family.value,
                    ),
                    replace(
                        source,
                        user_pk=2,
                        name=right_family.value,
                        strategy=right_family.value,
                    ),
                    _profile_for_family(left_family),
                    _profile_for_family(right_family),
                    seed,
                )
                strategy_observations.append(
                    _observe(
                        result,
                        left_family.value,
                        right_family.value,
                        equipment_cohort,
                    )
                )

    build_analysis = _directed_analysis(build_keys, build_observations)
    build_analysis["evaluation_mode"] = "native_tactic_per_build"
    build_analysis["tactic_assignment"] = {
        spec.slug: spec.native_family.value for spec in BUILD_SPECS
    }
    neutral_build_analysis = _directed_analysis(
        build_keys, neutral_build_observations
    )
    neutral_build_analysis["evaluation_mode"] = "shared_sustain_control"
    neutral_build_analysis["tactic_assignment"] = {
        key: TacticFamily.SUSTAIN.value for key in build_keys
    }
    tactic_sensitivity = {
        key: round(
            float(build_analysis["entities"][key]["win_rate"])
            - float(neutral_build_analysis["entities"][key]["win_rate"]),
            6,
        )
        for key in build_keys
    }
    strategy_analysis = _directed_analysis(
        family_keys, strategy_observations
    )
    strategy_analysis["labels"] = {
        family.value: FAMILY_LABELS[family] for family in FAMILY_ORDER
    }
    mirror = _mirror_analysis(mirror_observations, build_keys)
    runtime = _runtime_metrics(
        build_observations,
        build_analysis,
        supported_environment_ids,
        environment_observations=environment_observations,
    )
    diagnostics = _diagnostics(
        mirror,
        build_analysis,
        strategy_analysis,
        runtime,
        (
            engine.ruleset.tempo.target_median_ticks_low,
            engine.ruleset.tempo.target_median_ticks_high,
        ),
    )
    cohort_analysis = _equipment_cohort_analysis(
        provenance,
        build_observations,
        build_analysis,
        cohort_count,
        seed_count,
    )
    return {
        "level": level,
        # Preserve the original concise entry point for report consumers and
        # expose every sampled acquisition cohort separately below.
        "reachable_builds": {
            key: cohorts[0] for key, cohorts in provenance.items()
        },
        "equipment_cohort_provenance": provenance,
        "equipment_cohort_analysis": cohort_analysis,
        "mirror_side_bias": mirror,
        "build_balance": build_analysis,
        "neutral_build_balance": neutral_build_analysis,
        "native_minus_neutral_win_rate": tactic_sensitivity,
        "strategy_family_balance": strategy_analysis,
        "combat_runtime": runtime,
        "diagnostics": diagnostics,
        "red_flag_count": sum(
            item["severity"] == "red" for item in diagnostics
        ),
        "amber_flag_count": sum(
            item["severity"] == "amber" for item in diagnostics
        ),
    }


def _run_level_job(
    arguments: tuple[int, int, int, int]
) -> tuple[int, dict[str, object]]:
    level, seed_count, base_seed, equipment_cohort_count = arguments
    return level, _run_level(
        level, seed_count, base_seed, equipment_cohort_count
    )


def run_lab(
    *,
    seed_count: int = 100,
    levels: Sequence[int] = DEFAULT_LEVELS,
    workers: int = 1,
    base_seed: int = DEFAULT_BASE_SEED,
    equipment_cohort_count: int = DEFAULT_EQUIPMENT_COHORTS,
) -> dict[str, object]:
    """Run the lab and return a deterministic, JSON-serializable report."""
    if seed_count < 1:
        raise ValueError("seed_count must be positive")
    normalized_levels = tuple(dict.fromkeys(int(level) for level in levels))
    if not normalized_levels or any(level < 1 or level > 100 for level in normalized_levels):
        raise ValueError("levels must contain values from 1 to 100")
    if workers < 1:
        raise ValueError("workers must be positive")
    if equipment_cohort_count < 1:
        raise ValueError("equipment_cohort_count must be positive")

    jobs = [
        (
            level,
            int(seed_count),
            int(base_seed),
            int(equipment_cohort_count),
        )
        for level in normalized_levels
    ]
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(jobs))
        ) as executor:
            completed = dict(executor.map(_run_level_job, jobs))
    else:
        completed = dict(_run_level_job(job) for job in jobs)
    tiers = {
        str(level): completed[level] for level in normalized_levels
    }
    all_diagnostics = [
        {"level": int(level), **finding}
        for level, tier in tiers.items()
        for finding in tier["diagnostics"]
    ]
    return {
        "schema_version": 3,
        "report_kind": "v11_reachability_design_diagnostic",
        "engine_version": SideviewCombatEngine.ENGINE_VERSION,
        "inputs": {
            "base_seed": int(base_seed),
            "seeds_per_directed_matchup": int(seed_count),
            "equipment_cohorts_requested": int(equipment_cohort_count),
            "equipment_cohorts_sampled": min(
                int(equipment_cohort_count), int(seed_count)
            ),
            "levels": list(normalized_levels),
        },
        "methodology": {
            "purpose": (
                "surface curves, dominance, tempo, randomness, and reachable "
                "build failures; this report is not a release gate"
            ),
            "build_source": (
                "production stat budgets + production progression formulas + "
                "production equipment catalog/factory/build/attribute services"
            ),
            "skill_model": (
                "three rewarded encounters per modeled active day; actual raw "
                "usage, fixed-point XP, potential decay, and skill caps"
            ),
            "equipment_model": (
                "deterministic rejection sampling from generated catalog items; "
                "accepted item level must be within 15 levels and never above "
                "the character level; both combatants share the same acquisition "
                "cohort ordinal and slot-keyed factory entropy"
            ),
            "strategy_isolation": (
                "same reachable build on both sides, rotating build identity"
            ),
            "build_tactic_sampling": (
                "the primary build matrix assigns each build its declared "
                "native tactic family; neutral_build_balance repeats every "
                "match with sustain on both sides to isolate raw build rules, "
                "and native_minus_neutral_win_rate exposes tactic sensitivity"
            ),
            "environment_sampling": (
                "the main build matrix uses the rated PvP default pool; a "
                "separate deterministic round-robin forces every supported "
                "environment so Nefia/operation modifiers remain measurable"
            ),
            "flags": (
                "red/amber findings annotate observations; absence of a flag "
                "does not mean the design passed"
            ),
        },
        "levels": tiers,
        "summary": {
            "red_flag_count": sum(
                item["severity"] == "red" for item in all_diagnostics
            ),
            "amber_flag_count": sum(
                item["severity"] == "amber" for item in all_diagnostics
            ),
            "findings": all_diagnostics,
        },
    }


def _parse_levels(raw: str) -> tuple[int, ...]:
    try:
        levels = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("levels must be comma-separated integers") from error
    if not levels or any(level < 1 or level > 100 for level in levels):
        raise argparse.ArgumentTypeError("levels must be between 1 and 100")
    return levels


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the v11 reachability-aware combat design laboratory."
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=100,
        help="Monte Carlo seeds per directed matchup (default: 100)",
    )
    parser.add_argument(
        "--base-seed",
        type=int,
        default=DEFAULT_BASE_SEED,
        help="root seed used to derive every keyed experiment seed",
    )
    parser.add_argument(
        "--equipment-cohorts",
        type=int,
        default=DEFAULT_EQUIPMENT_COHORTS,
        help=(
            "matched attainable equipment sets distributed across combat "
            "seeds (default: 10)"
        ),
    )
    parser.add_argument(
        "--levels",
        type=_parse_levels,
        default=DEFAULT_LEVELS,
        help="comma-separated character levels (default: 10,25,50,75,100)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(len(DEFAULT_LEVELS), os.cpu_count() or 1)),
        help="parallel level workers",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write UTF-8 JSON to this path; stdout is used when omitted",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    report = run_lab(
        seed_count=args.seeds,
        levels=args.levels,
        workers=args.workers,
        base_seed=args.base_seed,
        equipment_cohort_count=args.equipment_cohorts,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
