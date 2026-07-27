try:
    from ..models.attributes import (
        DAMAGE_TYPES,
        PRIMARY_ATTRIBUTE_IDS,
        AttributeGrowth,
        DamageTypeDefinition,
        AttributeProgress,
        AdvancedAttributes,
        DerivedStats,
        PrimaryAttributes,
    )
    from .db import connect_db
    from .balance_rules import (
        attack_mode_attribute,
        physical_offense_multiplier,
        resistance_multiplier,
    )
    from .progression_rules import (
        RULESET_ID,
        attribute_exp_required,
        decay_attribute_potential,
        display_exp,
        scaled_exp_gain,
        skill_level_cap as scaled_skill_level_cap,
    )
    from .user_service import utc_now_text
except ImportError:
    from models.attributes import (
        DAMAGE_TYPES,
        PRIMARY_ATTRIBUTE_IDS,
        AttributeGrowth,
        DamageTypeDefinition,
        AttributeProgress,
        AdvancedAttributes,
        DerivedStats,
        PrimaryAttributes,
    )
    from services.db import connect_db
    from services.balance_rules import (
        attack_mode_attribute,
        physical_offense_multiplier,
        resistance_multiplier,
    )
    from services.progression_rules import (
        RULESET_ID,
        attribute_exp_required,
        decay_attribute_potential,
        display_exp,
        scaled_exp_gain,
        skill_level_cap as scaled_skill_level_cap,
    )
    from services.user_service import utc_now_text


ATTRIBUTE_LABELS = {
    "strength": "力量",
    "constitution": "体质",
    "dexterity": "灵巧",
    "perception": "感知",
    "magic": "魔力",
    "willpower": "意志",
}

ADVANCED_ATTRIBUTE_LABELS = {
    "life_growth": "生命成长", "mana_growth": "魔法成长",
    "speed": "速度", "luck": "幸运",
}
ADVANCED_STORAGE_COLUMNS = {
    "life_growth": "life_growth", "mana_growth": "mana_growth",
    "speed": "advanced_speed", "luck": "advanced_luck",
}

DAMAGE_TYPE_LABELS = {
    "physical": "物理",
    "magic": "魔法",
    "fire": "火焰",
    "cold": "寒冰",
    "lightning": "雷电",
    "shadow": "暗影",
    "nature": "自然",
    "mind": "精神",
    "hell": "地狱",
}

DAMAGE_TYPE_DEFINITIONS = {
    item.damage_type: item
    for item in (
        DamageTypeDefinition("magic", "魔法"),
        DamageTypeDefinition("fire", "火焰", "burn", "wet_resistance_bonus"),
        DamageTypeDefinition("cold", "寒冰", "chill"),
        DamageTypeDefinition("lightning", "雷电", "paralysis", "wet_resistance_penalty"),
        DamageTypeDefinition("shadow", "暗影", "blind"),
        DamageTypeDefinition("nature", "自然", "poison"),
        DamageTypeDefinition("mind", "精神", "confusion"),
        DamageTypeDefinition("hell", "地狱", "disease", "life_steal"),
    )
}

LEGACY_ATTRIBUTE_MAP = {
    "hp": "strength",
    "defense": "constitution",
    "speed": "dexterity",
    "atk": "perception",
    "luck": "magic",
}

WEAPON_PRIMARY_WEIGHTS = {
    "longsword": {"strength": 1.0},
    "axe": {"strength": 1.0},
    "scythe": {"strength": 1.0},
    "unarmed": {"strength": 0.7, "dexterity": 0.3},
    "shortsword": {"dexterity": 1.0},
    "bow": {"dexterity": 1.0},
    "crossbow": {"dexterity": 1.0},
    "throwing": {"strength": 0.5, "dexterity": 0.5},
    "spear": {"constitution": 1.0},
    "blunt": {"constitution": 1.0},
    "staff": {"constitution": 1.0},
    "firearm": {"perception": 1.0},
    "": {"strength": 0.7, "dexterity": 0.3},
}


def normalize_attribute_id(value: str) -> str | None:
    normalized = (value or "").strip().lower()
    aliases = {
        **{key: key for key in PRIMARY_ATTRIBUTE_IDS},
        **LEGACY_ATTRIBUTE_MAP,
        "str": "strength",
        "con": "constitution",
        "dex": "dexterity",
        "per": "perception",
        "wil": "willpower",
        "力量": "strength",
        "体质": "constitution",
        "灵巧": "dexterity",
        "感知": "perception",
        "魔力": "magic",
        "意志": "willpower",
        "生命": "strength",
        "血量": "strength",
        "防御": "constitution",
        "速度": "dexterity",
        "攻击": "perception",
        "幸运": "magic",
    }
    return aliases.get(normalized)


