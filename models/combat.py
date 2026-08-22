from dataclasses import asdict, dataclass, field

try:
    from .attributes import AdvancedAttributes, DerivedStats, PrimaryAttributes
    from .ability import BattleEntity, BattleZone, CombatStatus
    from .equipment import EquipmentBuild
    from .skill import SkillBuild
except ImportError:
    from models.attributes import AdvancedAttributes, DerivedStats, PrimaryAttributes
    from models.ability import BattleEntity, BattleZone, CombatStatus
    from models.equipment import EquipmentBuild
    from models.skill import SkillBuild


@dataclass(frozen=True)
class FighterSnapshot:
    user_pk: int
    name: str
    level: int
    hp: int
    atk: int
    defense: int
    speed: int
    luck: int
    strategy: str
    equipment_modifiers: dict[str, int] = field(default_factory=dict)
    skill_ids: tuple[str, ...] = ("basic_attack",)
    equipment: EquipmentBuild | None = None
    skills: SkillBuild | None = None
    attributes: PrimaryAttributes | None = None
    advanced_attributes: AdvancedAttributes | None = None
    derived: DerivedStats | None = None
    combatant_kind: str = "player"
    source_template_id: str = ""
    rank: str = "normal"

    def stat(self, name: str) -> int:
        if self.derived and hasattr(self.derived, name):
            return max(0, int(getattr(self.derived, name)))
        return max(
            0,
            int(getattr(self, name))
            + int(self.equipment_modifiers.get(name, 0)),
        )

    def primary(self, name: str) -> int:
        if self.attributes:
            return self.attributes.get(name)
        legacy = {
            "strength": self.hp,
            "constitution": self.defense,
            "dexterity": self.speed,
            "perception": self.atk,
            "magic": self.luck,
            "willpower": 5,
        }
        return max(0, int(legacy.get(name, 0)))

    @property
    def weapon_mode(self) -> str:
        return self.equipment.weapon_mode if self.equipment else "one_hand"

    @property
    def weapon_type(self) -> str:
        return self.equipment.weapon_type if self.equipment else ""

    @property
    def armor_style(self) -> str:
        return self.equipment.armor_style if self.equipment else "light"

    @property
    def overloaded(self) -> bool:
        return bool(self.equipment and self.equipment.overloaded)

    @property
    def is_ranged(self) -> bool:
        return self.weapon_mode == "two_hand_ranged"

    @property
    def max_hp(self) -> int:
        return self.derived.max_hp if self.derived else 50 + self.stat("hp") * 10

    @property
    def max_mp(self) -> int:
        return self.derived.max_mp if self.derived else 0

    @property
    def max_sp(self) -> int:
        if self.derived:
            return self.derived.max_sp
        return self.equipment.max_stamina if self.equipment else 100

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["skill_ids"] = list(self.skill_ids)
        payload["max_hp"] = self.max_hp
        payload["max_mp"] = self.max_mp
        payload["max_sp"] = self.max_sp
        return payload


@dataclass(frozen=True)
class AIProfile:
    aggression: float = 0.7
    guard_tendency: float = 0.2
    chase_tendency: float = 0.8
    preferred_range: int = 90
    retreat_tendency: float = 0.15
    low_hp_risk: float = 0.5
    strategy_name: str = "稳扎稳打"
    tactic_plan: tuple[str, str, str] = (
        "sustain",
        "sustain",
        "sustain",
    )


@dataclass(frozen=True)
class ActionIntent:
    action: str
    skill_id: str | None = None


@dataclass(frozen=True)
class FighterContinuationState:
    """Character-owned combat state that can cross battle boundaries."""

    hp_ratio: float = 1.0
    mana_ratio: float = 1.0
    stamina_ratio: float = 1.0
    hp_regen_buffer: float = 0.0
    mp_regen_buffer: float = 0.0
    sp_regen_buffer: float = 0.0
    recovery_turn_phase: int = 0
    statuses: tuple[dict, ...] = ()
    skill_cooldowns: dict[str, int] = field(default_factory=dict)
    attack_cooldown: int = 0
    recovery_ticks: int = 0
    hitstun_ticks: int = 0
    counter_cooldown: int = 0
    hard_control_immunity_ticks: int = 0
    stance_id: str | None = None
    frozen_mana_ratio: float = 0.0
    frozen_mana_capacity_ratio: float = 0.0
    lethal_survival_used: bool = False
    defeated: bool = False
    updated_at_ts: int = 0
    version: int = 0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["statuses"] = list(self.statuses)
        return payload


