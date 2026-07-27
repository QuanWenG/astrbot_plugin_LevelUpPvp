import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from models.user import UserIdentity
from services.build_service import CombatBuildService
from services.db import connect_db, init_db
from services.equipment_catalog import (
    DEFAULT_CATALOG_PATH,
    EquipmentCatalog,
    EquipmentFactory,
    load_equipment_catalog,
)
from services.equipment_service import EquipmentService
from services.user_service import UserService


class EquipmentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "equipment_catalog.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _load(self, raw=None):
        self.path.write_text(
            json.dumps(raw or self.raw, ensure_ascii=False),
            encoding="utf-8",
        )
        return load_equipment_catalog(self.path)

    def test_all_training_items_and_turtle_necklace_are_data_driven(self):
        snapshot = self._load()
        self.assertEqual(snapshot.schema_version, 2)
        self.assertEqual(len(snapshot.entries), 151)
        self.assertEqual(len(snapshot.starter_entries), 18)
        self.assertEqual(
            sum(entry.mode == "generated" for entry in snapshot.entries),
            112,
        )
        self.assertEqual(
            sum(
                entry.fixed.get("star_type") == "black_star"
                for entry in snapshot.entries
            ),
            21,
        )
        self.assertEqual(
            {entry.catalog_id for entry in snapshot.entries if 3001 <= entry.catalog_id <= 3112},
            set(range(3001, 3113)),
        )
        self.assertEqual(
            {entry.catalog_id for entry in snapshot.entries if 4001 <= entry.catalog_id <= 4020},
            set(range(4001, 4021)),
        )
        self.assertEqual(
            {
                entry.fixed["item_level"]
                for entry in snapshot.entries
                if 4001 <= entry.catalog_id <= 4020
            },
            {40},
        )
        longsword = snapshot.by_id[1001]
        item = EquipmentFactory().create_from_catalog(7, longsword, seed=99)
        self.assertEqual(item.template_id, "training_longsword")
        self.assertEqual(item.owner_pk, 7)
        self.assertEqual(item.item_level, 0)
        self.assertEqual(item.quality, "common")
        self.assertEqual(item.base_stats, {"weapon_power": 2})
        self.assertEqual(longsword.starter_equip_slots, ("main_hand",))

    def test_precious_turtle_necklace_is_fixed_black_star(self):
        entry = self._load().by_id[2001]
        item = EquipmentFactory().create_from_catalog(7, entry, seed=99)

        self.assertEqual(item.template_id, "precious_turtle_necklace")
        self.assertEqual(item.name, "珍贵的龟龟项链")
        self.assertEqual(item.item_type, "accessory")
        self.assertEqual(item.equip_slot, "neck")
        self.assertEqual(item.item_level, 1)
        self.assertEqual(item.quality, "legendary")
        self.assertEqual(item.star_type, "black_star")
        self.assertEqual(item.material, "emerald")
        self.assertEqual(item.enchant_capacity, 0)
        self.assertEqual(item.used_capacity, 0)
        self.assertEqual(
            item.description,
            "某个笨蛋丢失了大家最宝贵的东西，"
            "被大家揍了一顿之后爆出来的珍贵项链",
        )
        self.assertEqual(
            item.inherent_affixes,
            (
                {
                    "type": "advanced_stat",
                    "stat": "life_growth",
                    "value": 10,
                    "capacity": 0,
                },
                {
                    "type": "advanced_stat",
                    "stat": "mana_growth",
                    "value": 10,
                    "capacity": 0,
                },
                {
                    "type": "advanced_stat",
                    "stat": "speed",
                    "value": 5,
                    "capacity": 0,
                },
                {
                    "type": "advanced_stat",
                    "stat": "luck",
                    "value": 5,
                    "capacity": 0,
                },
            ),
        )

    def test_generated_items_stay_inside_level_and_quality_ranges(self):
        generated = copy.deepcopy(self.raw["items"][0])
        generated.update(
            id=9001,
            template_id="generated_test_sword",
            name="随机测试剑",
            mode="generated",
            starter_grant=False,
            starter_equip_slots=[],
        )
        generated.pop("fixed")
        generated["generation"] = {
            "level_min": 12,
            "level_max": 15,
            "qualities": [
                {"quality": "excellent", "weight": 1},
                {"quality": "rare", "weight": 2},
            ],
        }
        snapshot = self._load({"schema_version": 1, "items": [generated]})
        entry = snapshot.by_id[9001]
        factory = EquipmentFactory()
        items = [
            factory.create_from_catalog(owner_pk=index, entry=entry, seed=index)
            for index in range(40)
        ]
        self.assertTrue(all(12 <= item.item_level <= 15 for item in items))
        self.assertTrue(
            all(item.quality in {"excellent", "rare"} for item in items)
        )
        self.assertGreater(len({item.item_level for item in items}), 1)

    def test_v2_generated_materials_are_weighted_and_reproducible(self):
        snapshot = self._load()
        entry = snapshot.by_id[3001]
        factory = EquipmentFactory()
        first = factory.create_from_catalog(1, entry, seed=12345)
        replay = factory.create_from_catalog(1, entry, seed=12345)
        materials = {item["material"] for item in entry.generation["materials"]}

        self.assertEqual(first, replay)
        self.assertIn(first.material, materials)
        self.assertGreater(
            len(
                {
                    factory.create_from_catalog(1, entry, seed=seed).material
                    for seed in range(40)
                }
            ),
            1,
        )

    def test_invalid_catalog_fields_are_rejected(self):
        cases = {}

        duplicate_id = copy.deepcopy(self.raw)
        duplicate_id["items"][1]["id"] = duplicate_id["items"][0]["id"]
        cases["重复ID"] = duplicate_id

        duplicate_template = copy.deepcopy(self.raw)
        duplicate_template["items"][1]["template_id"] = (
            duplicate_template["items"][0]["template_id"]
        )
        cases["重复模板"] = duplicate_template

        unknown_material = copy.deepcopy(self.raw)
        unknown_material["items"][0]["material"] = "not-a-material"
        cases["未知材质"] = unknown_material

        invalid_affix = copy.deepcopy(self.raw)
        invalid_affix["items"][0]["inherent_affixes"] = [
            {"type": "not-an-affix", "value": 1, "capacity": 0}
        ]
        cases["非法词条"] = invalid_affix

        invalid_capacity = copy.deepcopy(self.raw)
        invalid_capacity["items"][0]["fixed"]["used_capacity"] = 1
        cases["容量错误"] = invalid_capacity

        invalid_description = copy.deepcopy(self.raw)
        invalid_description["items"][0]["description"] = 123
        cases["介绍类型错误"] = invalid_description

        slot_conflict = copy.deepcopy(self.raw)
        slot_conflict["items"][0]["starter_equip_slots"] = ["head"]
        cases["槽位冲突"] = slot_conflict

        generated_legendary = copy.deepcopy(self.raw)
        generated_legendary["items"][0]["mode"] = "generated"
        generated_legendary["items"][0].pop("fixed")
        generated_legendary["items"][0]["starter_grant"] = False
        generated_legendary["items"][0]["starter_equip_slots"] = []
        generated_legendary["items"][0]["generation"] = {
            "level_min": 1,
            "level_max": 2,
            "qualities": [{"quality": "legendary", "weight": 1}],
        }
        cases["随机传奇"] = generated_legendary

        generated_index = next(
            index
            for index, item in enumerate(self.raw["items"])
            if item["id"] == 3001
        )
        for label, materials in {
            "空材质池": [],
            "未知生成材质": [{"material": "not-a-material", "weight": 1}],
            "非法材质权重": [{"material": "iron", "weight": 0}],
            "重复生成材质": [
                {"material": "iron", "weight": 1},
                {"material": "iron", "weight": 2},
            ],
        }.items():
            invalid = copy.deepcopy(self.raw)
            invalid["items"][generated_index]["generation"]["materials"] = materials
            cases[label] = invalid

        for label, raw in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self._load(raw)

    def test_failed_reload_preserves_previous_snapshot(self):
        self._load()
        catalog = EquipmentCatalog(self.path)
        previous = catalog.snapshot
        self.path.write_text('{"schema_version": 1, "items": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            catalog.reload()
        self.assertIs(catalog.snapshot, previous)
        self.assertEqual(len(catalog.snapshot.entries), 151)


class EquipmentGrantServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.first = await self.users.get_or_create_user(
            UserIdentity("qq", "group-a", "one", "One")
        )
        self.second = await self.users.get_or_create_user(
            UserIdentity("qq", "group-a", "two", "Two")
        )
        self.third = await self.users.get_or_create_user(
            UserIdentity("other", "group-b", "three", "Three")
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_grant_is_per_role_and_repeated_grants_are_skipped(self):
        service = EquipmentService(self.db_path, seed_source=lambda: 123)
        first = await service.grant_catalog_item(
            [self.first.id, self.second.id],
            1001,
        )
        repeated = await service.grant_catalog_item(
            [self.first.id, self.second.id],
            1001,
        )
        self.assertEqual((first.granted, first.skipped), (2, 0))
        self.assertEqual((repeated.granted, repeated.skipped), (0, 2))

        # A later starter initialization reuses the granted template instead of
        # creating a duplicate copy.
        items = await service.list_items(self.first.id)
        self.assertEqual(
            sum(item.template_id == "training_longsword" for item in items),
            1,
        )

    async def test_user_scope_queries_keep_group_roles_separate(self):
        group = await self.users.list_user_pks(platform="qq", group_id="group-a")
        all_users = await self.users.list_user_pks()
        self.assertEqual(group, [self.first.id, self.second.id])
        self.assertEqual(
            all_users,
            [self.first.id, self.second.id, self.third.id],
        )

    async def test_turtle_necklace_persists_and_applies_all_effects(self):
        service = EquipmentService(self.db_path, seed_source=lambda: 123)
        result = await service.grant_catalog_item([self.first.id], 2001)
        self.assertEqual((result.granted, result.skipped), (1, 0))

        items = await service.list_items(self.first.id)
        necklace = next(
            item
            for item in items
            if item.template_id == "precious_turtle_necklace"
        )
        self.assertIn("某个笨蛋", necklace.description)
        await service.equip(self.first.id, necklace.id)
        slots, equipped = await service.get_loadout(self.first.id)
        build = CombatBuildService(service, None).resolve_equipment(
            self.first,
            slots,
            equipped,
            {},
        )

        self.assertEqual(
            build.advanced_stat_modifiers,
            {
                "life_growth": 10,
                "mana_growth": 10,
                "speed": 5,
                "luck": 5,
            },
        )
        self.assertEqual(build.combat_effects["resistance_mind"], 50)

    async def test_equipment_schema_contains_description_column(self):
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute("PRAGMA table_info(equipment_items)")
            columns = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
        self.assertIn("description", columns)
        self.assertIn("source_effects_json", columns)

    async def test_legacy_equipment_table_gains_description_column(self):
        handle, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """
            CREATE TABLE equipment_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_pk INTEGER NOT NULL,
                template_id TEXT NOT NULL,
                name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                equip_slot TEXT NOT NULL,
                hand_mode TEXT NOT NULL DEFAULT 'none',
                weapon_type TEXT NOT NULL DEFAULT '',
                armor_type TEXT NOT NULL DEFAULT '',
                item_level INTEGER NOT NULL DEFAULT 0,
                quality TEXT NOT NULL DEFAULT 'common',
                star_type TEXT NOT NULL DEFAULT 'none',
                material TEXT NOT NULL DEFAULT 'iron',
                blessing_state TEXT NOT NULL DEFAULT 'normal',
                enhancement_level INTEGER NOT NULL DEFAULT 0,
                weight REAL NOT NULL DEFAULT 0,
                enchant_capacity INTEGER NOT NULL DEFAULT 0,
                used_capacity INTEGER NOT NULL DEFAULT 0,
                base_stats_json TEXT NOT NULL DEFAULT '{}',
                inherent_affixes_json TEXT NOT NULL DEFAULT '[]',
                random_affixes_json TEXT NOT NULL DEFAULT '[]',
                fusion_affixes_json TEXT NOT NULL DEFAULT '[]',
                bound INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        connection.close()
        try:
            await init_db(legacy_path)
            async with await connect_db(legacy_path) as db:
                cursor = await db.execute("PRAGMA table_info(equipment_items)")
                columns = {row["name"] for row in await cursor.fetchall()}
                await cursor.close()
            self.assertIn("description", columns)
            self.assertIn("source_effects_json", columns)
        finally:
            os.remove(legacy_path)

    async def test_generation_failure_rolls_back_the_whole_batch(self):
        service = EquipmentService(self.db_path, seed_source=lambda: 123)
        original = service.factory.create_from_catalog
        call_count = 0

        def fail_second(owner_pk, entry, seed):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("synthetic generation failure")
            return original(owner_pk, entry, seed)

        service.factory.create_from_catalog = fail_second
        with self.assertRaises(RuntimeError):
            await service.grant_catalog_item(
                [self.first.id, self.second.id],
                1001,
            )

        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS amount FROM equipment_items "
                "WHERE template_id = 'training_longsword'"
            )
            row = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(int(row["amount"]), 0)
