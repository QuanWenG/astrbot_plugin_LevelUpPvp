import hashlib
import random
import secrets
from collections import defaultdict

try:
    from ..models.ability import (
        SpellBookCollectionEntry,
        SpellBookCraftResult,
        SpellBookItem,
        SpellBookDrop,
        SpellBookGrantResult,
        SpellBookLibrary,
        SpellResearchCraftOption,
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
    from .daily_growth_budget import daily_growth_day_window
    from .skill_catalog import SKILL_DEFINITIONS
    from .progression_rules import (
        RULESET_ID,
        decay_spell_potential,
        display_exp,
        recover_potential,
        scaled_exp_gain,
        spell_level_cap,
    )
    from .user_service import utc_now_text
except ImportError:
    from models.ability import (
        SpellBookCollectionEntry,
        SpellBookCraftResult,
        SpellBookDrop,
        SpellBookGrantResult,
        SpellBookItem,
        SpellBookLibrary,
        SpellResearchCraftOption,
        SpellGrowth,
        SpellReadResult,
        UserSpell,
    )
    from services.ability_catalog import SPELL_DEFINITIONS, spell_exp_required
    from services.attribute_service import training_efficiency
    from services.build_service import CombatBuildService
    from services.db import connect_db
    from services.daily_growth_budget import daily_growth_day_window
    from services.skill_catalog import SKILL_DEFINITIONS
    from services.progression_rules import (
        RULESET_ID,
        decay_spell_potential,
        display_exp,
        recover_potential,
        scaled_exp_gain,
        spell_level_cap,
    )
    from services.user_service import utc_now_text


SPELLBOOK_RARITY_WEIGHTS = {
    "common": 12.0,
    "uncommon": 6.0,
    "rare": 2.5,
    "legendary": 1.0,
}

SPELL_RESEARCH_PAGES_BY_RARITY = {
    "common": 3,
    "uncommon": 5,
    "rare": 8,
    "legendary": 12,
}

SPELLBOOK_CRAFT_COST_BY_RARITY = {
    "common": 12,
    "uncommon": 20,
    "rare": 32,
    "legendary": 48,
}


def spellbook_rarity(reading_difficulty: int) -> str:
    if reading_difficulty <= 250:
        return "common"
    if reading_difficulty <= 650:
        return "uncommon"
    if reading_difficulty <= 1050:
        return "rare"
    return "legendary"


def spellbook_tier_cap(player_level: int) -> int:
    """Highest spell unlock tier eligible for a player's random book drop."""
    level = max(1, min(100, int(player_level)))
    if level < 20:
        return 1
    if level < 50:
        return 20
    if level < 80:
        return 50
    return 80


def spell_research_pages(reading_difficulty: int) -> int:
    return SPELL_RESEARCH_PAGES_BY_RARITY[
        spellbook_rarity(reading_difficulty)
    ]


def spellbook_craft_cost(reading_difficulty: int) -> int:
    return SPELLBOOK_CRAFT_COST_BY_RARITY[
        spellbook_rarity(reading_difficulty)
    ]


def select_spellbook_drop(
    *,
    random_seed: int,
    player_level: int = 1,
    known_spell_ids: tuple[str, ...] | list[str] | set[str] = (),
    preferred_schools: tuple[str, ...] | list[str] | set[str] = (),
    excluded_spell_ids: tuple[str, ...] | list[str] | set[str] = (),
) -> SpellBookDrop:
    """Roll a deterministic, level-shaped book without hiding the long tail."""
    level = max(1, min(100, int(player_level)))
    tier_cap = spellbook_tier_cap(level)
    schools = {str(value) for value in preferred_schools if str(value)}
    candidates = [
        definition for definition in SPELL_DEFINITIONS.values()
        if definition.unlock_level <= tier_cap
        and (not schools or definition.unlock_skill_id in schools)
    ]
    if not candidates:
        candidates = [
            definition for definition in SPELL_DEFINITIONS.values()
            if definition.unlock_level <= tier_cap
        ]
    excluded = {str(value) for value in excluded_spell_ids}
    unblocked = [
        definition
        for definition in candidates
        if definition.ability_id not in excluded
    ]
    # Exclusions express a novelty preference, not a way to create an empty
    # loot table when a player already owns every reachable book.
    if unblocked:
        candidates = unblocked
    known = {str(value) for value in known_spell_ids}
    comfortable_difficulty = 160 + level * 14
    weighted = []
    for definition in sorted(candidates, key=lambda item: item.ability_id):
        rarity = spellbook_rarity(definition.reading_difficulty)
        weight = SPELLBOOK_RARITY_WEIGHTS[rarity]
        if definition.ability_id not in known:
            weight *= 2.5
        if definition.reading_difficulty > comfortable_difficulty:
            weight *= max(
                0.18,
                comfortable_difficulty / definition.reading_difficulty,
            )
        weighted.append((definition, rarity, weight))
    rng = random.Random(int(random_seed))
    cursor = rng.random() * sum(item[2] for item in weighted)
    chosen, rarity, _ = weighted[-1]
    for definition, candidate_rarity, weight in weighted:
        cursor -= weight
        if cursor <= 0:
            chosen, rarity = definition, candidate_rarity
            break
    return SpellBookDrop(
        chosen.ability_id,
        chosen.name,
        rarity,
        chosen.reading_difficulty,
        int(random_seed),
    )


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

    @staticmethod
    def spell_id_for_reference(reference: str) -> str:
        value = str(reference or "").strip()
        if value in SPELL_DEFINITIONS:
            return value
        matches = [
            spell_id
            for spell_id, definition in SPELL_DEFINITIONS.items()
            if definition.name == value
        ]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"找不到法术或魔法书《{value}》")

    async def _book_row_in_db(self, db, user_pk: int, reference):
        value = str(reference or "").strip()
        if isinstance(reference, int) or value.isdigit():
            cursor = await db.execute(
                """
                SELECT id, owner_pk, spell_id, quantity, source,
                       random_seed, bound
                FROM spellbook_items
                WHERE id = ? AND owner_pk = ? AND quantity > 0
                """,
                (int(value), int(user_pk)),
            )
        else:
            spell_id = self.spell_id_for_reference(value)
            cursor = await db.execute(
                """
                SELECT id, owner_pk, spell_id, quantity, source,
                       random_seed, bound
                FROM spellbook_items
                WHERE owner_pk = ? AND spell_id = ? AND quantity > 0
                ORDER BY id
                LIMIT 1
                """,
                (int(user_pk), spell_id),
            )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise ValueError("魔法书不存在或已经用完")
        return row

    @staticmethod
    async def _consume_book_copy_in_db(db, book) -> None:
        if int(book["quantity"]) <= 1:
            await db.execute(
                "DELETE FROM spellbook_items WHERE id = ?", (int(book["id"]),)
            )
        else:
            await db.execute(
                "UPDATE spellbook_items SET quantity = quantity - 1 WHERE id = ?",
                (int(book["id"]),),
            )

    async def research_balance_in_db(self, db, user_pk: int) -> int:
        cursor = await db.execute(
            "SELECT pages FROM spell_research_balances WHERE user_pk = ?",
            (int(user_pk),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["pages"]) if row is not None else 0

    async def get_research_balance(self, user_pk: int) -> int:
        async with await connect_db(self.db_path) as db:
            return await self.research_balance_in_db(db, user_pk)

    async def _change_research_pages_in_db(
        self,
        db,
        *,
        user_pk: int,
        spell_id: str,
        delta: int,
        reason: str,
        operation_key: str,
        source_book_id: int | None = None,
        source_seed: int | None = None,
        result_book_id: int | None = None,
    ) -> tuple[bool, int]:
        cursor = await db.execute(
            "SELECT user_pk, spell_id, delta, reason, balance_after "
            "FROM spell_research_logs WHERE operation_key = ?",
            (str(operation_key),),
        )
        duplicate = await cursor.fetchone()
        await cursor.close()
        if duplicate is not None:
            if (
                int(duplicate["user_pk"]) != int(user_pk)
                or str(duplicate["spell_id"]) != str(spell_id)
                or int(duplicate["delta"]) != int(delta)
                or str(duplicate["reason"]) != str(reason)
            ):
                raise RuntimeError("咒文残页操作键与既有审计记录冲突")
            return False, await self.research_balance_in_db(db, user_pk)

        balance = await self.research_balance_in_db(db, user_pk)
        balance_after = balance + int(delta)
        if balance_after < 0:
            raise ValueError(
                f"咒文残页不足：需要{-int(delta)}，当前只有{balance}"
            )
        definition = SPELL_DEFINITIONS.get(spell_id)
        school_id = definition.unlock_skill_id if definition is not None else ""
        await db.execute(
            """
            INSERT INTO spell_research_logs (
                user_pk, spell_id, school_id, delta, balance_after,
                reason, operation_key, source_book_id, source_seed,
                result_book_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_pk),
                str(spell_id),
                school_id,
                int(delta),
                balance_after,
                str(reason),
                str(operation_key),
                source_book_id,
                source_seed,
                result_book_id,
                utc_now_text(),
            ),
        )
        await db.execute(
            """
            INSERT INTO spell_research_balances (user_pk, pages, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_pk) DO UPDATE SET
                pages = excluded.pages,
                updated_at = excluded.updated_at
            """,
            (int(user_pk), balance_after, utc_now_text()),
        )
        return True, balance_after

    async def _reading_profile_in_db(self, db, user) -> dict[str, object]:
        cursor = await db.execute(
            "SELECT skill_id, level FROM user_skills WHERE user_pk = ?",
            (int(user.id),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        permanent_levels = {
            str(row["skill_id"]): int(row["level"]) for row in rows
        }
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
                attribute_id: int(combat_attributes.get(attribute_id))
                for attribute_id in attributes
            }
            equipment_power = float(
                equipment.combat_effects.get("reading_power", 0.0)
            )
            equipment_chance = float(
                equipment.combat_effects.get("reading_success", 0.0)
            )
        return {
            "permanent_levels": permanent_levels,
            "effective_levels": effective_levels,
            "attributes": attributes,
            "equipment_power": equipment_power,
            "equipment_chance": equipment_chance,
        }

    @staticmethod
    def _reading_metrics(
        definition,
        profile: dict[str, object],
        study_progress: float,
        *,
        reading_bonus: float = 0.0,
        reading_power_bonus: float = 0.0,
    ) -> tuple[float, float]:
        effective_levels = profile["effective_levels"]
        attributes = profile["attributes"]
        reading_level = effective_levels.get("reading", 0)
        effective_school = effective_levels.get(definition.unlock_skill_id, 0)
        governing_value = attributes[definition.reading_attribute]
        reading_power = (
            80
            + reading_level * 8
            + governing_value * 5
            + effective_school * 3
            + float(profile["equipment_power"])
            + float(reading_power_bonus)
        )
        chance = min(
            0.95,
            max(
                0.05,
                0.50
                + (reading_power - definition.reading_difficulty) * 0.001
                + float(profile["equipment_chance"])
                + float(reading_bonus),
            )
            + float(study_progress),
        )
        return float(reading_power), float(chance)

    async def _study_states_in_db(
        self, db, user_pk: int, activity_day_key: str
    ) -> dict[str, tuple[int, bool]]:
        cursor = await db.execute(
            """
            SELECT spell_id, success, activity_day_key
            FROM spell_read_logs
            WHERE user_pk = ?
            ORDER BY id
            """,
            (int(user_pk),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        states: dict[str, tuple[int, bool]] = {}
        for row in rows:
            spell_id = str(row["spell_id"])
            failures, studied_today = states.get(spell_id, (0, False))
            if bool(row["success"]):
                failures = 0
            else:
                failures += 1
                studied_today = studied_today or (
                    str(row["activity_day_key"] or "") == activity_day_key
                )
            states[spell_id] = failures, studied_today
        return states

    async def get_book_library(self, user, now=None) -> SpellBookLibrary:
        async with await connect_db(self.db_path) as db:
            books = await self.books_in_db(db, int(user.id))
            spells = await self.spells_in_db(db, int(user.id))
            profile = await self._reading_profile_in_db(db, user)
            activity_day_key, _, _ = daily_growth_day_window(now)
            study_states = await self._study_states_in_db(
                db, int(user.id), activity_day_key
            )
            grouped: dict[str, list[SpellBookItem]] = defaultdict(list)
            for book in books:
                grouped[book.spell_id].append(book)
            entries: list[SpellBookCollectionEntry] = []
            permanent_levels = profile["permanent_levels"]
            for spell_id, items in grouped.items():
                definition = SPELL_DEFINITIONS.get(spell_id)
                if definition is None:
                    continue
                failed_attempts, studied_today = study_states.get(
                    spell_id, (0, False)
                )
                study_progress = min(0.90, failed_attempts * 0.10)
                reading_power, chance = self._reading_metrics(
                    definition, profile, study_progress
                )
                learned = spells.get(spell_id)
                pages = (
                    spell_research_pages(definition.reading_difficulty)
                    if learned is not None
                    and learned.potential >= self.MAX_POTENTIAL
                    else 0
                )
                entries.append(
                    SpellBookCollectionEntry(
                        spell_id=spell_id,
                        spell_name=definition.name,
                        school_id=definition.unlock_skill_id,
                        items=tuple(items),
                        quantity=sum(item.quantity for item in items),
                        learned_spell=learned,
                        success_chance=1.0 if pages else chance,
                        reading_power=reading_power,
                        reading_difficulty=definition.reading_difficulty,
                        reading_attribute=definition.reading_attribute,
                        study_progress=study_progress,
                        studied_today=studied_today,
                        school_level=int(
                            permanent_levels.get(definition.unlock_skill_id, 0)
                        ),
                        research_pages_per_book=pages,
                    )
                )
            entries.sort(key=lambda entry: (entry.spell_name, entry.spell_id))
            balance = await self.research_balance_in_db(db, int(user.id))
            cap = spellbook_tier_cap(int(user.level))
            options = []
            for spell_id, definition in SPELL_DEFINITIONS.items():
                if (
                    spell_id in spells
                    or spell_id in grouped
                    or definition.unlock_level > cap
                    or int(permanent_levels.get(definition.unlock_skill_id, 0)) < 1
                ):
                    continue
                cost = spellbook_craft_cost(definition.reading_difficulty)
                options.append(
                    SpellResearchCraftOption(
                        spell_id,
                        definition.name,
                        definition.unlock_skill_id,
                        cost,
                        balance >= cost,
                    )
                )
            options.sort(
                key=lambda option: (
                    not option.affordable,
                    option.cost,
                    option.spell_name,
                )
            )
            return SpellBookLibrary(
                tuple(entries),
                len(spells),
                len(SPELL_DEFINITIONS),
                balance,
                tuple(options),
            )

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
        seed = int(
            random_seed if random_seed is not None else secrets.randbits(62)
        ) & ((1 << 62) - 1)
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

    async def grant_book_reward_in_db(
        self,
        db,
        *,
        user_pk: int,
        spell_id: str,
        reward_key: str,
        source: str,
        quantity: int = 1,
        random_seed: int | None = None,
    ) -> SpellBookGrantResult:
        """Grant once inside the caller's transaction; never commits itself."""
        source = str(source).strip()
        reward_key = str(reward_key).strip()
        if not source or not reward_key:
            raise ValueError("source 和 reward_key 不能为空")
        if spell_id not in SPELL_DEFINITIONS:
            raise ValueError("未知魔法书")
        if quantity < 1:
            raise ValueError("魔法书数量必须大于0")
        identity = f"{source}\0{reward_key}".encode("utf-8")
        digest = hashlib.blake2b(identity, digest_size=20).hexdigest()
        grant_key = f"spellbook-reward-v1:{digest}"
        cursor = await db.execute(
            "INSERT OR IGNORE INTO feature_grants "
            "(user_pk, grant_key, created_at) VALUES (?, ?, ?)",
            (int(user_pk), grant_key, utc_now_text()),
        )
        await cursor.close()
        cursor = await db.execute("SELECT changes() AS count")
        row = await cursor.fetchone()
        await cursor.close()
        applied = int(row["count"]) == 1
        seed = int(
            int(random_seed) & ((1 << 62) - 1)
            if random_seed is not None
            else int.from_bytes(
                hashlib.blake2b(
                    f"{user_pk}\0{source}\0{reward_key}\0{spell_id}".encode(
                        "utf-8"
                    ),
                    digest_size=8,
                ).digest(),
                "big",
            ) & ((1 << 62) - 1)
        )
        definition = SPELL_DEFINITIONS[spell_id]
        drop = SpellBookDrop(
            spell_id,
            definition.name,
            spellbook_rarity(definition.reading_difficulty),
            definition.reading_difficulty,
            seed,
        )
        if not applied:
            return SpellBookGrantResult(False, reward_key, drop, None)
        item = await self.grant_book_in_db(
            db,
            int(user_pk),
            spell_id,
            int(quantity),
            source,
            seed,
        )
        return SpellBookGrantResult(True, reward_key, drop, item)

    async def grant_random_book_reward_in_db(
        self,
        db,
        *,
        user_pk: int,
        reward_key: str,
        source: str,
        random_seed: int,
        player_level: int = 1,
        known_spell_ids: tuple[str, ...] | list[str] | set[str] = (),
        preferred_schools: tuple[str, ...] | list[str] | set[str] = (),
    ) -> SpellBookGrantResult:
        drop = select_spellbook_drop(
            random_seed=int(random_seed),
            player_level=player_level,
            known_spell_ids=known_spell_ids,
            preferred_schools=preferred_schools,
        )
        return await self.grant_book_reward_in_db(
            db,
            user_pk=user_pk,
            spell_id=drop.spell_id,
            reward_key=reward_key,
            source=source,
            random_seed=drop.random_seed,
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

    async def craft_book(
        self,
        user,
        spell_reference: str,
        *,
        random_seed: int | None = None,
    ) -> SpellBookCraftResult:
        spell_id = self.spell_id_for_reference(spell_reference)
        definition = SPELL_DEFINITIONS[spell_id]
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            spells = await self.spells_in_db(db, int(user.id))
            if spell_id in spells:
                raise ValueError(
                    f"已经学会《{definition.name}》，残页应留给尚未收集的法术"
                )
            cursor = await db.execute(
                "SELECT id FROM spellbook_items "
                "WHERE owner_pk = ? AND spell_id = ? AND quantity > 0 "
                "ORDER BY id LIMIT 1",
                (int(user.id), spell_id),
            )
            held = await cursor.fetchone()
            await cursor.close()
            if held is not None:
                raise ValueError(
                    f"背包里已有《{definition.name}》#{int(held['id'])}，"
                    f"请先 /阅读 {int(held['id'])}"
                )
            tier_cap = spellbook_tier_cap(int(user.level))
            if definition.unlock_level > tier_cap:
                raise ValueError(
                    f"《{definition.name}》属于Lv.{definition.unlock_level}阶段，"
                    f"当前成长阶段只能研制Lv.{tier_cap}及以下法术"
                )
            cursor = await db.execute(
                "SELECT level FROM user_skills "
                "WHERE user_pk = ? AND skill_id = ?",
                (int(user.id), definition.unlock_skill_id),
            )
            school = await cursor.fetchone()
            await cursor.close()
            school_level = int(school["level"]) if school is not None else 0
            if school_level < 1:
                school_definition = SKILL_DEFINITIONS.get(
                    definition.unlock_skill_id
                )
                school_name = (
                    school_definition.name
                    if school_definition is not None
                    else definition.unlock_skill_id
                )
                raise ValueError(
                    f"需要先学会{school_name}，再定向研制这个学派的魔法书"
                )
            cost = spellbook_craft_cost(definition.reading_difficulty)
            balance = await self.research_balance_in_db(db, int(user.id))
            if balance < cost:
                raise ValueError(
                    f"研制《{definition.name}》需要{cost}张咒文残页，"
                    f"当前只有{balance}张"
                )
            seed = int(
                random_seed if random_seed is not None else secrets.randbits(62)
            ) & ((1 << 62) - 1)
            item = await self.grant_book_in_db(
                db,
                int(user.id),
                spell_id,
                1,
                "spell_research",
                seed,
            )
            _, balance_after = await self._change_research_pages_in_db(
                db,
                user_pk=int(user.id),
                spell_id=spell_id,
                delta=-cost,
                reason="targeted_spellbook_craft",
                operation_key=f"spell-research-craft-v1:{user.id}:{item.id}",
                result_book_id=item.id,
                source_seed=seed,
            )
            await db.commit()
            return SpellBookCraftResult(
                item,
                definition.name,
                cost,
                balance_after,
            )

    async def read_book(
        self,
        user,
        book_id: int | str,
        random_seed: int | None = None,
        reading_bonus: float = 0.0,
        reading_power_bonus: float = 0.0,
        now=None,
    ) -> SpellReadResult:
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            book = await self._book_row_in_db(db, int(user.id), book_id)
            definition = SPELL_DEFINITIONS.get(book["spell_id"])
            if not definition:
                raise ValueError("魔法书内容无法识别")
            spells = await self.spells_in_db(db, int(user.id))
            existing = spells.get(definition.ability_id)
            activity_day_key, _, _ = daily_growth_day_window(now)
            if existing is not None and existing.potential >= self.MAX_POTENTIAL:
                pages = spell_research_pages(definition.reading_difficulty)
                quantity_before = int(book["quantity"])
                operation_key = (
                    f"spell-research-recycle-v1:{user.id}:"
                    f"{int(book['id'])}:{quantity_before}"
                )
                _, balance_after = await self._change_research_pages_in_db(
                    db,
                    user_pk=int(user.id),
                    spell_id=definition.ability_id,
                    delta=pages,
                    reason="max_potential_duplicate_book",
                    operation_key=operation_key,
                    source_book_id=int(book["id"]),
                    source_seed=int(book["random_seed"]),
                )
                await self._consume_book_copy_in_db(db, book)
                await db.execute(
                    """
                    INSERT INTO spell_read_logs (
                        user_pk, spell_id, book_item_id, success,
                        success_chance, random_seed, potential_before,
                        potential_after, reading_difficulty, reading_power,
                        reading_attribute, activity_day_key, created_at
                    ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        int(user.id),
                        definition.ability_id,
                        int(book["id"]),
                        int(book["random_seed"]),
                        existing.potential,
                        existing.potential,
                        definition.reading_difficulty,
                        definition.reading_attribute,
                        activity_day_key,
                        utc_now_text(),
                    ),
                )
                await db.commit()
                return SpellReadResult(
                    existing,
                    True,
                    1.0,
                    int(book["random_seed"]),
                    1,
                    0,
                    0.0,
                    definition.reading_difficulty,
                    definition.reading_attribute,
                    False,
                    0.0,
                    "research_converted",
                    pages,
                    balance_after,
                )

            profile = await self._reading_profile_in_db(db, user)
            permanent_levels = profile["permanent_levels"]
            school_level = permanent_levels.get(definition.unlock_skill_id, 0)
            if school_level < 1:
                school = SKILL_DEFINITIONS.get(definition.unlock_skill_id)
                school_name = (
                    school.name if school is not None else definition.unlock_skill_id
                )
                raise ValueError(
                    f"需要先学会{school_name}（永久等级Lv.1），"
                    f"当前Lv.{school_level}；可用 /学习 {school_name} 消耗1技能点学习"
                )

            study_states = await self._study_states_in_db(
                db, int(user.id), activity_day_key
            )
            failed_attempts, failed_today = study_states.get(
                definition.ability_id, (0, False)
            )
            study_progress = min(0.90, failed_attempts * 0.10)
            if failed_today:
                raise ValueError(
                    f"今天已经研读过《{definition.name}》；魔法书完好，"
                    f"研读进度为{study_progress:.0%}。请在下个04:00日界线后继续"
                )
            seed = int(
                int(random_seed) & ((1 << 62) - 1)
                if random_seed is not None
                else int.from_bytes(
                    hashlib.blake2b(
                        f"{int(book['random_seed'])}:{failed_attempts}".encode(
                            "ascii"
                        ),
                        digest_size=8,
                    ).digest(),
                    "big",
                ) & ((1 << 62) - 1)
            )

            reading_power, chance = self._reading_metrics(
                definition,
                profile,
                study_progress,
                reading_bonus=reading_bonus,
                reading_power_bonus=reading_power_bonus,
            )
            success = random.Random(seed).random() < chance
            before = existing.potential if existing else 0
            potential_gain = 0
            research_pages_gain = 0
            research_pages_balance = 0
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
                silent_reading = profile["effective_levels"].get(
                    "silent_reading", 0
                )
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
                # A duplicate should advance both mastery and collection.
                # One annotated page is deliberately much smaller than the
                # full-book recycle at 400 potential, but makes the research
                # loop visible months before a spell is capped.
                research_pages_gain = 1
                _, research_pages_balance = (
                    await self._change_research_pages_in_db(
                        db,
                        user_pk=int(user.id),
                        spell_id=definition.ability_id,
                        delta=research_pages_gain,
                        reason="successful_duplicate_annotation",
                        operation_key=(
                            f"spell-research-annotation-v1:{user.id}:"
                            f"{int(book['id'])}:{int(book['quantity'])}"
                        ),
                        source_book_id=int(book["id"]),
                        source_seed=int(book["random_seed"]),
                    )
                )

            if success:
                await self._consume_book_copy_in_db(db, book)
            after = result_spell.potential if result_spell else before
            await db.execute(
                """
                INSERT INTO spell_read_logs (
                    user_pk, spell_id, book_item_id, success,
                    success_chance, random_seed, potential_before,
                    potential_after, reading_difficulty, reading_power,
                    reading_attribute, activity_day_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    definition.ability_id,
                    int(book["id"]),
                    int(success),
                    chance,
                    seed,
                    before,
                    after,
                    definition.reading_difficulty,
                    reading_power,
                    definition.reading_attribute,
                    activity_day_key,
                    utc_now_text(),
                ),
            )
            if self.skill_service:
                raw = {"reading": 3 if success else 1}
                if success and existing:
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
                int(success),
                potential_gain,
                reading_power,
                definition.reading_difficulty,
                definition.reading_attribute,
                not success,
                study_progress + (0.10 if not success else 0.0),
                (
                    "learned" if success and existing is None
                    else "potential_restored" if success
                    else "study_progress"
                ),
                research_pages_gain,
                research_pages_balance,
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
                potential = decay_spell_potential(potential)
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
