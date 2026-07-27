from dataclasses import asdict, dataclass, field

try:
    from .combat import AIProfile, FighterSnapshot
    from .attributes import AdvancedAttributes, PrimaryAttributes
except ImportError:
    from models.combat import AIProfile, FighterSnapshot
    from models.attributes import AdvancedAttributes, PrimaryAttributes


@dataclass(frozen=True)
class MonsterTemplate:
    catalog_id: int
    template_id: str
    name: str
    source_name_ja: str
    base_level: int
    race_id: str
    class_id: str
    rank: str = "normal"
    hostile: bool = True
    capturable: bool = True
    attribute_weights: dict[str, float] = field(default_factory=dict)
    skill_coefficients: dict[str, dict[str, float]] = field(
        default_factory=dict
    )
    abilities: tuple[dict, ...] = ()
    removed_ability_ids: tuple[str, ...] = ()
    combat: dict[str, object] = field(default_factory=dict)
    resistances: dict[str, float] = field(default_factory=dict)
    source_effects: tuple[str, ...] = ()
    source_stats: dict[str, object] = field(default_factory=dict)
    provenance: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["abilities"] = list(self.abilities)
        payload["removed_ability_ids"] = list(
            self.removed_ability_ids
        )
        payload["source_effects"] = list(self.source_effects)
        return payload


@dataclass(frozen=True)
class MonsterSpawnSpec:
    template_id: str
    level: int | None = None
    rank: str | None = None
    combatant_pk: int | None = None


@dataclass(frozen=True)
class MonsterBuild:
    template: MonsterTemplate
    level: int
    rank: str
    attributes: PrimaryAttributes
    advanced_attributes: AdvancedAttributes
    skill_levels: dict[str, int]
    ability_ids: tuple[str, ...]
    ai_profile: AIProfile
    weapon_power: float
    armor_power: float
    snapshot: FighterSnapshot
    provenance: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "template": self.template.to_dict(),
            "level": self.level,
            "rank": self.rank,
            "attributes": self.attributes.to_dict(),
            "advanced_attributes": self.advanced_attributes.to_dict(),
            "skill_levels": dict(self.skill_levels),
            "ability_ids": list(self.ability_ids),
            "ai_profile": asdict(self.ai_profile),
            "weapon_power": self.weapon_power,
            "armor_power": self.armor_power,
            "snapshot": self.snapshot.to_dict(),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class MonsterCatalogSnapshot:
    schema_version: int
    scaling_version: str
    defaults: dict[str, object]
    races: dict[str, dict]
    classes: dict[str, dict]
    ai_profiles: dict[str, AIProfile]
    ranks: dict[str, dict]
    monsters: tuple[MonsterTemplate, ...]
    by_id: dict[int, MonsterTemplate]
    by_template_id: dict[str, MonsterTemplate]
