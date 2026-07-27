import json
import random
import secrets
from dataclasses import dataclass

try:
    from ..models.equipment import EQUIPMENT_SLOTS, EquipmentItem
    from .db import connect_db
    from .equipment_catalog import (
        DEFAULT_EQUIPMENT_CATALOG,
        EquipmentCatalog,
        EquipmentFactory,
    )
    from .equipment_catalog import EquipmentCatalogEntry
    from .user_service import utc_now_text
except ImportError:
    from models.equipment import EQUIPMENT_SLOTS, EquipmentItem
    from services.db import connect_db
    from services.equipment_catalog import (
        DEFAULT_EQUIPMENT_CATALOG,
        EquipmentCatalog,
        EquipmentFactory,
    )
    from services.equipment_catalog import EquipmentCatalogEntry
    from services.user_service import utc_now_text


@dataclass(frozen=True)
class EquipmentGrantResult:
    catalog_id: int
    equipment_name: str
    granted: int
    skipped: int


class EquipmentService:
    def __init__(
        self,
        db_path: str,
        catalog: EquipmentCatalog | None = None,
        seed_source=None,
    ):
        self.db_path = db_path
        self.catalog = catalog or DEFAULT_EQUIPMENT_CATALOG
        self.factory = EquipmentFactory()
        self._seed_source = seed_source or (lambda: secrets.randbits(63))

    async def ensure_starter_in_db(self, db, user_pk: int) -> None:
        cursor = await db.execute(
            "SELECT 1 FROM feature_grants WHERE user_pk = ? AND grant_key = ?",
            (user_pk, "starter-armory-v1"),
        )
        granted = await cursor.fetchone()
        await cursor.close()
        if not granted:
            cursor = await db.execute(
                "SELECT id, template_id FROM equipment_items WHERE owner_pk = ?",
                (user_pk,),
            )
            created = {
                row["template_id"]: int(row["id"])
                for row in await cursor.fetchall()
            }
            await cursor.close()
            for entry in self.catalog.snapshot.starter_entries:
                template_id = entry.template.template_id
                if template_id not in created:
                    item = self.factory.create_from_catalog(
                        user_pk,
                        entry,
                        seed=self._seed_source(),
                    )
                    created[template_id] = await self._insert_item_in_db(
                        db, item
                    )
                for slot in entry.starter_equip_slots:
                    await db.execute(
                        "INSERT OR REPLACE INTO equipment_loadout "
                        "(user_pk, slot, equipment_id) VALUES (?, ?, ?)",
                        (user_pk, slot, created[template_id]),
                    )
            await db.execute(
                "INSERT INTO feature_grants (user_pk, grant_key, created_at) "
                "VALUES (?, ?, ?)",
                (user_pk, "starter-armory-v1", utc_now_text()),
            )
        await self._ensure_material_armory_v2_in_db(db, user_pk)

    async def _ensure_material_armory_v2_in_db(self, db, user_pk: int) -> None:
        grant_key = "starter-armory-v2-materials"
        cursor = await db.execute(
            "SELECT 1 FROM feature_grants WHERE user_pk = ? AND grant_key = ?",
            (user_pk, grant_key),
        )
        if await cursor.fetchone():
            await cursor.close()
            return
        await cursor.close()
        cursor = await db.execute(
            "SELECT id, template_id FROM equipment_items WHERE owner_pk = ?",
            (user_pk,),
        )
        existing = {
            row["template_id"]: int(row["id"])
            for row in await cursor.fetchall()
        }
        await cursor.close()
        template_by_id = {
            entry.template.template_id: entry
            for entry in self.catalog.snapshot.starter_entries
        }
        for template_id, entry in template_by_id.items():
            if template_id in existing:
                await db.execute(
                    "UPDATE equipment_items SET weight = ? "
                    "WHERE owner_pk = ? AND template_id = ? "
                    "AND item_level = 0 AND quality = 'common' "
                    "AND star_type = 'none' AND enhancement_level = 0 "
                    "AND enchant_capacity = 0 AND used_capacity = 0",
                    (entry.template.weight, user_pk, template_id),
                )
                continue
            if template_id not in {"training_cape", "training_gloves"}:
                continue
            item = self.factory.create_from_catalog(
                user_pk,
                entry,
                seed=self._seed_source(),
            )
            existing[template_id] = await self._insert_item_in_db(db, item)
        for slot, template_id in (
            ("back", "training_cape"),
            ("wrist", "training_gloves"),
        ):
            if template_id not in existing:
                continue
            await db.execute(
                "INSERT OR IGNORE INTO equipment_loadout "
                "(user_pk, slot, equipment_id) VALUES (?, ?, ?)",
                (user_pk, slot, existing[template_id]),
            )
        await db.execute(
            "INSERT INTO feature_grants (user_pk, grant_key, created_at) "
            "VALUES (?, ?, ?)",
            (user_pk, grant_key, utc_now_text()),
        )

    async def grant_catalog_item(
        self,
        user_pks: list[int] | tuple[int, ...],
        catalog_id: int,
    ) -> EquipmentGrantResult:
        entry = self.catalog.get(catalog_id)
        unique_user_pks = tuple(dict.fromkeys(int(pk) for pk in user_pks))
        granted = 0
        skipped = 0
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                for user_pk in unique_user_pks:
                    cursor = await db.execute(
                        "SELECT 1 FROM equipment_items "
                        "WHERE owner_pk = ? AND template_id = ? LIMIT 1",
                        (user_pk, entry.template.template_id),
                    )
                    exists = await cursor.fetchone()
                    await cursor.close()
                    if exists:
                        skipped += 1
                        continue
                    item = self.factory.create_from_catalog(
                        user_pk,
                        entry,
                        seed=self._seed_source(),
                    )
                    await self._insert_item_in_db(db, item)
                    granted += 1
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return EquipmentGrantResult(
            catalog_id=entry.catalog_id,
            equipment_name=entry.template.name,
            granted=granted,
            skipped=skipped,
        )

    def generate_reward(
        self,
        owner_pk: int,
        catalog_id_min: int,
        catalog_id_max: int,
        level_min: int,
        level_max: int,
        seed: int | None = None,
    ) -> EquipmentItem:
        """Generate a single random equipment reward within a catalog-ID range.

        The item_level is forced into [level_min, level_max], overriding the
        catalog's own generation range so dungeon rewards match the dungeon's
        intended tier.
        """
        candidates = tuple(
            entry
            for entry in self.catalog.snapshot.entries
            if (
                catalog_id_min <= entry.catalog_id < catalog_id_max
                and entry.mode == "generated"
            )
        )
        if not candidates:
            raise ValueError(
                f"装备目录中不存在ID范围[{catalog_id_min},{catalog_id_max})的可生成装备"
            )
        rng = random.Random(seed if seed is not None else self._seed_source())
        entry: EquipmentCatalogEntry = rng.choice(candidates)
        generation = entry.generation
        item_level = rng.randint(
            max(0, int(level_min)),
            max(int(level_min), int(level_max)),
        )
        qualities = generation.get("qualities", ())
        if qualities:
            quality = rng.choices(
                [item["quality"] for item in qualities],
                weights=[float(item["weight"]) for item in qualities],
                k=1,
            )[0]
        else:
            quality = "common"
        materials = generation.get("materials", ())
        if materials:
            material = rng.choices(
                [item["material"] for item in materials],
                weights=[float(item["weight"]) for item in materials],
                k=1,
            )[0]
        else:
            material = entry.template.material
        from dataclasses import replace as _replace
        template = _replace(entry.template, material=material)
        item = self.factory.generate(
            owner_pk,
            template,
            item_level,
            quality,
            rng.getrandbits(63),
        )
        return _replace(item, bound=entry.bound)

    async def generate_reward_in_db(
        self,
        db,
        owner_pk: int,
        catalog_id_min: int,
        catalog_id_max: int,
        level_min: int,
        level_max: int,
        seed: int | None = None,
    ) -> EquipmentItem:
        """Generate a reward and persist it within an existing transaction."""
        item = self.generate_reward(
            owner_pk,
            catalog_id_min,
            catalog_id_max,
            level_min,
            level_max,
            seed,
        )
        await self._insert_item_in_db(db, item)
        return item

    async def _insert_item_in_db(self, db, item: EquipmentItem) -> int:
        cursor = await db.execute(
            """
            INSERT INTO equipment_items (
                owner_pk, template_id, name, item_type, equip_slot, hand_mode,
                weapon_type, armor_type, item_level, quality, star_type,
                material, blessing_state, enhancement_level, weight,
                enchant_capacity, used_capacity, base_stats_json,
                inherent_affixes_json, random_affixes_json,
                fusion_affixes_json, bound, description, source_effects_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._insert_values(item),
        )
        await cursor.close()
        cursor = await db.execute("SELECT last_insert_rowid() AS id")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["id"])
    async def list_items(self, user_pk: int) -> list[EquipmentItem]:
        async with await connect_db(self.db_path) as db:
            await self.ensure_starter_in_db(db, user_pk)
            items = await self.list_items_in_db(db, user_pk)
            await db.commit()
            return items

    async def list_items_in_db(self, db, user_pk: int) -> list[EquipmentItem]:
        cursor = await db.execute(
            """
            SELECT * FROM equipment_items
            WHERE owner_pk = ?
            ORDER BY CASE quality
                WHEN 'legendary' THEN 6
                WHEN 'mythic' THEN 5
                WHEN 'epic' THEN 4
                WHEN 'rare' THEN 3
                WHEN 'excellent' THEN 2
                WHEN 'common' THEN 1
                ELSE 0
            END DESC, item_level DESC, id ASC
            """,
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_item(row) for row in rows]

    async def loadout_in_db(self, db, user_pk: int) -> tuple[dict[str, int], list[EquipmentItem]]:
        await self.ensure_starter_in_db(db, user_pk)
        cursor = await db.execute(
            """
            SELECT l.slot AS equipped_slot, i.*
            FROM equipment_loadout l JOIN equipment_items i ON i.id = l.equipment_id
            WHERE l.user_pk = ? ORDER BY l.slot
            """,
            (user_pk,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        slots = {row["equipped_slot"]: int(row["id"]) for row in rows}
        unique = {}
        for row in rows:
            unique[int(row["id"])] = self._row_to_item(row)
        return slots, list(unique.values())

    async def get_loadout(self, user_pk: int):
        async with await connect_db(self.db_path) as db:
            result = await self.loadout_in_db(db, user_pk)
            await db.commit()
            return result

    async def equip(self, user_pk: int, equipment_id: int, requested_slot: str = ""):
        results = await self.equip_many(
            user_pk,
            ((equipment_id, requested_slot),),
        )
        return results[0]

    async def equip_many(
        self,
        user_pk: int,
        assignments: tuple[tuple[int, str], ...] | list[tuple[int, str]],
    ) -> list[tuple[EquipmentItem, tuple[str, ...]]]:
        if not assignments:
            raise ValueError("请指定要穿戴的装备")
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                await self.ensure_starter_in_db(db, user_pk)
                resolved = []
                for equipment_id, requested_slot in assignments:
                    item = await self._get_owned_item_in_db(
                        db, user_pk, int(equipment_id)
                    )
                    slots = self._target_slots(item, requested_slot)
                    resolved.append((item, slots))
                for item, slots in resolved:
                    await self._equip_in_db(db, user_pk, item, slots)
                await db.commit()
                return resolved
            except Exception:
                await db.rollback()
                raise


    async def auto_equip(
        self,
        user_pk: int,
        assignments: tuple[tuple[int, str], ...] | list[tuple[int, str]],
    ) -> list[tuple[EquipmentItem, tuple[str, ...]]]:
        """Atomically replace the entire loadout with the given assignments."""
        if not assignments:
            raise ValueError("没有可穿戴的装备")
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                await self.ensure_starter_in_db(db, user_pk)
                await db.execute(
                    "DELETE FROM equipment_loadout WHERE user_pk = ?",
                    (user_pk,),
                )
                resolved = []
                for equipment_id, requested_slot in assignments:
                    item = await self._get_owned_item_in_db(
                        db, user_pk, int(equipment_id)
                    )
                    slots = self._target_slots(item, requested_slot)
                    resolved.append((item, slots))
                for item, slots in resolved:
                    await self._equip_in_db(db, user_pk, item, slots)
                await db.commit()
                return resolved
            except Exception:
                await db.rollback()
                raise

    async def _equip_in_db(
        self,
        db,
        user_pk: int,
        item: EquipmentItem,
        slots: tuple[str, ...],
    ) -> None:
        if item.hand_mode in {"two_hand_heavy", "two_hand_melee", "two_hand_ranged"}:
            await db.execute(
                "DELETE FROM equipment_loadout "
                "WHERE user_pk = ? AND slot IN ('main_hand', 'off_hand')",
                (user_pk,),
            )
        elif any(slot in {"main_hand", "off_hand"} for slot in slots):
            await db.execute(
                "DELETE FROM equipment_loadout WHERE user_pk = ? "
                "AND slot IN ('main_hand', 'off_hand') "
                "AND equipment_id IN ("
                "SELECT equipment_id FROM equipment_loadout "
                "WHERE user_pk = ? GROUP BY equipment_id HAVING COUNT(*) > 1)",
                (user_pk, user_pk),
            )
        await db.execute(
            "DELETE FROM equipment_loadout "
            "WHERE user_pk = ? AND equipment_id = ?",
            (user_pk, item.id),
        )
        for slot in slots:
            await db.execute(
                "INSERT OR REPLACE INTO equipment_loadout "
                "(user_pk, slot, equipment_id) VALUES (?, ?, ?)",
                (user_pk, slot, item.id),
            )

    async def unequip(self, user_pk: int, slot: str) -> None:
        await self.unequip_many(user_pk, (slot,))

    async def unequip_many(
        self,
        user_pk: int,
        slots: tuple[str, ...] | list[str],
    ) -> int:
        if not slots:
            raise ValueError("请指定要卸下的装备槽")
        if any(slot not in EQUIPMENT_SLOTS for slot in slots):
            raise ValueError("未知装备槽")
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                placeholders = ",".join("?" for _ in slots)
                cursor = await db.execute(
                    "SELECT DISTINCT equipment_id FROM equipment_loadout "
                    f"WHERE user_pk = ? AND slot IN ({placeholders})",
                    (user_pk, *slots),
                )
                equipment_ids = [
                    int(row["equipment_id"]) for row in await cursor.fetchall()
                ]
                await cursor.close()
                if not equipment_ids:
                    raise ValueError("指定位置没有装备")
                id_placeholders = ",".join("?" for _ in equipment_ids)
                await db.execute(
                    "DELETE FROM equipment_loadout WHERE user_pk = ? "
                    f"AND equipment_id IN ({id_placeholders})",
                    (user_pk, *equipment_ids),
                )
                await db.commit()
                return len(equipment_ids)
            except Exception:
                await db.rollback()
                raise

    async def unequip_all(self, user_pk: int) -> int:
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT equipment_id) AS count "
                "FROM equipment_loadout WHERE user_pk = ?",
                (user_pk,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            count = int(row["count"])
            await db.execute(
                "DELETE FROM equipment_loadout WHERE user_pk = ?",
                (user_pk,),
            )
            await db.commit()
            return count

    async def item_detail(self, user_pk: int, equipment_id: int) -> EquipmentItem:
        async with await connect_db(self.db_path) as db:
            return await self._get_owned_item_in_db(db, user_pk, equipment_id)

    async def _get_owned_item_in_db(self, db, user_pk, equipment_id):
        cursor = await db.execute(
            "SELECT * FROM equipment_items WHERE id = ? AND owner_pk = ?",
            (equipment_id, user_pk),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError("装备不存在或不属于你")
        return self._row_to_item(row)

    def _target_slots(self, item: EquipmentItem, requested: str) -> tuple[str, ...]:
        if item.hand_mode in {"two_hand_heavy", "two_hand_melee", "two_hand_ranged"}:
            return ("main_hand", "off_hand")
        if item.hand_mode == "shield":
            return ("off_hand",)
        if item.item_type == "weapon":
            slot = requested or item.equip_slot
            if slot not in {"main_hand", "off_hand"}:
                raise ValueError("单手武器只能穿戴到主手或副手")
            return (slot,)
        if item.equip_slot in {"left_finger", "right_finger"}:
            slot = requested or item.equip_slot
            if slot not in {"left_finger", "right_finger"}:
                raise ValueError("戒指只能穿戴到左指或右指")
            return (slot,)
        return (item.equip_slot,)

    def _insert_values(self, item: EquipmentItem) -> tuple:
        return (
            item.owner_pk, item.template_id, item.name, item.item_type,
            item.equip_slot, item.hand_mode, item.weapon_type, item.armor_type,
            item.item_level, item.quality, item.star_type, item.material,
            item.blessing_state, item.enhancement_level, item.weight,
            item.enchant_capacity, item.used_capacity,
            json.dumps(item.base_stats, ensure_ascii=False),
            json.dumps(item.inherent_affixes, ensure_ascii=False),
            json.dumps(item.random_affixes, ensure_ascii=False),
            json.dumps(item.fusion_affixes, ensure_ascii=False),
            1 if item.bound else 0, item.description,
            json.dumps(item.source_effects, ensure_ascii=False),
            utc_now_text(),
        )

    def _row_to_item(self, row) -> EquipmentItem:
        return EquipmentItem(
            int(row["id"]), int(row["owner_pk"]), row["template_id"], row["name"],
            row["item_type"], row["equip_slot"], row["hand_mode"], row["weapon_type"],
            row["armor_type"], int(row["item_level"]), row["quality"], row["star_type"],
            row["material"], row["blessing_state"], int(row["enhancement_level"]),
            float(row["weight"]), int(row["enchant_capacity"]), int(row["used_capacity"]),
            json.loads(row["base_stats_json"] or "{}"),
            tuple(json.loads(row["inherent_affixes_json"] or "[]")),
            tuple(json.loads(row["random_affixes_json"] or "[]")),
            tuple(json.loads(row["fusion_affixes_json"] or "[]")), bool(row["bound"]),
            row["description"] or "",
            tuple(json.loads(row["source_effects_json"] or "[]")),
        )
