"""Run a deterministic primary-attribute balance benchmark.

This is a diagnostic benchmark, not a combat-formula gate. It deliberately
uses one neutral longsword loadout so changes to primary-attribute mappings are
easy to compare across revisions.
"""

from __future__ import annotations

import json
import os
import sys


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.attributes import AdvancedAttributes, PrimaryAttributes
from models.combat import FighterSnapshot
from models.equipment import EquipmentBuild
from models.skill import SkillBuild
from services.attribute_service import AttributeService
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine


ATTRIBUTE_IDS = (
    "strength",
    "constitution",
    "dexterity",
    "perception",
    "magic",
    "willpower",
)
LEVELS = (5, 20, 50)
AGGREGATE_TARGET = (0.35, 0.65)
PAIR_TARGET = (0.20, 0.80)
MIRROR_TARGET = (0.45, 0.55)


def _equipment() -> EquipmentBuild:
    return EquipmentBuild(
        items=(),
        slots={},
        stat_modifiers={},
        skill_modifiers={},
        weapon_mode="one_hand",
        weapon_type="longsword",
        armor_style="light",
        total_weight=10.0,
        carry_capacity=100.0,
        overloaded=False,
        attack_range=100,
        damage_multiplier=1.0,
        attack_windup=1,
        attack_recovery=2,
        attack_cooldown=6,
        attack_stamina=8,
        movement_multiplier=1.0,
        stamina_regen=10,
        max_stamina=100,
        weapon_power=10,
        armor_power=10,
        weapon_weight=3,
        action_speed=100,
    )


def _fighter(
    user_pk: int,
    build_name: str,
    level: int,
    attributes: PrimaryAttributes,
) -> FighterSnapshot:
    equipment = _equipment()
    advanced = AdvancedAttributes()
    derived = AttributeService().derive(
        level=level,
        attributes=attributes,
        equipment=equipment,
        advanced=advanced,
        effective_skills={},
    )
    return FighterSnapshot(
        user_pk=user_pk,
        name=build_name,
        level=level,
        hp=attributes.strength,
        atk=attributes.perception,
        defense=attributes.constitution,
        speed=attributes.dexterity,
        luck=attributes.magic,
        strategy="稳扎稳打",
        equipment=equipment,
        skills=SkillBuild({}, {}),
        attributes=attributes,
        advanced_attributes=advanced,
        derived=derived,
    )


def _builds(level: int) -> dict[str, PrimaryAttributes]:
    budget = level - 1
    builds: dict[str, PrimaryAttributes] = {}
    for index, name in enumerate(ATTRIBUTE_IDS):
        values = [1] * len(ATTRIBUTE_IDS)
        values[index] += budget
        builds[name] = PrimaryAttributes(*values)
    quotient, remainder = divmod(budget, len(ATTRIBUTE_IDS))
    builds["balanced"] = PrimaryAttributes(
        *(
            1 + quotient + (1 if index < remainder else 0)
            for index in range(len(ATTRIBUTE_IDS))
        )
    )
    return builds


def run_benchmark(
    matrix_seed_count: int = 8,
    mirror_seed_count: int = 200,
) -> dict:
    engine = SideviewCombatEngine()
    profile = STRATEGY_PROFILES["稳扎稳打"]
    tiers = {}
    for level in LEVELS:
        builds = _builds(level)
        names = tuple(builds)
        wins = {name: 0 for name in names}
        games = {name: 0 for name in names}
        pair_rates = {}
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                first_wins = 0
                pair_games = 0
                for seed_offset in range(matrix_seed_count):
                    seed = level * 100_000 + seed_offset
                    for attacker, defender in ((first, second), (second, first)):
                        result = engine.simulate(
                            _fighter(1, attacker, level, builds[attacker]),
                            _fighter(2, defender, level, builds[defender]),
                            profile,
                            profile,
                            seed,
                        )
                        winner = attacker if result.winner_pk == 1 else defender
                        wins[winner] += 1
                        games[first] += 1
                        games[second] += 1
                        first_wins += winner == first
                        pair_games += 1
                pair_rates[f"{first}_vs_{second}"] = first_wins / pair_games

        aggregate_rates = {
            name: wins[name] / games[name]
            for name in names
        }
        mirror_rates = {}
        for name in names:
            attacker_wins = 0
            for seed_offset in range(mirror_seed_count):
                result = engine.simulate(
                    _fighter(1, name, level, builds[name]),
                    _fighter(2, name, level, builds[name]),
                    profile,
                    profile,
                    level * 200_000 + seed_offset,
                )
                attacker_wins += result.winner_pk == 1
            mirror_rates[name] = attacker_wins / mirror_seed_count

        tiers[str(level)] = {
            "aggregate_rates": aggregate_rates,
            "pair_rates": pair_rates,
            "mirror_attacker_rates": mirror_rates,
            "aggregate_violations": sorted(
                name
                for name, rate in aggregate_rates.items()
                if not AGGREGATE_TARGET[0] <= rate <= AGGREGATE_TARGET[1]
            ),
            "pair_violations": sorted(
                name
                for name, rate in pair_rates.items()
                if not PAIR_TARGET[0] <= rate <= PAIR_TARGET[1]
            ),
            "mirror_violations": sorted(
                name
                for name, rate in mirror_rates.items()
                if not MIRROR_TARGET[0] <= rate <= MIRROR_TARGET[1]
            ),
        }
    return {
        "engine_version": SideviewCombatEngine.ENGINE_VERSION,
        "loadout": "neutral_longsword",
        "matrix_seed_count": matrix_seed_count,
        "mirror_seed_count": mirror_seed_count,
        "targets": {
            "aggregate": AGGREGATE_TARGET,
            "pair": PAIR_TARGET,
            "mirror": MIRROR_TARGET,
        },
        "tiers": tiers,
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), ensure_ascii=False, indent=2))
