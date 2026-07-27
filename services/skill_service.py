from collections import defaultdict

try:
    from ..models.attributes import PrimaryAttributes
    from ..models.skill import SkillGrowth, UserSkill
    from ..models.ability import UserSpell
    from .ability_catalog import (
        ACTIVE_ABILITY_DEFINITIONS, ability_id_for, ability_is_unlocked
    )
    from .attribute_service import skill_level_cap, training_efficiency
    from .db import connect_db
    from .progression_rules import (
        RULESET_ID,
        decay_skill_potential,
        display_exp,
        recover_potential,
        scaled_exp_gain,
    )
    from .skill_catalog import INITIAL_SKILLS, SKILL_DEFINITIONS, skill_exp_required, skill_id_for
    from .user_service import utc_now_text
except ImportError:
    from models.attributes import PrimaryAttributes
    from models.skill import SkillGrowth, UserSkill
    from models.ability import UserSpell
    from services.ability_catalog import (
        ACTIVE_ABILITY_DEFINITIONS, ability_id_for, ability_is_unlocked
    )
    from services.attribute_service import skill_level_cap, training_efficiency
    from services.db import connect_db
    from services.progression_rules import (
        RULESET_ID,
        decay_skill_potential,
        display_exp,
        recover_potential,
        scaled_exp_gain,
    )
    from services.skill_catalog import INITIAL_SKILLS, SKILL_DEFINITIONS, skill_exp_required, skill_id_for
    from services.user_service import utc_now_text


