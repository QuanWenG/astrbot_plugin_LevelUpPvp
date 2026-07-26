import random

try:
    from ..models.equipment import EquipmentItem, EquipmentTemplate
    from .material_catalog import (
        MATERIAL_DEFINITIONS, MATERIAL_MULTIPLIERS, material_for,
        validate_base_weight,
    )
except ImportError:
    from models.equipment import EquipmentItem, EquipmentTemplate
    from services.material_catalog import (
        MATERIAL_DEFINITIONS, MATERIAL_MULTIPLIERS, material_for,
        validate_base_weight,
    )


QUALITY_LABELS = {
    "common": "普通", "excellent": "优秀", "rare": "精良",
    "epic": "史诗", "mythic": "传说", "legendary": "传奇",
}
QUALITY_MULTIPLIERS = {
    "common": 0.67, "excellent": 0.83, "rare": 1.00,
    "epic": 1.25, "mythic": 1.40, "legendary": 1.0,
}
QUALITY_COLORS = {
    "common": "白色", "excellent": "绿色", "rare": "蓝色",
    "epic": "紫色", "mythic": "金黄色", "legendary": "红色",
}


def _template(template_id, name, item_type, slot, **kwargs):
    return EquipmentTemplate(template_id, name, item_type, slot, **kwargs)


STARTER_TEMPLATES = (
    _template("training_longsword", "训练长剑", "weapon", "main_hand", hand_mode="one_hand", weapon_type="longsword", material="iron", weight=2.0, base_stats={"weapon_power": 2}),
    _template("training_shield", "训练木盾", "shield", "off_hand", hand_mode="shield", material="wood", weight=1.0, base_stats={"armor_power": 2}, inherent_affixes=({"type": "block_rate", "value": 0.08, "capacity": 0},)),
    _template("training_dagger_left", "训练短剑·左", "weapon", "main_hand", hand_mode="one_hand", weapon_type="shortsword", material="iron", weight=0.8, base_stats={"weapon_power": 1, "action_speed": 3}),
    _template("training_dagger_right", "训练短剑·右", "weapon", "off_hand", hand_mode="one_hand", weapon_type="shortsword", material="iron", weight=0.8, base_stats={"weapon_power": 1, "action_speed": 3}),
    _template("training_greataxe", "训练重斧", "weapon", "main_hand", hand_mode="two_hand_heavy", weapon_type="axe", material="iron", weight=4.2, base_stats={"weapon_power": 4}),
    _template("training_spear", "训练长枪", "weapon", "main_hand", hand_mode="two_hand_melee", weapon_type="spear", material="wood", weight=2.5, base_stats={"weapon_power": 3}),
    _template("training_bow", "训练弓", "weapon", "main_hand", hand_mode="two_hand_ranged", weapon_type="bow", material="wood", weight=1.0, base_stats={"weapon_power": 2}),
    _template("training_firearm", "训练枪械", "weapon", "main_hand", hand_mode="two_hand_ranged", weapon_type="firearm", material="iron", weight=2.0, base_stats={"weapon_power": 3}),
    _template("training_throwing", "训练投掷组", "weapon", "main_hand", hand_mode="two_hand_ranged", weapon_type="throwing", material="iron", weight=0.4, base_stats={"weapon_power": 2, "action_speed": 3}),
    _template("training_cap", "训练布帽", "armor", "head", armor_type="light", material="cloth", weight=0.4, base_stats={"armor_power": 1}),
    _template("training_amulet", "训练项链", "accessory", "neck", material="iron", weight=0.1, base_stats={"max_hp": 10}),
    _template("training_cape", "训练披风", "armor", "back", armor_type="light", material="cloth", weight=0.7, base_stats={"evasion": 1}),
    _template("training_clothes", "训练轻甲", "armor", "body", armor_type="light", material="leather", weight=1.8, base_stats={"armor_power": 2, "action_speed": 3}),
    _template("training_gloves", "训练手套", "armor", "wrist", armor_type="light", material="cloth", weight=0.2, base_stats={"accuracy": 1}),
    _template("training_ring_left", "训练戒指·左", "accessory", "left_finger", material="iron", weight=0.1, base_stats={"perception": 1}),
    _template("training_ring_right", "训练戒指·右", "accessory", "right_finger", material="iron", weight=0.1, base_stats={"perception": 1}),
    _template("training_belt", "训练腰带", "armor", "waist", armor_type="light", material="leather", weight=0.6, base_stats={"max_hp": 10}),
    _template("training_boots", "训练轻靴", "armor", "feet", armor_type="light", material="leather", weight=0.3, base_stats={"action_speed": 3}),
)
STARTER_BY_ID = {item.template_id: item for item in STARTER_TEMPLATES}
BLACK_STAR_TEMPLATES: dict[str, EquipmentTemplate] = {}


