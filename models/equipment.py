from dataclasses import asdict, dataclass, field


EQUIPMENT_SLOTS = (
    "head", "neck", "back", "body", "wrist", "main_hand", "off_hand",
    "left_finger", "right_finger", "waist", "feet",
)

SLOT_LABELS = {
    "head": "头", "neck": "颈", "back": "背部", "body": "身体",
    "wrist": "手腕", "main_hand": "主手", "off_hand": "副手",
    "left_finger": "左指", "right_finger": "右指", "waist": "腰",
    "feet": "脚",
}


@dataclass(frozen=True)
class EquipmentTemplate:
    template_id: str
    name: str
    item_type: str
    equip_slot: str
    hand_mode: str = "none"
    weapon_type: str = ""
    armor_type: str = ""
    material: str = "iron"
    weight: float = 0.0
    base_stats: dict[str, float] = field(default_factory=dict)
    inherent_affixes: tuple[dict, ...] = ()
    weight_range_exception: bool = False


@dataclass(frozen=True)
class EquipmentItem:
    id: int | None
    owner_pk: int
    template_id: str
    name: str
    item_type: str
    equip_slot: str
    hand_mode: str
    weapon_type: str
    armor_type: str
    item_level: int
    quality: str
    star_type: str
    material: str
    blessing_state: str
    enhancement_level: int
    weight: float
    enchant_capacity: int
    used_capacity: int
    base_stats: dict[str, float] = field(default_factory=dict)
    inherent_affixes: tuple[dict, ...] = ()
    random_affixes: tuple[dict, ...] = ()
    fusion_affixes: tuple[dict, ...] = ()
    bound: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.item_level <= 100:
            raise ValueError("装备等级必须在0到100之间")
        if self.quality not in {"common", "excellent", "rare", "epic", "mythic", "legendary"}:
            raise ValueError("未知装备品质")
        if self.blessing_state not in {"normal", "blessed", "cursed", "corrupted"}:
            raise ValueError("未知装备状态")
        if self.used_capacity > self.enchant_capacity:
            raise ValueError("词条超出附魔容量")
        if len(self.fusion_affixes) > self.fusion_slot_limit:
            raise ValueError("融合词条数量超过上限")

    @property
    def fusion_slot_limit(self) -> int:
        return 3 if self.star_type == "black_star" else 2 if self.star_type == "white_star" else 0
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EquipmentBuild:
    items: tuple[EquipmentItem, ...]
    slots: dict[str, int]
    stat_modifiers: dict[str, int]
    skill_modifiers: dict[str, int]
    weapon_mode: str
    weapon_type: str
    armor_style: str
    total_weight: float
    carry_capacity: float
    overloaded: bool
    attack_range: int
    damage_multiplier: float
    attack_windup: int
    attack_recovery: int
    attack_cooldown: int
    attack_stamina: int
    movement_multiplier: float
    stamina_regen: int
    max_stamina: int
    block_rate: float = 0.0
    knockback_resistance: float = 0.0
    melee_followup: float = 0.0
    ranged_followup: float = 0.0
    reserved_effects: dict[str, float] = field(default_factory=dict)
    weapon_power: float = 0.0
    armor_power: float = 0.0
    weapon_weight: float = 0.0
    action_speed: float = 100.0
    combat_effects: dict[str, float] = field(default_factory=dict)
    advanced_stat_modifiers: dict[str, int] = field(default_factory=dict)
    item_weights: dict[int, float] = field(default_factory=dict)
    physical_accuracy_multiplier: float = 1.0
    spell_accuracy_multiplier: float = 1.0

    @property
    def is_ranged(self) -> bool:
        return self.weapon_mode == "two_hand_ranged"

    def to_dict(self) -> dict:
        return asdict(self)