class SkillService:
    MAX_LEVEL = 100
    MAX_EFFECTIVE_LEVEL = 150
    MAX_POTENTIAL = 400
    RAW_XP_CAP = 20

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def ensure_initialized_in_db(self, db, user) -> None:
        cursor = await db.execute(
            "SELECT 1 FROM feature_grants WHERE user_pk = ? AND grant_key = ?",
            (user.id, "skills-v1"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return
        retroactive = max(0, int(user.level) - 1 - int(user.skill_points))
        await db.execute(
            "UPDATE users SET skill_points = skill_points + ? WHERE id = ?",
            (retroactive, user.id),
        )
        for skill_id in INITIAL_SKILLS:
            await db.execute(
                "INSERT OR IGNORE INTO user_skills (user_pk, skill_id, level, exp, potential) VALUES (?, ?, 1, 0, 100)",
                (user.id, skill_id),
            )
        await db.execute(
            "INSERT OR REPLACE INTO active_skill_slots (user_pk, slot, skill_id) VALUES (?, 1, 'power_strike')",
            (user.id,),
        )
        await db.execute(
            "INSERT INTO feature_grants (user_pk, grant_key, created_at) VALUES (?, ?, ?)",
            (user.id, "skills-v1", utc_now_text()),
        )
        user.skill_points += retroactive

    async def skills_in_db(self, db, user_pk: int) -> dict[str, UserSkill]:
        cursor = await db.execute(
            "SELECT skill_id, level, exp, potential FROM user_skills WHERE user_pk = ?",
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {row["skill_id"]: UserSkill(row["skill_id"], int(row["level"]), int(row["exp"]), int(row["potential"])) for row in rows}

    async def active_slots_in_db(self, db, user_pk: int) -> tuple[str, ...]:
        cursor = await db.execute(
            "SELECT slot, skill_id FROM active_skill_slots WHERE user_pk = ? ORDER BY slot",
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        values = [""] * 4
        for row in rows:
            if 1 <= int(row["slot"]) <= 4:
                values[int(row["slot"]) - 1] = row["skill_id"]
        return tuple(values)

    async def get_skills(self, user) -> tuple[dict[str, UserSkill], tuple[str, ...]]:
        async with await connect_db(self.db_path) as db:
            await self.ensure_initialized_in_db(db, user)
            result = await self.skills_in_db(db, user.id), await self.active_slots_in_db(db, user.id)
            await db.commit()
            return result

    async def learn(self, user, name: str) -> UserSkill:
        return (await self.learn_many(user, (name,)))[0]

    async def learn_many(
        self,
        user,
        names: tuple[str, ...] | list[str],
    ) -> list[UserSkill]:
        skill_ids = [skill_id_for(name) for name in names]
        if not skill_ids or any(not skill_id for skill_id in skill_ids):
            raise ValueError("未知技能")
        if len(set(skill_ids)) != len(skill_ids):
            raise ValueError("学习列表中有重复技能")
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                await self.ensure_initialized_in_db(db, user)
                skills = await self.skills_in_db(db, user.id)
                learned = []
                for skill_id in skill_ids:
                    if skill_id in skills:
                        raise ValueError(
                            f"{SKILL_DEFINITIONS[skill_id].name}已经学会"
                        )
                    definition = SKILL_DEFINITIONS[skill_id]
                    missing = self.missing_prerequisites(definition, skills)
                    if missing:
                        progress = "、".join(
                            f"{SKILL_DEFINITIONS[required_id].name} "
                            f"{skills.get(required_id).level if required_id in skills else 0}/{required_level}"
                            for required_id, required_level in missing
                        )
                        raise ValueError(
                            f"{definition.name}前置技能不足：{progress}"
                        )
                    skill = UserSkill(skill_id, 1, 0, 100)
                    skills[skill_id] = skill
                    learned.append(skill)
                cursor = await db.execute(
                    "SELECT skill_points FROM users WHERE id = ?", (user.id,)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if int(row["skill_points"]) < len(learned):
                    raise ValueError(
                        f"技能点不足，需要{len(learned)}点"
                    )
                await db.execute(
                    "UPDATE users SET skill_points = skill_points - ? "
                    "WHERE id = ?",
                    (len(learned), user.id),
                )
                for skill in learned:
                    await db.execute(
                        "INSERT INTO user_skills "
                        "(user_pk, skill_id, level, exp, potential) "
                        "VALUES (?, ?, 1, 0, 100)",
                        (user.id, skill.skill_id),
                    )
                await db.commit()
                return learned
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def missing_prerequisites(definition, skills) -> tuple[tuple[str, int], ...]:
        return tuple(
            (required_id, required_level)
            for required_id, required_level in definition.prerequisites
            if required_id not in skills
            or skills[required_id].level < required_level
        )
    async def train_potential(self, user, name: str, points: int) -> UserSkill:
        return (await self.train_many(user, ((name, points),)))[0]

    async def train_many(
        self,
        user,
        assignments: tuple[tuple[str, int], ...] | list[tuple[str, int]],
    ) -> list[UserSkill]:
        if not assignments:
            raise ValueError("用法：/训练技能 技能名 点数 [...]")
        skill_ids = [skill_id_for(name) for name, _ in assignments]
        if (
            any(not skill_id for skill_id in skill_ids)
            or any(int(points) < 1 for _, points in assignments)
        ):
            raise ValueError("用法：/训练技能 技能名 点数 [...]")
        if len(set(skill_ids)) != len(skill_ids):
            raise ValueError("训练列表中有重复技能")
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                await self.ensure_initialized_in_db(db, user)
                skills = await self.skills_in_db(db, user.id)
                results = []
                total_spent = 0
                for (name, requested), skill_id in zip(
                    assignments, skill_ids
                ):
                    skill = skills.get(skill_id)
                    if not skill:
                        raise ValueError(f"请先学习{name}")
                    potential = skill.potential
                    spent = 0
                    for _ in range(int(requested)):
                        if potential >= self.MAX_POTENTIAL:
                            break
                        potential = recover_potential(potential)
                        spent += 1
                    if not spent:
                        raise ValueError(
                            f"{SKILL_DEFINITIONS[skill_id].name}"
                            "潜力已经达到上限"
                        )
                    total_spent += spent
                    results.append(
                        UserSkill(
                            skill_id, skill.level, skill.exp, potential
                        )
                    )
                cursor = await db.execute(
                    "SELECT skill_points FROM users WHERE id = ?", (user.id,)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if int(row["skill_points"]) < total_spent:
                    raise ValueError(
                        f"技能点不足，需要{total_spent}点"
                    )
                await db.execute(
                    "UPDATE users SET skill_points = skill_points - ? "
                    "WHERE id = ?",
                    (total_spent, user.id),
                )
                for skill in results:
                    await db.execute(
                        "UPDATE user_skills SET potential = ? "
                        "WHERE user_pk = ? AND skill_id = ?",
                        (skill.potential, user.id, skill.skill_id),
                    )
                await db.commit()
                return results
            except Exception:
                await db.rollback()
                raise

    async def set_active_slot(self, user, slot: int, name: str) -> None:
        await self.set_active_slots(user, ((slot, name),))

    async def set_active_slots(
        self,
        user,
        assignments: tuple[tuple[int, str], ...] | list[tuple[int, str]],
    ) -> None:
        if not assignments:
            raise ValueError("请指定技能栏配置")
        if any(int(slot) not in range(1, 5) for slot, _ in assignments):
            raise ValueError("技能栏位置必须是1到4")
        if len({int(slot) for slot, _ in assignments}) != len(assignments):
            raise ValueError("技能栏位置不能重复")
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                await self.ensure_initialized_in_db(db, user)
                skills = await self.skills_in_db(db, user.id)
                cursor = await db.execute(
                    "SELECT spell_id, level, exp, potential FROM user_spells WHERE user_pk = ?",
                    (user.id,),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                spells = {
                    row["spell_id"]: UserSpell(
                        row["spell_id"], int(row["level"]),
                        int(row["exp"]), int(row["potential"]),
                    )
                    for row in rows
                }
                resolved = []
                for slot, name in assignments:
                    if name in {"", "清空"}:
                        resolved.append((int(slot), ""))
                        continue
                    ability_id = ability_id_for(name)
                    if not ability_id:
                        raise ValueError(f"未知主动能力：{name}")
                    definition = ACTIVE_ABILITY_DEFINITIONS[ability_id]
                    if not ability_is_unlocked(definition, skills, spells):
                        if definition.ability_type == "spell":
                            raise ValueError(
                                f"尚未通过魔法书学会{definition.name}"
                            )
                        raise ValueError(
                            f"{definition.name}需要"
                            f"{definition.unlock_skill_id}"
                            f"永久等级达到{definition.unlock_level}"
                        )
                    resolved.append((int(slot), ability_id))
                for slot, ability_id in resolved:
                    await db.execute(
                        "DELETE FROM active_skill_slots "
                        "WHERE user_pk = ? AND slot = ?",
                        (user.id, slot),
                    )
                    if not ability_id:
                        continue
                    await db.execute(
                        "DELETE FROM active_skill_slots "
                        "WHERE user_pk = ? AND skill_id = ?",
                        (user.id, ability_id),
                    )
                    await db.execute(
                        "INSERT OR REPLACE INTO active_skill_slots "
                        "(user_pk, slot, skill_id) VALUES (?, ?, ?)",
                        (user.id, slot, ability_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    def usage_from_simulation(self, result) -> dict[int, dict[str, int]]:
        usage: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        snapshots = {result.attacker.user_pk: result.attacker, result.defender.user_pk: result.defender}
        for event in result.events:
            actor = snapshots.get(event.actor_pk)
            target = snapshots.get(event.target_pk)
            if event.kind in {"damage", "whiff"} and actor:
                definition = ACTIVE_ABILITY_DEFINITIONS.get(event.skill_id or "")
                if not definition or definition.ability_type != "spell":
                    amount = 3 if event.kind == "damage" else 1
                    usage[actor.user_pk][actor.weapon_type or "tactics"] += amount
                    combat_skill = "marksmanship" if actor.weapon_type in {"bow", "crossbow", "firearm"} else "tactics"
                    usage[actor.user_pk][combat_skill] += amount
                    if actor.weapon_mode == "dual_wield": usage[actor.user_pk]["dual_wield"] += amount
                    if actor.weapon_mode in {"one_hand", "two_hand_melee", "two_hand_heavy"}: usage[actor.user_pk]["two_handed"] += amount
                    advanced_by_weapon = {
                        "shortsword": "noble_weapon", "staff": "noble_weapon",
                        "scythe": "cleric_weapon", "blunt": "cleric_weapon",
                        "longsword": "officer_weapon", "spear": "officer_weapon",
                        "axe": "hero_weapon", "unarmed": "hero_weapon",
                    }
                    advanced_id = advanced_by_weapon.get(actor.weapon_type)
                    if (
                        advanced_id
                        and actor.skills
                        and advanced_id in actor.skills.skills
                    ):
                        usage[actor.user_pk][advanced_id] += amount
            if event.kind == "guard" and actor and actor.weapon_mode == "sword_shield": usage[actor.user_pk]["shield"] += 2
            if event.kind == "evade" and actor: usage[actor.user_pk]["dodge"] += 2
            if event.kind in {"damage", "followup", "counter_damage"} and target:
                armor_style = event.armor_style or target.armor_style
                usage[target.user_pk][f"{armor_style}_armor"] += 1
            if event.kind in {"skill_use", "ability_use", "spell_cast"} and actor:
                definition = ACTIVE_ABILITY_DEFINITIONS.get(event.skill_id or "")
                if definition:
                    training_id = (
                        definition.unlock_skill_id
                        if definition.ability_type != "legacy"
                        else "power_strike"
                    )
                    usage[actor.user_pk][training_id] += 3
            if event.kind == "recover_hp" and actor: usage[actor.user_pk]["healing"] += max(1, event.value)
            if event.kind == "recover_mp" and actor: usage[actor.user_pk]["meditation"] += max(1, event.value)
        for snapshot in snapshots.values():
            if snapshot.overloaded:
                usage[snapshot.user_pk]["weightlifting"] += max(1, result.duration_ticks // 10)
        return {pk: {key: min(self.RAW_XP_CAP, value) for key, value in values.items()} for pk, values in usage.items()}

    async def apply_growth_in_db(self, db, user_pk: int, raw_usage: dict[str, int], battle_id: int) -> list[SkillGrowth]:
        skills = await self.skills_in_db(db, user_pk)
        cursor = await db.execute(
            "SELECT hp, atk, defense, speed, luck, willpower FROM users WHERE id = ?",
            (user_pk,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return []
        attributes = PrimaryAttributes(
            int(row["hp"]),
            int(row["defense"]),
            int(row["speed"]),
            int(row["atk"]),
            int(row["luck"]),
            int(row["willpower"]),
        )
        will_efficiency = training_efficiency(attributes.willpower)
        growths = []
        for skill_id, raw in raw_usage.items():
            skill = skills.get(skill_id)
            definition = SKILL_DEFINITIONS.get(skill_id)
            if not skill or not definition or raw <= 0:
                continue
            level_cap = skill_level_cap(
                attributes, definition.governing_attributes, skill_id
            )
            if skill.level >= min(self.MAX_LEVEL, level_cap):
                continue
            gain = scaled_exp_gain(
                min(self.RAW_XP_CAP, raw),
                skill.potential,
                will_efficiency,
            )
            level, exp, potential = skill.level, skill.exp + gain, skill.potential
            old_level, old_potential = level, potential
            while (
                level < min(self.MAX_LEVEL, level_cap)
                and exp >= skill_exp_required(level)
            ):
                exp -= skill_exp_required(level)
                level += 1
                potential = decay_skill_potential(potential)
            await db.execute("UPDATE user_skills SET level = ?, exp = ?, potential = ? WHERE user_pk = ? AND skill_id = ?", (level, exp, potential, user_pk, skill_id))
            await db.execute(
                "INSERT INTO skill_growth_logs "
                "(user_pk, battle_id, skill_id, exp_gain, from_level, "
                "to_level, potential_before, potential_after, created_at, "
                "rules_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_pk, battle_id, skill_id, gain, old_level, level,
                    old_potential, potential, utc_now_text(), RULESET_ID,
                ),
            )
            growths.append(
                SkillGrowth(
                    user_pk,
                    skill_id,
                    SKILL_DEFINITIONS[skill_id].name,
                    display_exp(gain),
                    old_level,
                    level,
                    potential,
                )
            )
        return growths
