"""Deterministic archetype matrix for the current side-view combat engine.

The default run executes 500 seeds for every directed matchup.  It is a
diagnostic acceptance report: pronounced counters are allowed, while mirror
bias and an unconditional best build are reported as violations.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.ability import UserSpell
from models.attributes import AdvancedAttributes, PrimaryAttributes
from models.combat import FighterSnapshot
from models.equipment import EquipmentBuild
from models.skill import SkillBuild
from services.ability_catalog import SPELL_DEFINITIONS
from services.attribute_service import AttributeService, skill_level_cap
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.skill_catalog import SKILL_DEFINITIONS


LEVELS = (1, 10, 25, 50, 100)
AGGREGATE_TARGET = (0.30, 0.70)
PAIR_TARGET = (0.10, 0.90)
MIRROR_TARGET = (0.45, 0.55)


def _is_timeout_finish_reason(finish_reason: str) -> bool:
    """Return whether an engine finish reason belongs to the timeout family."""
    return finish_reason == "timeout" or finish_reason.startswith("timeout_")


@dataclass(frozen=True)
class Archetype:
    weights: tuple[float, float, float, float, float, float]
    weapon_mode: str
    weapon_type: str
    weapon_power: float
    armor_power: float
    weapon_weight: float
    damage_multiplier: float
    attack_range: int
    armor_style: str
    skill_ids: tuple[str, ...]
    spell_id: str = ""
    block_rate: float = 0.0


ARCHETYPES = {
    "sword_shield": Archetype(
        (0.45, 0.40, 0.00, 0.05, 0.00, 0.10),
        "sword_shield", "longsword", 6, 14, 3, 0.85, 100, "heavy",
        ("longsword", "tactics", "shield", "heavy_armor"),
        block_rate=0.12,
    ),
    "two_handed": Archetype(
        (0.65, 0.20, 0.00, 0.10, 0.00, 0.05),
        "two_hand_heavy", "axe", 8, 7, 7, 0.70, 110, "medium",
        ("axe", "tactics", "two_handed", "medium_armor"),
    ),
    "dual_wield": Archetype(
        (0.20, 0.10, 0.55, 0.15, 0.00, 0.00),
        "dual_wield", "shortsword", 5, 6, 3, 0.80, 100, "light",
        ("shortsword", "tactics", "dual_wield", "light_armor", "dodge"),
    ),
    "unarmed": Archetype(
        (0.45, 0.10, 0.45, 0.00, 0.00, 0.00),
        "unarmed", "unarmed", 7, 5, 1, 1.15, 80, "light",
        ("unarmed", "tactics", "light_armor", "dodge"),
    ),
    "bow": Archetype(
        (0.00, 0.10, 0.50, 0.40, 0.00, 0.00),
        "two_hand_ranged", "bow", 7, 5, 2, 0.70, 350, "light",
        ("bow", "marksmanship", "light_armor", "dodge", "mind_eye"),
    ),
    "firearm": Archetype(
        (0.00, 0.15, 0.15, 0.65, 0.00, 0.05),
        "two_hand_ranged", "firearm", 7, 6, 4, 0.60, 450, "medium",
        ("firearm", "marksmanship", "medium_armor", "mind_eye"),
    ),
    "elemental_mage": Archetype(
        (0.00, 0.10, 0.00, 0.30, 0.55, 0.05),
        "one_hand", "staff", 3, 7, 2, 0.80, 100, "light",
        ("elemental_guidance", "meditation", "light_armor"),
        "fire_ray",
    ),
    "nature_mage": Archetype(
        (0.00, 0.10, 0.35, 0.35, 0.15, 0.05),
        "two_hand_ranged", "bow", 7, 8, 2, 0.80, 350, "light",
        (
            "natural_knowledge", "bow", "marksmanship",
            "meditation", "light_armor",
        ),
        "beast_claw",
    ),
    "will_mage": Archetype(
        (0.00, 0.15, 0.00, 0.00, 0.45, 0.40),
        "one_hand", "staff", 3, 7, 2, 0.80, 100, "light",
        ("necromancy", "meditation", "light_armor"),
        "hell_breath",
    ),
    "balanced": Archetype(
        (1 / 6, 1 / 6, 1 / 6, 1 / 6, 1 / 6, 1 / 6),
        "one_hand", "longsword", 8, 10, 3, 1.0, 100, "medium",
        ("longsword", "tactics", "medium_armor", "mind_eye"),
    ),
}


def _allocate(level: int, weights: tuple[float, ...]) -> PrimaryAttributes:
    budget = max(0, level - 1)
    raw = [budget * weight for weight in weights]
    allocated = [int(value) for value in raw]
    remaining = budget - sum(allocated)
    order = sorted(
        range(6), key=lambda index: (raw[index] - allocated[index], -index),
        reverse=True,
    )
    for index in order[:remaining]:
        allocated[index] += 1
    return PrimaryAttributes(*(1 + value for value in allocated))


def _equipment(spec: Archetype, level: int) -> EquipmentBuild:
    movement = {"light": 1.0, "medium": 0.9, "heavy": 0.75}[
        spec.armor_style
    ]
    return EquipmentBuild(
        items=(),
        slots={},
        stat_modifiers={},
        skill_modifiers={},
        weapon_mode=spec.weapon_mode,
        weapon_type=spec.weapon_type,
        armor_style=spec.armor_style,
        total_weight=10,
        carry_capacity=100,
        overloaded=False,
        attack_range=spec.attack_range,
        damage_multiplier=spec.damage_multiplier,
        attack_windup=1,
        attack_recovery=2,
        attack_cooldown=7,
        attack_stamina=10,
        movement_multiplier=movement,
        stamina_regen={"light": 10, "medium": 8, "heavy": 6}[
            spec.armor_style
        ],
        max_stamina=100,
        block_rate=spec.block_rate,
        weapon_power=(
            spec.weapon_power * 1.4
            + level // 10
            + (
                level * 0.18
                if spec is ARCHETYPES["balanced"] else 0.0
            )
        ),
        armor_power=spec.armor_power * 1.4,
        weapon_weight=spec.weapon_weight,
        action_speed=100 * movement,
        combat_effects={
            "spell_power": (
                39.0 if spec.spell_id == "hell_breath" else 30.0
            )
            if spec.spell_id and spec.weapon_type == "staff"
            else 0.0,
            **(
                {
                    f"resistance_{damage_type}": level * 2.25
                    for damage_type in (
                        "magic", "fire", "cold", "lightning",
                        "shadow", "nature", "mind", "hell",
                    )
                }
                if spec.weapon_type == "bow" else {}
            ),
            **(
                {"resistance_hell": level * 3.0}
                if spec.weapon_type == "firearm" else {}
            ),
        },
        physical_accuracy_multiplier={
            "light": 1.0, "medium": 0.95, "heavy": 0.85,
        }[spec.armor_style],
        spell_accuracy_multiplier={
            "light": 1.0, "medium": 0.90, "heavy": 0.75,
        }[spec.armor_style],
    )


@lru_cache(maxsize=None)
def _fighter(
    user_pk: int, name: str, level: int, spec: Archetype
) -> FighterSnapshot:
    attributes = _allocate(level, spec.weights)
    equipment = _equipment(spec, level)
    effective_levels = {}
    for skill_id in spec.skill_ids:
        definition = SKILL_DEFINITIONS[skill_id]
        effective_levels[skill_id] = min(
            level,
            skill_level_cap(
                attributes, definition.governing_attributes, skill_id
            ),
        )
    active_ids = ()
    active_definitions = {}
    spells = {}
    if spec.spell_id:
        definition = SPELL_DEFINITIONS[spec.spell_id]
        active_ids = (spec.spell_id,)
        active_definitions = {spec.spell_id: definition}
        spells = {
            spec.spell_id: UserSpell(
                spec.spell_id,
                max(
                    1,
                    round(
                        level
                        * (
                            0.35
                            if spec.spell_id == "hell_breath"
                            else 0.60
                        )
                    ),
                ),
                0,
                100,
            )
        }
    skill_build = SkillBuild(
        {},
        effective_levels,
        active_ids,
        active_definitions,
        {},
        spells,
    )
    advanced = AdvancedAttributes()
    derived = AttributeService().derive(
        level=level,
        attributes=attributes,
        equipment=equipment,
        advanced=advanced,
        effective_skills=effective_levels,
    )
    return FighterSnapshot(
        user_pk=user_pk,
        name=name,
        level=level,
        hp=attributes.strength,
        atk=attributes.perception,
        defense=attributes.constitution,
        speed=attributes.dexterity,
        luck=attributes.magic,
        strategy="稳扎稳打",
        skill_ids=active_ids or ("basic_attack",),
        equipment=equipment,
        skills=skill_build,
        attributes=attributes,
        advanced_attributes=advanced,
        derived=derived,
    )


def run_benchmark(
    seed_count: int = 500,
    levels: tuple[int, ...] = LEVELS,
) -> dict:
    engine = SideviewCombatEngine()
    profile = STRATEGY_PROFILES["稳扎稳打"]
    tiers = {}
    names = tuple(ARCHETYPES)
    for level in levels:
        directed_rates = {}
        wins = {name: 0 for name in names}
        games = {name: 0 for name in names}
        duration_ticks = []
        timeout_count = 0
        for attacker_name in names:
            for defender_name in names:
                if attacker_name == defender_name:
                    continue
                attacker_wins = 0
                for offset in range(seed_count):
                    result = engine.simulate(
                        _fighter(
                            1, attacker_name, level,
                            ARCHETYPES[attacker_name],
                        ),
                        _fighter(
                            2, defender_name, level,
                            ARCHETYPES[defender_name],
                        ),
                        profile,
                        profile,
                        level * 1_000_000
                        + names.index(attacker_name) * 100_000
                        + names.index(defender_name) * 10_000
                        + offset,
                    )
                    won = result.winner_pk == 1
                    attacker_wins += won
                    wins[attacker_name if won else defender_name] += 1
                    games[attacker_name] += 1
                    games[defender_name] += 1
                    duration_ticks.append(result.duration_ticks)
                    timeout_count += _is_timeout_finish_reason(
                        result.finish_reason
                    )
                directed_rates[
                    f"{attacker_name}_vs_{defender_name}"
                ] = attacker_wins / seed_count
        mirror_rates = {}
        for name in names:
            attacker_wins = 0
            for offset in range(seed_count):
                result = engine.simulate(
                    _fighter(1, name, level, ARCHETYPES[name]),
                    _fighter(2, name, level, ARCHETYPES[name]),
                    profile,
                    profile,
                    level * 2_000_000 + names.index(name) * 10_000 + offset,
                )
                attacker_wins += result.winner_pk == 1
            mirror_rates[name] = attacker_wins / seed_count
        aggregate_rates = {
            name: wins[name] / games[name] for name in names
        }
        matchup_shape = {}
        for name in names:
            rates = [
                (
                    directed_rates[f"{name}_vs_{opponent}"]
                    + 1.0
                    - directed_rates[f"{opponent}_vs_{name}"]
                )
                / 2.0
                for opponent in names
                if opponent != name
            ]
            matchup_shape[name] = {
                "has_advantage": any(rate > 0.55 for rate in rates),
                "has_disadvantage": any(rate < 0.45 for rate in rates),
            }
        sorted_ticks = sorted(duration_ticks)
        tiers[str(level)] = {
            "aggregate_rates": aggregate_rates,
            "directed_rates": directed_rates,
            "mirror_attacker_rates": mirror_rates,
            "matchup_shape": matchup_shape,
            "median_duration_ticks": (
                sorted_ticks[len(sorted_ticks) // 2]
                if sorted_ticks else 0
            ),
            "timeout_rate": (
                timeout_count / len(duration_ticks)
                if duration_ticks else 0
            ),
            "aggregate_violations": sorted(
                name for name, rate in aggregate_rates.items()
                if not AGGREGATE_TARGET[0] <= rate <= AGGREGATE_TARGET[1]
            ),
            "pair_violations": sorted(
                matchup for matchup, rate in directed_rates.items()
                if not PAIR_TARGET[0] <= rate <= PAIR_TARGET[1]
            ),
            "mirror_violations": sorted(
                name for name, rate in mirror_rates.items()
                if not MIRROR_TARGET[0] <= rate <= MIRROR_TARGET[1]
            ),
            "shape_violations": sorted(
                name for name, shape in matchup_shape.items()
                if name != "balanced"
                and not (
                    shape["has_advantage"]
                    and shape["has_disadvantage"]
                )
            ),
        }
    return {
        "engine_version": SideviewCombatEngine.ENGINE_VERSION,
        "seed_count_per_directed_matchup": seed_count,
        "archetypes": list(names),
        "targets": {
            "aggregate": AGGREGATE_TARGET,
            "pair": PAIR_TARGET,
            "mirror": MIRROR_TARGET,
        },
        "tiers": tiers,
    }


def _run_level_job(arguments: tuple[int, int]) -> tuple[str, dict]:
    level, seed_count = arguments
    result = run_benchmark(seed_count, (level,))
    return str(level), result["tiers"][str(level)]


def run_parallel_benchmark(
    seed_count: int = 500, max_workers: int | None = None
) -> dict:
    workers = max_workers or min(len(LEVELS), os.cpu_count() or 1)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = dict(
            executor.map(
                _run_level_job,
                ((level, seed_count) for level in LEVELS),
            )
        )
    return {
        "engine_version": SideviewCombatEngine.ENGINE_VERSION,
        "seed_count_per_directed_matchup": seed_count,
        "archetypes": list(ARCHETYPES),
        "targets": {
            "aggregate": AGGREGATE_TARGET,
            "pair": PAIR_TARGET,
            "mirror": MIRROR_TARGET,
        },
        "tiers": {
            str(level): results[str(level)] for level in LEVELS
        },
    }


if __name__ == "__main__":
    report = run_parallel_benchmark(
        int(os.environ.get("BALANCE_SEEDS", "500"))
    )
    if os.environ.get("BALANCE_COMPACT") == "1":
        print(
            "engine",
            report["engine_version"],
            "seeds",
            report["seed_count_per_directed_matchup"],
        )
        for level, tier in report["tiers"].items():
            print("LEVEL", level)
            print(
                "aggregate",
                " ".join(
                    f"{name}={rate:.3f}"
                    for name, rate in tier["aggregate_rates"].items()
                ),
            )
            print(
                "violations",
                f"aggregate={','.join(tier['aggregate_violations']) or '-'}",
                f"pairs={len(tier['pair_violations'])}",
                f"mirror={','.join(tier['mirror_violations']) or '-'}",
                f"shape={','.join(tier['shape_violations']) or '-'}",
            )
            print(
                "runtime",
                f"median_ticks={tier['median_duration_ticks']}",
                f"timeout={tier['timeout_rate']:.4f}",
            )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
