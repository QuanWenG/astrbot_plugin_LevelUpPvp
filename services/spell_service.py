import random
import secrets
from collections import defaultdict

try:
    from ..models.ability import (
        SpellBookItem,
        SpellGrowth,
        SpellReadResult,
        UserSpell,
    )
    from .ability_catalog import (
        SPELL_DEFINITIONS,
        spell_exp_required,
    )
    from .attribute_service import training_efficiency
    from .build_service import CombatBuildService
    from .db import connect_db
    from .progression_rules import (
        RULESET_ID,
        decay_skill_potential,
        display_exp,
        recover_potential,
        scaled_exp_gain,
        spell_level_cap,
    )
    from .user_service import utc_now_text
except ImportError:
    from models.ability import SpellBookItem, SpellGrowth, SpellReadResult, UserSpell
    from services.ability_catalog import SPELL_DEFINITIONS, spell_exp_required
    from services.attribute_service import training_efficiency
    from services.build_service import CombatBuildService
    from services.db import connect_db
    from services.progression_rules import (
        RULESET_ID,
        decay_skill_potential,
        display_exp,
        recover_potential,
        scaled_exp_gain,
        spell_level_cap,
    )
    from services.user_service import utc_now_text


class SpellService:
    MAX_LEVEL = 100
    MAX_POTENTIAL = 400
    RAW_XP_CAP = 20

    def __init__(
        self, db_path: str, skill_service=None,
        equipment_service=None, attribute_service=None,
    ):
        self.db_path = db_path
        self.skill_service = skill_service
        self.equipment_service = equipment_service
        self.attribute_service = attribute_service

    async def spells_in_db(self, db, user_pk: int) -> dict[str, UserSpell]:
        cursor = await db.execute(
            "SELECT spell_id, level, exp, potential FROM user_spells WHERE user_pk = ?",
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            row["spell_id"]: UserSpell(
                row["spell_id"],
                int(row["level"]),
                int(row["exp"]),
                int(row["potential"]),
            )
            for row in rows
        }

    async def get_spells(self, user_pk: int) -> dict[str, UserSpell]:
        async with await connect_db(self.db_path) as db:
            return await self.spells_in_db(db, user_pk)

    async def books_in_db(self, db, user_pk: int) -> list[SpellBookItem]:
        cursor = await db.execute(
            """
            SELECT id, owner_pk, spell_id, quantity, source, random_seed, bound
            FROM spellbook_items
            WHERE owner_pk = ? AND quantity > 0
            ORDER BY id
            """,
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            SpellBookItem(
                int(row["id"]),
                int(row["owner_pk"]),
                row["spell_id"],
                int(row["quantity"]),
                row["source"],
                int(row["random_seed"]),
                bool(row["bound"]),
            )
            for row in rows
        ]

    async def list_books(self, user_pk: int) -> list[SpellBookItem]:
        async with await connect_db(self.db_path) as db:
            return await self.books_in_db(db, user_pk)

    async def grant_book_in_db(
        self,
        db,
        user_pk: int,
        spell_id: str,
        quantity: int = 1,
        source: str = "internal",
        random_seed: int | None = None,
    ) -> SpellBookItem:
        if spell_id not in SPELL_DEFINITIONS:
            raise ValueError("未知魔法书")
        if quantity < 1:
            raise ValueError("魔法书数量必须大于0")
        seed = int(random_seed if random_seed is not None else secrets.randbits(62))
        await db.execute(
            """
            INSERT INTO spellbook_items (
                owner_pk, spell_id, quantity, source,
                random_seed, bound, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (user_pk, spell_id, quantity, source, seed, utc_now_text()),
        )
        cursor = await db.execute("SELECT last_insert_rowid() AS id")
        row = await cursor.fetchone()
        await cursor.close()
        return SpellBookItem(
            int(row["id"]), user_pk, spell_id, quantity, source, seed, True
        )

    async def grant_book(
        self,
        user_pk: int,
        spell_id: str,
        quantity: int = 1,
        source: str = "internal",
        random_seed: int | None = None,
    ) -> SpellBookItem:
        async with await connect_db(self.db_path) as db:
            item = await self.grant_book_in_db(
                db, user_pk, spell_id, quantity, source, random_seed
            )
            await db.commit()
            return item

    async def read_book(
        self,
        user,
        book_id: int,
        random_seed: int | None = None,
        reading_bonus: float = 0.0,
        reading_power_bonus: float = 0.0,
    ) -> SpellReadResult:
        seed = int(random_seed if random_seed is not None else secrets.randbits(62))
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT id, owner_pk, spell_id, quantity
                FROM spellbook_items
                WHERE id = ? AND owner_pk = ? AND quantity > 0
                """,
                (book_id, user.id),
            )
            book = await cursor.fetchone()
            await cursor.close()
            if not book:
                raise ValueError("魔法书不存在或已经用完")
            definition = SPELL_DEFINITIONS.get(book["spell_id"])
            if not definition:
                raise ValueError("魔法书内容无法识别")

            cursor = await db.execute(
                "SELECT skill_id, level FROM user_skills WHERE user_pk = ?",
                (user.id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            permanent_levels = {
                row["skill_id"]: int(row["level"]) for row in rows
            }
            school_level = permanent_levels.get(definition.unlock_skill_id, 0)
            if school_level < 1:
                raise ValueError(
                    f"需要先学会{definition.unlock_skill_id}（永久等级Lv.1），"
                    f"当前Lv.{school_level}"
                )

            effective_levels = dict(permanent_levels)
            attributes = {
                "strength": int(user.strength),
                "constitution": int(user.constitution),
                "dexterity": int(user.dexterity),
                "perception": int(user.perception),
                "magic": int(user.magic),
                "willpower": int(user.willpower),
            }
            equipment_power = 0.0
            equipment_chance = 0.0
            if self.equipment_service and self.skill_service and self.attribute_service:
                await self.equipment_service.ensure_starter_in_db(db, user.id)
                slots, items = await self.equipment_service.loadout_in_db(db, user.id)
                learned = await self.skill_service.skills_in_db(db, user.id)
                equipment = CombatBuildService(
                    self.equipment_service,
                    self.skill_service,
                    self.attribute_service,
                ).resolve_equipment(user, slots, items, learned)
                effective_levels = {
                    skill_id: min(
                        self.skill_service.MAX_EFFECTIVE_LEVEL,
                        level + equipment.skill_modifiers.get(skill_id, 0),
                    )
                    for skill_id, level in permanent_levels.items()
                }
                combat_attributes = self.attribute_service.attributes_for_user(
                    user, equipment.stat_modifiers
                )
                attributes = {
                    attribute_id: combat_attributes.get(attribute_id)
                    for attribute_id in attributes
                }
                equipment_power = float(
                    equipment.combat_effects.get("reading_power", 0.0)
                )
                equipment_chance = float(
                    equipment.combat_effects.get("reading_success", 0.0)
                )

            reading_level = effective_levels.get("reading", 0)
            effective_school = effective_levels.get(definition.unlock_skill_id, 0)
            governing_value = attributes[definition.reading_attribute]
            reading_power = (
                80
                + reading_level * 8
                + governing_value * 5
                + effective_school * 3
                + equipment_power
                + reading_power_bonus
            )
            chance = max(
                0.05,
                min(
                    0.95,
                    0.50
                    + (reading_power - definition.reading_difficulty) * 0.001
                    + equipment_chance
                    + reading_bonus,
                ),
            )
            success = random.Random(seed).random() < chance
            spells = await self.spells_in_db(db, user.id)
            existing = spells.get(definition.ability_id)
            before = existing.potential if existing else 0
            potential_gain = 0
            result_spell = existing
            if success and not existing:
                result_spell = UserSpell(definition.ability_id, 1, 0, 100)
                await db.execute(
                    """
                    INSERT INTO user_spells (
                        user_pk, spell_id, level, exp, potential
                    ) VALUES (?, ?, 1, 0, 100)
                    """,
                    (user.id, definition.ability_id),
                )
            elif success and existing:
                silent_reading = effective_levels.get("silent_reading", 0)
                potential = recover_potential(
                    existing.potential,
                    1 + silent_reading * 0.005,
                )
                potential_gain = potential - existing.potential
                result_spell = UserSpell(
                    existing.spell_id,
                    existing.level,
                    existing.exp,
                    potential,
                )
                await db.execute(
                    """
                    UPDATE user_spells SET potential = ?
                    WHERE user_pk = ? AND spell_id = ?
                    """,
                    (potential, user.id, definition.ability_id),
                )

            if int(book["quantity"]) == 1:
                await db.execute(
                    "DELETE FROM spellbook_items WHERE id = ?", (book_id,)
                )
            else:
                await db.execute(
                    "UPDATE spellbook_items SET quantity = quantity - 1 WHERE id = ?",
                    (book_id,),
                )
            after = result_spell.potential if result_spell else before
            await db.execute(
                """
                INSERT INTO spell_read_logs (
                    user_pk, spell_id, book_item_id, success,
                    success_chance, random_seed, potential_before,
                    potential_after, reading_difficulty, reading_power,
                    reading_attribute, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    definition.ability_id,
                    book_id,
                    int(success),
                    chance,
                    seed,
                    before,
                    after,
                    definition.reading_difficulty,
                    reading_power,
                    definition.reading_attribute,
                    utc_now_text(),
                ),
            )
            if self.skill_service and success:
                raw = {"reading": 3}
                if existing:
                    raw["silent_reading"] = 2
                await self.skill_service.apply_growth_in_db(
                    db, user.id, raw, None
                )
            await db.commit()
            return SpellReadResult(
                result_spell,
                success,
                chance,
                seed,
                1,
                potential_gain,
                reading_power,
                definition.reading_difficulty,
                definition.reading_attribute,
            )

    def usage_from_simulation(self, result) -> dict[int, dict[str, int]]:
        usage: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for event in result.events:
            if (
                event.kind == "spell_cast"
                and event.actor_pk is not None
                and event.skill_id in SPELL_DEFINITIONS
            ):
                usage[event.actor_pk][event.skill_id] += 3
        return {
            user_pk: {
                spell_id: min(self.RAW_XP_CAP, raw)
                for spell_id, raw in values.items()
            }
            for user_pk, values in usage.items()
        }

    async def apply_growth_in_db(
        self,
        db,
        user_pk: int,
        raw_usage: dict[str, int],
        battle_id: int | None,
    ) -> list[SpellGrowth]:
        spells = await self.spells_in_db(db, user_pk)
        cursor = await db.execute(
            "SELECT skill_id, level FROM user_skills WHERE user_pk = ?",
            (user_pk,),
        )
        skill_rows = await cursor.fetchall()
        await cursor.close()
        skill_levels = {
            row["skill_id"]: int(row["level"]) for row in skill_rows
        }
        cursor = await db.execute(
            "SELECT willpower FROM users WHERE id = ?", (user_pk,)
        )
        user_row = await cursor.fetchone()
        await cursor.close()
        if not user_row:
            return []
        will_efficiency = training_efficiency(int(user_row["willpower"]))
        growths: list[SpellGrowth] = []
        for spell_id, raw in raw_usage.items():
            current = spells.get(spell_id)
            definition = SPELL_DEFINITIONS.get(spell_id)
            if not current or not definition or raw <= 0:
                continue
            level_cap = spell_level_cap(
                skill_levels.get(definition.unlock_skill_id, 0),
                self.MAX_LEVEL,
            )
            if current.level >= level_cap:
                continue
            gain = scaled_exp_gain(
                min(self.RAW_XP_CAP, raw),
                current.potential,
                will_efficiency,
            )
            level = current.level
            exp = current.exp + gain
            potential = current.potential
            while level < level_cap and exp >= spell_exp_required(level):
                exp -= spell_exp_required(level)
                level += 1
                potential = decay_skill_potential(potential)
            await db.execute(
                """
                UPDATE user_spells
                SET level = ?, exp = ?, potential = ?
                WHERE user_pk = ? AND spell_id = ?
                """,
                (level, exp, potential, user_pk, spell_id),
            )
            await db.execute(
                """
                INSERT INTO spell_growth_logs (
                    user_pk, battle_id, spell_id, exp_gain,
                    from_level, to_level, potential_before,
                    potential_after, created_at, rules_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_pk,
                    battle_id,
                    spell_id,
                    gain,
                    current.level,
                    level,
                    current.potential,
                    potential,
                    utc_now_text(),
                    RULESET_ID,
                ),
            )
            growths.append(
                SpellGrowth(
                    user_pk,
                    spell_id,
                    definition.name,
                    display_exp(gain),
                    current.level,
                    level,
                    potential,
                )
            )
        return growths
