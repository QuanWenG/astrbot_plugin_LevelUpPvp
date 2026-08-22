"""Domain models for the interactive, deterministic Nefia dungeon loop.

The aggregate is deliberately persistence-agnostic.  A command handler or a
database adapter may serialize it, while the dungeon domain remains testable
without importing the plugin database layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

try:
    from .combat import FighterContinuationState, SimulationResult
except ImportError:
    from models.combat import FighterContinuationState, SimulationResult


DungeonPhase = Literal[
    "route_choice",
    "risk_choice",
    "combat_ready",
    "cleared",
    "defeated",
    "retreated",
]

DungeonNodeKind = Literal[
    "normal",
    "elite",
    "boss",
    "camp",
    "remains",
    "gathering",
    "hidden_room",
    "treasure",
]


@dataclass(frozen=True)
class DungeonEnvironment:
    environment_id: str
    name: str
    description: str
    combat_environment_id: str
    threat_multiplier: float = 1.0
    reward_multiplier: float = 1.0


@dataclass(frozen=True)
class DungeonRiskChoice:
    risk_id: str
    name: str
    description: str
    monster_level_delta: int = 0
    reward_multiplier: float = 1.0
    entry_hp_cost_ratio: float = 0.0
    entry_mp_cost_ratio: float = 0.0


@dataclass(frozen=True)
class DungeonAffix:
    affix_id: str
    name: str
    description: str
    level_delta: int = 0
    aggression_delta: float = 0.0
    guard_delta: float = 0.0
    reward_multiplier: float = 1.0


@dataclass(frozen=True)
class DungeonDiscovery:
    discovery_id: str
    discovery_type: Literal[
        "ordinary_chest",
        "material_cache",
        "gem_cache",
        "mystery_chest",
        "hidden_room",
        "gathering_point",
        "camp",
        "remains",
    ]
    name: str
    description: str
    reward_multiplier: float = 1.0
    skill_id: str | None = None
    skill_threshold: int = 0
    unlock_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class DungeonRouteOption:
    option_id: str
    name: str
    description: str
    node_kind: DungeonNodeKind
    monster_template_id: str
    monster_level: int
    monster_rank: Literal["normal", "elite", "boss"]
    environment: DungeonEnvironment
    affixes: tuple[DungeonAffix, ...]
    risk_choices: tuple[DungeonRiskChoice, DungeonRiskChoice]
    terrain_id: Literal["forest", "cave", "fortress", "tower"] = "cave"
    terrain_name: str = "洞窟"
    discovery: DungeonDiscovery | None = None
    base_reward_multiplier: float = 1.0


@dataclass(frozen=True)
class DungeonFloor:
    floor_index: int
    routes: tuple[DungeonRouteOption, DungeonRouteOption]


@dataclass(frozen=True)
class DungeonRewardIntent:
    """An immutable request for an external settlement service.

    ``source_key`` is the idempotency key. ``random_seed`` is personal and
    stable, so a retry grants the same item/book instead of rerolling loot.
    """

    source_key: str
    reward_type: Literal[
        "experience", "equipment", "spellbook", "salvage"
    ]
    quantity: int
    random_seed: int
    item_level_min: int = 0
    item_level_max: int = 0
    catalog_id_min: int = 0
    catalog_id_max: int = 0
    spell_pool: tuple[str, ...] = ()
    quality_bonus: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["spell_pool"] = list(self.spell_pool)
        return payload


@dataclass(frozen=True)
class DungeonEncounterRecord:
    floor_index: int
    route_id: str
    risk_id: str
    monster_template_id: str
    monster_rank: str
    environment_id: str
    affix_ids: tuple[str, ...]
    won: bool
    simulation: SimulationResult | None
    narrative: str = ""


@dataclass(frozen=True)
class DungeonAdventure:
    adventure_id: str
    settlement_key: str
    owner_key: str
    group_key: str
    dungeon_id: str
    cycle_key: str
    seed: int
    player_level: int
    difficulty: int
    floors: tuple[DungeonFloor, ...]
    phase: DungeonPhase = "route_choice"
    floor_index: int = 0
    selected_route_id: str | None = None
    selected_risk_id: str | None = None
    continuation_state: FighterContinuationState | None = None
    encounters: tuple[DungeonEncounterRecord, ...] = ()
    reward_intents: tuple[DungeonRewardIntent, ...] = ()
    equipment_misses: int = 0
    spellbook_misses: int = 0
    strategy: str = ""
    version: int = 0

    @property
    def terminal(self) -> bool:
        return self.phase in {"cleared", "defeated", "retreated"}

    @property
    def completed_floors(self) -> int:
        return sum(1 for record in self.encounters if record.won)

    @property
    def current_floor(self) -> DungeonFloor | None:
        if self.terminal or not 0 <= self.floor_index < len(self.floors):
            return None
        return self.floors[self.floor_index]


@dataclass(frozen=True)
class DungeonActionResult:
    adventure: DungeonAdventure
    simulation: SimulationResult | None = None
    newly_earned_intents: tuple[DungeonRewardIntent, ...] = ()
    narrative: str = ""

    @property
    def settlement_ready(self) -> bool:
        return self.adventure.terminal


@dataclass(frozen=True)
class DungeonRiskView:
    risk_id: str
    name: str
    description: str
    monster_level: int
    monster_level_delta: int
    reward_multiplier: float
    entry_hp_cost_ratio: float
    entry_mp_cost_ratio: float
    capability_mitigated: bool = False
    reward_quality_bonus: float = 0.0
    reward_effective_quality_bonus: float = 0.0
    reward_quality_progress: float = 0.0
    reward_minimum_quality: str = "common"
    reward_guaranteed_upgrades: int = 0
    reward_upgrade_chance: float = 0.0
    rare_find_quality_bonus: float = 0.0


@dataclass(frozen=True)
class DungeonRouteView:
    option_id: str
    name: str
    description: str
    node_kind: str
    monster_level: int
    monster_rank: str
    environment_id: str
    environment_name: str
    affix_names: tuple[str, ...]
    terrain_name: str
    discovery_name: str = ""
    discovery_accessible: bool = False
    risk_choices: tuple[DungeonRiskView, ...] = ()
    monster_name: str = ""
    requires_combat: bool = True


@dataclass(frozen=True)
class DungeonAdventureView:
    adventure_id: str
    dungeon_id: str
    cycle_key: str
    phase: DungeonPhase
    floor_number: int
    floor_count: int
    completed_floors: int
    difficulty: int
    strategy: str
    routes: tuple[DungeonRouteView, ...] = ()
    selected_route_id: str | None = None
    selected_risk_id: str | None = None
    hp_ratio: float = 1.0
    mana_ratio: float = 1.0
    stamina_ratio: float = 1.0
    equipment_misses: int = 0
    spellbook_misses: int = 0
    version: int = 0

    @property
    def terminal(self) -> bool:
        return self.phase in {"cleared", "defeated", "retreated"}


@dataclass(frozen=True)
class DungeonRewardReceipt:
    reward_key: str
    reward_type: str
    quantity: int
    applied: bool
    description: str
    exp_gain: int = 0
    scrap_gain: int = 0
    equipment_ids: tuple[int, ...] = ()
    equipment_names: tuple[str, ...] = ()
    spell_ids: tuple[str, ...] = ()
    spell_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class DungeonApplicationResult:
    """Transport-neutral result returned by the persistent application API."""

    view: DungeonAdventureView
    simulation: SimulationResult | None = None
    rewards: tuple[DungeonRewardReceipt, ...] = ()
    skill_growth_count: int = 0
    spell_growth_count: int = 0
    attribute_growth_count: int = 0
    narrative: str = ""
