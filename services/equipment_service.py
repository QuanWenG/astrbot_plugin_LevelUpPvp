import json

try:
    from ..models.equipment import EQUIPMENT_SLOTS, EquipmentItem
    from .db import connect_db
    from .equipment_catalog import STARTER_TEMPLATES, starter_item
    from .user_service import utc_now_text
except ImportError:
    from models.equipment import EQUIPMENT_SLOTS, EquipmentItem
    from services.db import connect_db
    from services.equipment_catalog import STARTER_TEMPLATES, starter_item
    from services.user_service import utc_now_text


DEFAULT_EQUIPPED_TEMPLATES = {
    "main_hand": "training_longsword", "off_hand": "training_shield",
    "head": "training_cap", "neck": "training_amulet",
    "back": "training_cape", "body": "training_clothes",
    "wrist": "training_gloves", "left_finger": "training_ring_left",
    "right_finger": "training_ring_right", "waist": "training_belt",
    "feet": "training_boots",
}


class EquipmentService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def ensure_starter_in_db(self, db, user_pk: int) -> None:
        cursor = await db.execute(
            "SELECT 1 FROM feature_grants WHERE user_pk = ? AND grant_key = ?",
            (user_pk, "starter-armory-v1"),
        )
        granted = await cursor.fetchone()
        await cursor.close()
        if not granted:
            created: dict[str, int] = {}
            for template in STARTER_TEMPLATES:
                item = starter_item(user_pk, template)
                cursor = await db.execute(
                    """
                    INSERT INTO equipment_items (
                        owner_pk, template_id, name, item_type, equip_slot, hand_mode,
                        weapon_type, armor_type, item_level, quality, star_type,
                        material, blessing_state, enhancement_level, weight,
                        enchant_capacity, used_capacity, base_stats_json,
                        inherent_affixes_json, random_affixes_json,
                        fusion_affixes_json, bound, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._insert_values(item),
                )
                await cursor.close()
                cursor = await db.execute("SELECT last_insert_rowid() AS id")
                row = await cursor.fetchone()
                await cursor.close()
                created[template.template_id] = int(row["id"])
            for slot, template_id in DEFAULT_EQUIPPED_TEMPLATES.items():
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
            template.template_id: template for template in STARTER_TEMPLATES
        }
        for template_id, template in template_by_id.items():
            if template_id in existing:
                await db.execute(
                    "UPDATE equipment_items SET weight = ? "
                    "WHERE owner_pk = ? AND template_id = ? "
                    "AND item_level = 0 AND quality = 'common' "
                    "AND star_type = 'none' AND enhancement_level = 0 "
                    "AND enchant_capacity = 0 AND used_capacity = 0",
                    (template.weight, user_pk, template_id),
                )
                continue
            if template_id not in {"training_cape", "training_gloves"}:
                continue
            item = starter_item(user_pk, template)
            cursor = await db.execute(
                """
                INSERT INTO equipment_items (
                    owner_pk, template_id, name, item_type, equip_slot, hand_mode,
                    weapon_type, armor_type, item_level, quality, star_type,
                    material, blessing_state, enhancement_level, weight,
                    enchant_capacity, used_capacity, base_stats_json,
                    inherent_affixes_json, random_affixes_json,
                    fusion_affixes_json, bound, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._insert_values(item),
            )
            await cursor.close()
            cursor = await db.execute("SELECT last_insert_rowid() AS id")
            row = await cursor.fetchone()
            await cursor.close()
            existing[template_id] = int(row["id"])
        for slot, template_id in (
            ("back", "training_cape"),
            ("wrist", "training_gloves"),
        ):
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
    async def list_items(self, user_pk: int) -> list[EquipmentItem]:
        async with await connect_db(self.db_path) as db:
            await self.ensure_starter_in_db(db, user_pk)
            items = await self.list_items_in_db(db, user_pk)
            await db.commit()
            return items

    async def list_items_in_db(self, db, user_pk: int) -> list[EquipmentItem]:
        cursor = await db.execute(
            "SELECT * FROM equipment_items WHERE owner_pk = ? ORDER BY id", (user_pk,)
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
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            await self.ensure_starter_in_db(db, user_pk)
            item = await self._get_owned_item_in_db(db, user_pk, equipment_id)
            slots = self._target_slots(item, requested_slot)
            if item.hand_mode in {"two_hand_heavy", "two_hand_melee", "two_hand_ranged"}:
                await db.execute(
                    "DELETE FROM equipment_loadout WHERE user_pk = ? AND slot IN ('main_hand', 'off_hand')",
                    (user_pk,),
                )
            elif any(slot in {"main_hand", "off_hand"} for slot in slots):
                await db.execute(
                    "DELETE FROM equipment_loadout WHERE user_pk = ? AND slot IN ('main_hand', 'off_hand') AND equipment_id IN (SELECT equipment_id FROM equipment_loadout WHERE user_pk = ? GROUP BY equipment_id HAVING COUNT(*) > 1)",
                    (user_pk, user_pk),
                )
            await db.execute(
                "DELETE FROM equipment_loadout WHERE user_pk = ? AND equipment_id = ?",
                (user_pk, equipment_id),
            )
            for slot in slots:
                await db.execute(
                    "INSERT OR REPLACE INTO equipment_loadout (user_pk, slot, equipment_id) VALUES (?, ?, ?)",
                    (user_pk, slot, equipment_id),
                )
            await db.commit()
            return item, slots

    async def unequip(self, user_pk: int, slot: str) -> None:
        if slot not in EQUIPMENT_SLOTS:
            raise ValueError("未知装备槽")
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT equipment_id FROM equipment_loadout WHERE user_pk = ? AND slot = ?",
                (user_pk, slot),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if not row:
                raise ValueError("该位置没有装备")
            await db.execute(
                "DELETE FROM equipment_loadout WHERE user_pk = ? AND equipment_id = ?",
                (user_pk, row["equipment_id"]),
            )
            await db.commit()

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
            1 if item.bound else 0, utc_now_text(),
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
        )