class EquipmentFactory:
    AFFIX_TYPES = ("stat_flat", "skill_level", "block_rate", "knockback_resistance", "melee_followup", "ranged_followup", "accuracy", "evasion", "critical_rate", "resistance_fire", "resistance_cold", "resistance_lightning", "resistance_shadow", "resistance_nature", "resistance_mind", "resistance_hell", "resistance_magic", "damage_magic", "damage_fire", "damage_cold", "damage_lightning", "damage_shadow", "damage_nature", "damage_mind", "damage_hell")

    def generate(self, owner_pk: int, template: EquipmentTemplate, item_level: int, quality: str, seed: int) -> EquipmentItem:
        material_for(template.material)
        validate_base_weight(
            template, allow_exception=template.weight_range_exception
        )
        if not 0 <= item_level <= 100:
            raise ValueError("装备等级必须在0到100之间")
        if quality not in QUALITY_MULTIPLIERS:
            raise ValueError("未知装备品质")
        if quality == "legendary":
            raise ValueError("黑星装备必须来自固定模板")
        rng = random.Random(seed)
        star_type = "white_star" if quality in {"epic", "mythic"} else ("black_star" if quality == "legendary" else "none")
        affix_count = {"common": 0, "excellent": 1, "rare": 2, "epic": 3, "mythic": 4, "legendary": 0}[quality]
        random_affixes = []
        for _ in range(affix_count):
            kind = rng.choice(self.AFFIX_TYPES)
            affix = {"type": kind, "capacity": 1}
            if kind == "stat_flat":
                affix.update(stat=rng.choice(("strength", "constitution", "dexterity", "perception", "magic", "willpower")), value=rng.randint(1, 3))
            elif kind == "skill_level":
                affix.update(
                    skill_id=rng.choice(
                        (
                            "longsword", "shortsword", "axe", "spear",
                            "bow", "firearm", "throwing", "tactics",
                        )
                    ),
                    value=rng.randint(1, 3),
                )
            elif kind.startswith("resistance_"):
                affix["value"] = rng.randint(10, 50)
            elif kind in {"accuracy", "evasion"} or kind.startswith("damage_"):
                affix["value"] = rng.randint(1, 5)
            else:
                affix["value"] = round(rng.uniform(0.02, 0.10), 3)
            random_affixes.append(affix)
        random_affixes = tuple(random_affixes)
        capacity = {"common": 0, "excellent": 2, "rare": 4, "epic": 7, "mythic": 10, "legendary": 12}[quality]
        return EquipmentItem(None, owner_pk, template.template_id, template.name, template.item_type, template.equip_slot, template.hand_mode, template.weapon_type, template.armor_type, item_level, quality, star_type, template.material, "normal", 0, template.weight, capacity, affix_count, dict(template.base_stats), template.inherent_affixes, random_affixes, (), True)


def starter_item(owner_pk: int, template: EquipmentTemplate) -> EquipmentItem:
    material_for(template.material)
    validate_base_weight(
        template, allow_exception=template.weight_range_exception
    )
    return EquipmentItem(None, owner_pk, template.template_id, template.name, template.item_type, template.equip_slot, template.hand_mode, template.weapon_type, template.armor_type, 0, "common", "none", template.material, "normal", 0, template.weight, 0, 0, dict(template.base_stats), template.inherent_affixes, (), (), True)