def skill_level_cap(
    attributes: PrimaryAttributes,
    governing: tuple[str, ...],
    skill_id: str = "",
) -> int:
    primary = max((attributes.get(name) for name in governing), default=0)
    cap = scaled_skill_level_cap(attributes.magic, primary)
    if skill_id == "healing":
        cap = min(cap, max(1, attributes.constitution))
    elif skill_id == "meditation":
        cap = min(cap, max(1, attributes.willpower))
    return cap

def training_efficiency(willpower: int) -> float:
    return min(2.0, 1.0 + max(0, willpower) * 0.01)


def elemental_multiplier(resistance: float, attacker_level: int = 1) -> float:
    """Compatibility wrapper for the level-relative resistance-point curve."""
    return resistance_multiplier(resistance, attacker_level)


class AttributeService:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path

    MAX_POTENTIAL = 400
    CHECKIN_POTENTIAL_RESTORE = 10
    BATTLE_RAW_EXP_CAP = 20

    def attributes_for_user(self, user, modifiers=None) -> PrimaryAttributes:
        modifiers = modifiers or {}
        return PrimaryAttributes(
            *(
                max(0, int(getattr(user, name)) + int(modifiers.get(name, 0)))
                for name in PRIMARY_ATTRIBUTE_IDS
            )
        )

    def advanced_attributes_for_user(
        self, user, modifiers=None
    ) -> AdvancedAttributes:
        modifiers = modifiers or {}
        return AdvancedAttributes(
            max(0, int(user.life_growth) + int(modifiers.get("life_growth", 0))),
            max(0, int(user.mana_growth) + int(modifiers.get("mana_growth", 0))),
            max(0, int(user.advanced_speed) + int(modifiers.get("speed", 0))),
            max(0, int(user.advanced_luck) + int(modifiers.get("luck", 0))),
        )

    async def increase_advanced_attribute_in_db(
        self,
        db,
        user_pk: int,
        attribute_id: str,
        amount: int,
        source: str,
    ) -> AdvancedAttributes:
        if attribute_id not in ADVANCED_STORAGE_COLUMNS:
            raise ValueError("未知高级属性")
        if amount <= 0:
            raise ValueError("高级属性增加量必须为正整数")
        column = ADVANCED_STORAGE_COLUMNS[attribute_id]
        cursor = await db.execute(
            f"SELECT {column} AS value FROM users WHERE id = ?",
            (user_pk,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError("用户不存在")
        before = int(row["value"])
        after = before + int(amount)
        await db.execute(
            f"UPDATE users SET {column} = ?, updated_at = ? WHERE id = ?",
            (after, utc_now_text(), user_pk),
        )
        await db.execute(
            """
            INSERT INTO advanced_attribute_logs (
                user_pk, attribute_id, amount, value_before,
                value_after, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_pk, attribute_id, int(amount), before,
                after, source or "internal", utc_now_text(),
            ),
        )
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_pk,))
        updated = await cursor.fetchone()
        await cursor.close()
        return AdvancedAttributes(
            int(updated["life_growth"]), int(updated["mana_growth"]),
            int(updated["advanced_speed"]), int(updated["advanced_luck"]),
        )

    async def increase_advanced_attribute(
        self,
        user_pk: int,
        attribute_id: str,
        amount: int,
        source: str = "internal",
    ) -> AdvancedAttributes:
        if not self.db_path:
            raise ValueError("高级属性接口需要数据库路径")
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            result = await self.increase_advanced_attribute_in_db(
                db, user_pk, attribute_id, amount, source
            )
            await db.commit()
            return result
    def weapon_primary(self, attributes: PrimaryAttributes, weapon_type: str) -> float:
        weights = WEAPON_PRIMARY_WEIGHTS.get(
            weapon_type, WEAPON_PRIMARY_WEIGHTS[""]
        )
        return sum(attributes.get(name) * weight for name, weight in weights.items())

    def derive(
        self,
        *,
        level: int,
        attributes: PrimaryAttributes,
        equipment,
        advanced: AdvancedAttributes | None = None,
        effective_skills: dict[str, int],
    ) -> DerivedStats:
        try:
            from .passive_effects import resolve_passive_bonuses
        except ImportError:
            from services.passive_effects import resolve_passive_bonuses

        effects = equipment.combat_effects
        advanced = advanced or AdvancedAttributes()
        primary = self.weapon_primary(attributes, equipment.weapon_type)
        passives = resolve_passive_bonuses(effective_skills, equipment)
        attribute_values = {
            name: attributes.get(name) for name in PRIMARY_ATTRIBUTE_IDS
        }
        mode_attribute = attack_mode_attribute(
            equipment.weapon_mode,
            equipment.weapon_type,
            attribute_values,
        )
        combat_skill_id = (
            "marksmanship"
            if equipment.weapon_type in {"bow", "crossbow", "firearm"}
            else "tactics"
        )
        offense_multiplier = physical_offense_multiplier(
            weapon_primary=primary,
            mode_attribute=mode_attribute,
            combat_skill_level=effective_skills.get(combat_skill_id, 0),
            weapon_skill_level=effective_skills.get(
                equipment.weapon_type or "unarmed", 0
            ),
            weapon_weight=equipment.weapon_weight,
            style_multiplier=equipment.damage_multiplier,
        )

        base_hp = (
            50
            + level * 2
            + attributes.strength * 3
            + attributes.constitution * 7
            + attributes.willpower * 2
            + effects.get("max_hp", 0)
        )
        max_hp = round(base_hp * advanced.life_growth / 100)
        base_mp = (
            20
            + level
            + attributes.constitution
            + attributes.perception * 2
            + attributes.magic * 7
            + attributes.willpower * 2
            + effects.get("max_mp", 0)
        )
        max_mp = round(base_mp * advanced.mana_growth / 100)
        max_sp = round(
            60
            + attributes.constitution * 3
            + attributes.willpower * 2
            + effects.get("max_sp", 0)
        )
        attack_power = (
            equipment.weapon_power * 3
            + passives.attack_power
            + effects.get("attack_power", 0)
        )
        accuracy = (
            75
            + primary * 1.2
            + passives.accuracy
            + effects.get("accuracy", 0)
        )
        defense = (
            attributes.constitution * 0.8
            + equipment.armor_power * 2
            + passives.defense
        )
        evasion = (
            attributes.dexterity * 0.8
            + passives.evasion
            + effects.get("evasion", 0)
        )
        critical_rate = min(
            0.35,
            0.03
            + attributes.perception * 0.0015
            + passives.critical_rate
            + effects.get("critical_rate", 0),
        )
        critical_damage = min(
            2.50,
            1.50
            + equipment.weapon_weight * 0.01
            + passives.critical_damage
            + effects.get("critical_damage", 0),
        )
        action_speed = max(50.0, min(180.0, equipment.action_speed))
        resistances = {
            damage_type: float(
                effects.get(f"resistance_{damage_type}", 0.0)
            )
            for damage_type in DAMAGE_TYPES
        }
        elemental_damage = {
            damage_type: max(0.0, effects.get(f"damage_{damage_type}", 0.0))
            for damage_type in DAMAGE_TYPES
        }
        spell_schools = (
            "arcane", "barrier", "fire", "cold", "lightning",
            "shadow", "nature", "mind", "hell",
        )
        spell_multipliers = {
            school: 1.0 + passives.spell_bonuses.get(school, 0.0)
            for school in spell_schools
        }
        return DerivedStats(
            max_hp=max(1, max_hp),
            max_mp=max(0, max_mp),
            max_sp=max(1, max_sp),
            attack_power=max(1.0, attack_power),
            accuracy=max(1.0, accuracy),
            defense=max(0.0, defense),
            evasion=max(0.0, evasion),
            critical_rate=critical_rate,
            critical_damage=critical_damage,
            action_speed=action_speed,
            carry_capacity=equipment.carry_capacity,
            physical_accuracy_multiplier=equipment.physical_accuracy_multiplier,
            spell_accuracy_multiplier=equipment.spell_accuracy_multiplier,
            physical_damage_multiplier=offense_multiplier,
            hp_regen_per_tick=max(0.0, passives.hp_regen_per_tick),
            mp_regen_per_tick=max(0.0, passives.mp_regen_per_tick),
            healing_power=max(0.0, 1.0 + passives.healing_power_bonus),
            spell_multipliers=spell_multipliers,
            summon_power=max(0.0, 1.0 + passives.summon_power_bonus),
            blessing_power=max(0.0, 1.0 + passives.blessing_power_bonus),
            reading_success=max(0.0, passives.reading_success),
            magic_potential_gain=max(0.0, 1.0 + passives.magic_potential_bonus),
            mana_overcast_reduction=max(
                0.0, min(0.50, passives.mana_overcast_reduction)
            ),
            pve_stealth=max(0.0, min(0.50, passives.pve_stealth)),
            physical_reduction=max(
                0.0,
                min(
                    0.75,
                    effects.get("physical_reduction", 0.0)
                    + passives.physical_reduction,
                ),
            ),
            magical_reduction=max(
                0.0, min(0.75, effects.get("magical_reduction", 0.0))
            ),
            resistances=resistances,
            elemental_damage=elemental_damage,
        )
    async def apply_battle_growth_in_db(
        self,
        db,
        user_pk: int,
        raw_usage: dict[str, int],
        battle_id: int | None,
    ) -> list[AttributeGrowth]:
        try:
            from .skill_catalog import SKILL_DEFINITIONS
        except ImportError:
            from services.skill_catalog import SKILL_DEFINITIONS

        cursor = await db.execute(
            "SELECT hp, atk, defense, speed, luck, willpower FROM users WHERE id = ?",
            (user_pk,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return []
        values = {
            "strength": int(row["hp"]),
            "constitution": int(row["defense"]),
            "dexterity": int(row["speed"]),
            "perception": int(row["atk"]),
            "magic": int(row["luck"]),
            "willpower": int(row["willpower"]),
        }
        storage_columns = {
            "strength": "hp",
            "constitution": "defense",
            "dexterity": "speed",
            "perception": "atk",
            "magic": "luck",
            "willpower": "willpower",
        }
        progress = await self.progress_in_db(db, user_pk)
        raw_by_attribute: dict[str, float] = {
            attribute_id: 0.0 for attribute_id in PRIMARY_ATTRIBUTE_IDS
        }
        for skill_id, raw in raw_usage.items():
            definition = SKILL_DEFINITIONS.get(skill_id)
            if not definition or not definition.governing_attributes or raw <= 0:
                continue
            share = min(20, raw) / len(definition.governing_attributes)
            for attribute_id in definition.governing_attributes:
                raw_by_attribute[attribute_id] += share

        growths: list[AttributeGrowth] = []
        efficiency = training_efficiency(values["willpower"])
        for attribute_id, raw in raw_by_attribute.items():
            if raw <= 0:
                continue
            state = progress[attribute_id]
            gain = scaled_exp_gain(
                min(self.BATTLE_RAW_EXP_CAP, raw),
                state.potential,
                efficiency,
            )
            value = values[attribute_id]
            old_value = value
            exp = state.exp + gain
            potential = state.potential
            while exp >= attribute_exp_required(value):
                exp -= attribute_exp_required(value)
                value += 1
                potential = decay_attribute_potential(potential)
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = ?, potential = ?
                WHERE user_pk = ? AND attribute_id = ?
                """,
                (exp, potential, user_pk, attribute_id),
            )
            if value != old_value:
                column = storage_columns[attribute_id]
                await db.execute(
                    f"UPDATE users SET {column} = ? WHERE id = ?",
                    (value, user_pk),
                )
            await db.execute(
                """
                INSERT INTO attribute_growth_logs (
                    user_pk, battle_id, attribute_id, exp_gain,
                    from_value, to_value, potential_before,
                    potential_after, created_at, rules_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_pk,
                    battle_id,
                    attribute_id,
                    gain,
                    old_value,
                    value,
                    state.potential,
                    potential,
                    utc_now_text(),
                    RULESET_ID,
                ),
            )
            growths.append(
                AttributeGrowth(
                    user_pk,
                    attribute_id,
                    display_exp(gain),
                    old_value,
                    value,
                    potential,
                )
            )
        return growths
    async def get_progress(self, user_pk: int) -> dict[str, AttributeProgress]:
        if not self.db_path:
            raise RuntimeError("属性服务未配置数据库路径")
        async with await connect_db(self.db_path) as db:
            result = await self.progress_in_db(db, user_pk)
            await db.commit()
            return result
    async def ensure_progress_in_db(self, db, user_pk: int) -> None:
        for attribute_id in PRIMARY_ATTRIBUTE_IDS:
            await db.execute(
                """
                INSERT OR IGNORE INTO user_attribute_progress
                    (user_pk, attribute_id, exp, potential)
                VALUES (?, ?, 0, 100)
                """,
                (user_pk, attribute_id),
            )

    async def progress_in_db(self, db, user_pk: int) -> dict[str, AttributeProgress]:
        await self.ensure_progress_in_db(db, user_pk)
        cursor = await db.execute(
            "SELECT attribute_id, exp, potential FROM user_attribute_progress WHERE user_pk = ?",
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            row["attribute_id"]: AttributeProgress(
                row["attribute_id"], int(row["exp"]), int(row["potential"])
            )
            for row in rows
        }

    async def restore_checkin_potential_in_db(self, db, user_pk: int) -> int:
        await self.ensure_progress_in_db(db, user_pk)
        cursor = await db.execute(
            "SELECT SUM(potential) AS total FROM user_attribute_progress WHERE user_pk = ?",
            (user_pk,),
        )
        before = int((await cursor.fetchone())["total"] or 0)
        await cursor.close()
        await db.execute(
            """
            UPDATE user_attribute_progress
            SET potential = MIN(400, potential + ?)
            WHERE user_pk = ?
            """,
            (self.CHECKIN_POTENTIAL_RESTORE, user_pk),
        )
        cursor = await db.execute(
            "SELECT SUM(potential) AS total FROM user_attribute_progress WHERE user_pk = ?",
            (user_pk,),
        )
        after = int((await cursor.fetchone())["total"] or 0)
        await cursor.close()
        return after - before
