from dataclasses import asdict, dataclass, field

try:
    from .ability import ActiveAbilityDefinition, UserSpell
except ImportError:
    from models.ability import ActiveAbilityDefinition, UserSpell


@dataclass(frozen=True)
class SkillEffect:
    effect_id: str
    per_level: float
    max_bonus: float | None = None
    weapon_types: tuple[str, ...] = ()
    weapon_modes: tuple[str, ...] = ()
    armor_styles: tuple[str, ...] = ()
    pve_only: bool = False


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    category: str
    passive: bool = True
    compatible_weapon_modes: tuple[str, ...] = ()
    stamina_cost: int = 0
    cooldown_ticks: int = 0
    windup_ticks: int = 0
    recovery_ticks: int = 0
    damage_multiplier: float = 1.0
    bonus_knockback: int = 0
    governing_attributes: tuple[str, ...] = ()
    description: str = ""
    effects: tuple[SkillEffect, ...] = ()
    prerequisites: tuple[tuple[str, int], ...] = ()
    future_system: str = ""


@dataclass(frozen=True)
class UserSkill:
    skill_id: str
    level: int
    exp: int
    potential: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SkillGrowth:
    user_pk: int
    skill_id: str
    skill_name: str
    exp_gain: int
    from_level: int
    to_level: int
    potential_after: int


@dataclass(frozen=True)
class SkillBuild:
    skills: dict[str, UserSkill]
    effective_levels: dict[str, int]
    active_skill_ids: tuple[str, ...] = ()
    active_definitions: dict[str, ActiveAbilityDefinition] = field(default_factory=dict)
    level_caps: dict[str, int] = field(default_factory=dict)
    spells: dict[str, UserSpell] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "skills": {key: value.to_dict() for key, value in self.skills.items()},
            "effective_levels": dict(self.effective_levels),
            "active_skill_ids": list(self.active_skill_ids),
            "level_caps": dict(self.level_caps),
            "spells": {key: value.to_dict() for key, value in self.spells.items()},
        }
