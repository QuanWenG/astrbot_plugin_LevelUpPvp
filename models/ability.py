from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ActionEffect:
    effect_type: str
    target: str = "enemy"
    value: float = 0.0
    duration_ticks: int = 0
    chance: float = 1.0
    damage_type: str = "physical"
    status_id: str = ""
    radius: int = 0
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveAbilityDefinition:
    ability_id: str
    name: str
    ability_type: str
    unlock_skill_id: str = ""
    unlock_level: int = 1
    resource_type: str = "sp"
    resource_cost: int = 0
    cooldown_ticks: int = 0
    windup_ticks: int = 1
    recovery_ticks: int = 1
    cast_range: int = 100
    targeting: str = "single"
    compatible_weapon_types: tuple[str, ...] = ()
    compatible_weapon_modes: tuple[str, ...] = ()
    effects: tuple[ActionEffect, ...] = ()
    exclusive_group: str = ""
    freezes_mana: bool = False
    description: str = ""
    ai_tags: tuple[str, ...] = ()
    reading_difficulty: int = 0
    reading_attribute: str = ""
    base_mana_cost: float = 0.0
    mana_cost_mode: str = "scaled"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class UserSpell:
    spell_id: str
    level: int = 1
    exp: int = 0
    potential: int = 100

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SpellBookItem:
    id: int
    owner_pk: int
    spell_id: str
    quantity: int
    source: str
    random_seed: int
    bound: bool = True


@dataclass(frozen=True)
class SpellReadResult:
    spell: UserSpell | None
    success: bool
    chance: float
    random_seed: int
    consumed: int
    potential_gain: int = 0
    reading_power: float = 0.0
    reading_difficulty: int = 0
    reading_attribute: str = ""


@dataclass(frozen=True)
class SpellGrowth:
    user_pk: int
    spell_id: str
    spell_name: str
    exp_gain: int
    from_level: int
    to_level: int
    potential_after: int


@dataclass
class CombatStatus:
    status_id: str
    source_pk: int
    remaining_ticks: int
    stacks: int = 1
    magnitude: float = 0.0
    beneficial: bool = False
    dispellable: bool = True
    params: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BattleEntity:
    entity_id: str
    owner_pk: int
    position: int
    remaining_ticks: int
    aura_radius: int
    aura_effects: tuple[ActionEffect, ...] = ()
    dispellable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BattleZone:
    zone_id: str
    owner_pk: int
    position: int
    radius: int
    remaining_ticks: int
    effects: tuple[ActionEffect, ...] = ()
    affects_owner: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
