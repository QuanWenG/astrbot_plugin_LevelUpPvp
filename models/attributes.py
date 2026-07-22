from dataclasses import asdict, dataclass, field


PRIMARY_ATTRIBUTE_IDS = (
    "strength",
    "constitution",
    "dexterity",
    "perception",
    "magic",
    "willpower",
)

DAMAGE_TYPES = (
    "magic",
    "fire",
    "cold",
    "lightning",
    "shadow",
    "nature",
    "mind",
    "hell",
)


@dataclass(frozen=True)
class DamageTypeDefinition:
    damage_type: str
    label: str
    status_effect: str = ""
    special_effect: str = ""


@dataclass(frozen=True)
class PrimaryAttributes:
    strength: int
    constitution: int
    dexterity: int
    perception: int
    magic: int
    willpower: int

    def get(self, name: str) -> int:
        return max(0, int(getattr(self, name)))

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AdvancedAttributes:
    life_growth: int = 100
    mana_growth: int = 100
    speed: int = 100
    luck: int = 100

    def get(self, name: str) -> int:
        return max(0, int(getattr(self, name)))

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AttributeProgress:
    attribute_id: str
    exp: int = 0
    potential: int = 100


@dataclass(frozen=True)
class AttributeGrowth:
    user_pk: int
    attribute_id: str
    exp_gain: int
    from_value: int
    to_value: int
    potential_after: int


@dataclass(frozen=True)
class DerivedStats:
    max_hp: int
    max_mp: int
    max_sp: int
    attack_power: float
    accuracy: float
    defense: float
    evasion: float
    critical_rate: float
    critical_damage: float
    action_speed: float
    carry_capacity: float
    physical_accuracy_multiplier: float = 1.0
    spell_accuracy_multiplier: float = 1.0
    physical_damage_multiplier: float = 1.0
    hp_regen_per_tick: float = 0.0
    mp_regen_per_tick: float = 0.0
    healing_power: float = 1.0
    spell_multipliers: dict[str, float] = field(default_factory=dict)
    summon_power: float = 1.0
    blessing_power: float = 1.0
    reading_success: float = 0.0
    magic_potential_gain: float = 1.0
    mana_overcast_reduction: float = 0.0
    pve_stealth: float = 0.0
    physical_reduction: float = 0.0
    magical_reduction: float = 0.0
    resistances: dict[str, float] = field(default_factory=dict)
    elemental_damage: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
