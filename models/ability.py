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
class SpellBookDrop:
    """A deterministic spellbook roll before it is persisted."""

    spell_id: str
    spell_name: str
    rarity: str
    reading_difficulty: int
    random_seed: int


@dataclass(frozen=True)
class SpellBookGrantResult:
    """Result returned by caller-owned, idempotent reward transactions."""

    applied: bool
    reward_key: str
    drop: SpellBookDrop
    item: SpellBookItem | None = None


@dataclass(frozen=True)
class SpellBookCollectionEntry:
    """One spell's held books plus its current reading state."""

    spell_id: str
    spell_name: str
    school_id: str
    items: tuple[SpellBookItem, ...]
    quantity: int
    learned_spell: UserSpell | None
    success_chance: float
    reading_power: float
    reading_difficulty: int
    reading_attribute: str
    study_progress: float
    studied_today: bool
    school_level: int
    research_pages_per_book: int = 0

    @property
    def oldest_book_id(self) -> int:
        return self.items[0].id


@dataclass(frozen=True)
class SpellResearchCraftOption:
    spell_id: str
    spell_name: str
    school_id: str
    cost: int
    affordable: bool


@dataclass(frozen=True)
class SpellBookLibrary:
    entries: tuple[SpellBookCollectionEntry, ...]
    learned_count: int
    total_spell_count: int
    research_pages: int
    craft_options: tuple[SpellResearchCraftOption, ...] = ()


@dataclass(frozen=True)
class SpellBookCraftResult:
    item: SpellBookItem
    spell_name: str
    pages_spent: int
    pages_balance: int


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
    book_retained: bool = False
    study_progress: float = 0.0
    outcome: str = ""
    research_pages_gain: int = 0
    research_pages_balance: int = 0


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
    attack_effects: tuple[ActionEffect, ...] = ()
    attack_interval: int = 0
    spawned_tick: int = 0
    source_ability_id: str = ""

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