@dataclass
class FighterState:
    snapshot: FighterSnapshot
    current_hp: int
    position: int
    attack_cooldown: int = 0
    windup_ticks: int = 0
    recovery_ticks: int = 0
    hitstun_ticks: int = 0
    attack_pending: bool = False
    guarding: bool = False
    damage_dealt: int = 0
    stamina: int = 100
    mana: int = 0
    skill_cooldowns: dict[str, int] = field(default_factory=dict)
    pending_skill_id: str | None = None
    pending_resource_details: dict[str, object] = field(default_factory=dict)
    attack_bonus_knockback: int = 0
    hp_regen_buffer: float = 0.0
    mp_regen_buffer: float = 0.0
    sp_regen_buffer: float = 0.0
    recovery_turn_phase: int = 0
    statuses: dict[str, CombatStatus] = field(default_factory=dict)
    stance_id: str | None = None
    frozen_mana: int = 0
    frozen_mana_capacity: int = 0
    lethal_survival_used: bool = False
    counter_cooldown: int = 0
    runtime_armor_style: str = ""
    runtime_weight: float = 0.0
    runtime_overloaded: bool = False
    current_attributes: PrimaryAttributes | None = None
    current_derived: DerivedStats | None = None
    runtime_effective_skills: dict[str, int] = field(default_factory=dict)
    fortune_charges: int = 0
    fortune_cooldown: int = 0
    hard_control_immunity_until: int = 0
    tactic_initiative: float = 0.0
    tactic_counter_sp_cost: float = 0.0

    def __post_init__(self) -> None:
        if self.current_attributes is None:
            self.current_attributes = self.snapshot.attributes
        if self.current_derived is None:
            self.current_derived = self.snapshot.derived
        if not self.runtime_effective_skills and self.snapshot.skills:
            self.runtime_effective_skills = dict(self.snapshot.skills.effective_levels)
        if self.snapshot.equipment and not self.runtime_armor_style:
            self.runtime_armor_style = self.snapshot.equipment.armor_style
            self.runtime_weight = self.snapshot.equipment.total_weight
            self.runtime_overloaded = self.snapshot.equipment.overloaded

    @property
    def alive(self) -> bool:
        return self.current_hp > 0

    @property
    def hp_ratio(self) -> float:
        return max(0.0, self.current_hp / self.max_hp)

    @property
    def max_hp(self) -> int:
        return (
            self.current_derived.max_hp
            if self.current_derived else self.snapshot.max_hp
        )

    @property
    def max_mp(self) -> int:
        return (
            self.current_derived.max_mp
            if self.current_derived else self.snapshot.max_mp
        )

    @property
    def max_sp(self) -> int:
        return (
            self.current_derived.max_sp
            if self.current_derived else self.snapshot.max_sp
        )

    def primary(self, name: str) -> int:
        if self.current_attributes:
            return self.current_attributes.get(name)
        return self.snapshot.primary(name)

    def skill_level(self, skill_id: str) -> int:
        return max(0, int(self.runtime_effective_skills.get(skill_id, 0)))


@dataclass(frozen=True)
class BattleEvent:
    tick: int
    kind: str
    actor_pk: int | None = None
    target_pk: int | None = None
    value: int = 0
    remaining_hp: int | None = None
    position: int | None = None
    critical: bool = False
    guarded: bool = False
    skill_id: str | None = None
    stamina: int | None = None
    mana: int | None = None
    damage_type: str = "physical"
    damage_breakdown: dict[str, int] = field(default_factory=dict)
    status_id: str | None = None
    entity_id: str | None = None
    zone_id: str | None = None
    base_mana_cost: float | None = None
    level_mana_cost: int | None = None
    mana_cost_ratio: float | None = None
    mana_cost: int | None = None
    mana_before: int | None = None
    mana_after: int | None = None
    spell_power: float | None = None
    armor_style: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BattleState:
    tick: int
    attacker: FighterState
    defender: FighterState
    events: list[BattleEvent]
    random_seed: int
    finish_reason: str = ""
    entities: list[BattleEntity] = field(default_factory=list)
    zones: list[BattleZone] = field(default_factory=list)
    ruleset_id: str = "sideview-v11"
    environment_id: str = "calm"
    tactic_phase: str = ""


@dataclass(frozen=True)
class SimulationResult:
    attacker: FighterSnapshot
    defender: FighterSnapshot
    winner_pk: int
    loser_pk: int
    duration_ticks: int
    finish_reason: str
    attacker_remaining_hp: int
    defender_remaining_hp: int
    attacker_damage_dealt: int
    defender_damage_dealt: int
    events: tuple[BattleEvent, ...]
    random_seed: int
    engine_version: str = "sideview-v9"
    attacker_remaining_stamina: int = 0
    defender_remaining_stamina: int = 0
    attacker_remaining_mana: int = 0
    defender_remaining_mana: int = 0
    attacker_frozen_mana: int = 0
    defender_frozen_mana: int = 0
    attacker_final_statuses: tuple[dict, ...] = ()
    defender_final_statuses: tuple[dict, ...] = ()
    final_entities: tuple[dict, ...] = ()
    final_zones: tuple[dict, ...] = ()
    attacker_final_state: FighterContinuationState | None = None
    defender_final_state: FighterContinuationState | None = None
    ruleset_id: str = "sideview-v11"
    environment_id: str = "calm"

    def to_dict(self) -> dict:
        return {
            "engine_version": self.engine_version,
            "ruleset_id": self.ruleset_id,
            "environment_id": self.environment_id,
            "random_seed": self.random_seed,
            "duration_ticks": self.duration_ticks,
            "finish_reason": self.finish_reason,
            "winner_pk": self.winner_pk,
            "loser_pk": self.loser_pk,
            "attacker": self.attacker.to_dict(),
            "defender": self.defender.to_dict(),
            "attacker_remaining_hp": self.attacker_remaining_hp,
            "defender_remaining_hp": self.defender_remaining_hp,
            "attacker_damage_dealt": self.attacker_damage_dealt,
            "defender_damage_dealt": self.defender_damage_dealt,
            "attacker_remaining_stamina": self.attacker_remaining_stamina,
            "defender_remaining_stamina": self.defender_remaining_stamina,
            "attacker_remaining_mana": self.attacker_remaining_mana,
            "defender_remaining_mana": self.defender_remaining_mana,
            "attacker_frozen_mana": self.attacker_frozen_mana,
            "defender_frozen_mana": self.defender_frozen_mana,
            "attacker_final_statuses": list(self.attacker_final_statuses),
            "defender_final_statuses": list(self.defender_final_statuses),
            "final_entities": list(self.final_entities),
            "final_zones": list(self.final_zones),
            "attacker_final_state": (
                self.attacker_final_state.to_dict()
                if self.attacker_final_state else None
            ),
            "defender_final_state": (
                self.defender_final_state.to_dict()
                if self.defender_final_state else None
            ),
            "events": [event.to_dict() for event in self.events],
        }
