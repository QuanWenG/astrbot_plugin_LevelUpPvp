import copy
import json
import tempfile
import unittest
from pathlib import Path

from models.combat import AIProfile, FighterSnapshot
from models.monster import MonsterSpawnSpec
from services.combat_engine import SideviewCombatEngine
from services.monster_build_service import MonsterBuildService
from services.monster_catalog import (
    DEFAULT_MONSTER_CATALOG_PATH,
    MonsterCatalog,
)


class MonsterCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = MonsterCatalog()
        cls.payload = json.loads(
            DEFAULT_MONSTER_CATALOG_PATH.read_text(encoding="utf-8")
        )

    def _write(self, payload) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "monsters.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def test_default_catalog_has_exact_mobile_range(self):
        snapshot = self.catalog.snapshot
        self.assertEqual(len(snapshot.monsters), 215)
        self.assertEqual(
            [item.catalog_id for item in snapshot.monsters],
            list(range(1, 216)),
        )
        self.assertEqual(len(snapshot.by_id), 215)
        self.assertEqual(len(snapshot.by_template_id), 215)
        self.assertEqual(snapshot.by_id[1].name, "普奇")

    def test_provenance_is_explicit_and_source_stats_are_separate(self):
        putit = self.catalog.snapshot.by_id[1]
        self.assertEqual(putit.provenance["attribute_origin"], "pc_reference")
        self.assertEqual(
            set(putit.source_stats["attributes"]),
            {
                "strength", "constitution", "dexterity",
                "perception", "magic", "willpower",
            },
        )
        inferred = self.catalog.snapshot.by_id[3]
        self.assertEqual(
            inferred.provenance["attribute_origin"],
            "race_class_inferred",
        )
        self.assertNotIn("attributes", inferred.source_stats)

    def test_every_monster_has_a_chinese_name_without_placeholder(self):
        for monster in self.catalog.snapshot.monsters:
            with self.subTest(template_id=monster.template_id):
                self.assertFalse(monster.name.startswith("直译·"))
                self.assertFalse(
                    any(
                        "\u3040" <= character <= "\u30ff"
                        for character in monster.name
                    )
                )
                self.assertIn(
                    monster.provenance["localization_origin"],
                    {"official", "community", "translated"},
                )
                self.assertTrue(monster.source_name_ja)

    def test_every_race_has_a_chinese_name_and_japanese_source_name(self):
        self.assertEqual(len(self.catalog.snapshot.races), 60)
        for race_id, race in self.catalog.snapshot.races.items():
            with self.subTest(race_id=race_id):
                self.assertTrue(race["name"])
                self.assertFalse(
                    any(
                        "\u3040" <= character <= "\u30ff"
                        for character in race["name"]
                    )
                )
                self.assertTrue(race["source_name_ja"])
                self.assertIn(
                    race["localization_origin"],
                    {"official", "community", "translated"},
                )

    def test_unknown_references_and_bad_values_are_rejected(self):
        mutations = [
            lambda p: p["monsters"][0].update(race_id="missing"),
            lambda p: p["monsters"][0].update(class_id="missing"),
            lambda p: p["monsters"][0]["combat"].update(
                ai_profile_id="missing"
            ),
            lambda p: p["monsters"][0]["skills"].update(
                missing={"coefficient": 1, "flat": 0}
            ),
            lambda p: p["monsters"][0]["abilities"].append(
                {"ability_id": "missing", "min_level": 1, "priority": 1}
            ),
            lambda p: p["monsters"][0].update(base_level=0),
            lambda p: p["monsters"][0]["attribute_weights"].update(
                strength=-1
            ),
            lambda p: p["ranks"]["normal"].update(hp_multiplier=-1),
            lambda p: p["monsters"][1].update(id=1),
            lambda p: p["monsters"][1].update(template_id="monster_001"),
            lambda p: p["monsters"][0]["resistances"].update(
                unknown_element=1
            ),
            lambda p: p["races"]["race_001"].update(name=""),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(self.payload)
                mutation(payload)
                with self.assertRaises((ValueError, KeyError)):
                    MonsterCatalog(self._write(payload))

    def test_failed_reload_preserves_old_snapshot(self):
        catalog = MonsterCatalog()
        previous = catalog.snapshot
        payload = copy.deepcopy(self.payload)
        payload["schema_version"] = 99
        with self.assertRaises(ValueError):
            catalog.reload(self._write(payload))
        self.assertIs(catalog.snapshot, previous)


class MonsterBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = MonsterCatalog()
        cls.service = MonsterBuildService(cls.catalog)

    def test_build_is_deterministic_and_database_free(self):
        spec = MonsterSpawnSpec(
            "monster_001", level=40, rank="elite", combatant_pk=-7001
        )
        first = self.service.build(spec)
        second = self.service.build(spec)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.snapshot.combatant_kind, "monster")
        self.assertEqual(first.snapshot.source_template_id, "monster_001")
        self.assertEqual(first.snapshot.rank, "elite")
        self.assertEqual(first.snapshot.user_pk, -7001)

    def test_level_and_rank_scaling_boundaries(self):
        sums = []
        levels = (1, 20, 40, 100, 280)
        for level in levels:
            build = self.service.build(
                MonsterSpawnSpec("monster_001", level=level)
            )
            values = build.attributes.to_dict().values()
            sums.append(sum(values))
            self.assertTrue(all(1 <= value <= 100 for value in values))
            self.assertTrue(1 <= build.weapon_power <= 18)
            self.assertTrue(0 <= build.armor_power <= 18)
        self.assertEqual(
            sums,
            [round(6 + 1.35 * level) for level in levels],
        )
        self.assertEqual(sums, sorted(sums))

        normal = self.service.build(
            MonsterSpawnSpec("monster_049", level=40, rank="normal")
        )
        elite = self.service.build(
            MonsterSpawnSpec("monster_049", level=40, rank="elite")
        )
        boss = self.service.build(
            MonsterSpawnSpec("monster_049", level=40, rank="boss")
        )
        self.assertLess(normal.snapshot.max_hp, elite.snapshot.max_hp)
        self.assertLess(elite.snapshot.max_hp, boss.snapshot.max_hp)
        self.assertLessEqual(normal.weapon_power, elite.weapon_power)
        self.assertLessEqual(elite.weapon_power, boss.weapon_power)

    def test_representative_races_keep_source_character(self):
        by_name = {
            item.source_name_ja: item
            for item in self.catalog.snapshot.monsters
        }
        putit = self.service.build(
            MonsterSpawnSpec(by_name["プチ"].template_id)
        )
        quickling = self.service.build(
            MonsterSpawnSpec(by_name["クイックリング"].template_id)
        )
        cyclops = self.service.build(
            MonsterSpawnSpec(by_name["サイクロプス"].template_id)
        )
        self.assertEqual(putit.advanced_attributes.life_growth, 80)
        self.assertEqual(quickling.advanced_attributes.speed, 180)
        self.assertEqual(quickling.advanced_attributes.life_growth, 3)
        self.assertGreater(cyclops.attributes.strength, cyclops.attributes.magic)

        spiders = [
            item for item in self.catalog.snapshot.monsters
            if item.race_id == "spider"
        ]
        dragons = [
            item for item in self.catalog.snapshot.monsters
            if item.race_id == "dragon"
        ]
        self.assertTrue(spiders)
        self.assertTrue(dragons)
        spider = self.service.build(
            MonsterSpawnSpec(spiders[0].template_id)
        )
        dragon = self.service.build(
            MonsterSpawnSpec(dragons[0].template_id)
        )
        self.assertIn("web", spider.ability_ids)
        self.assertEqual(
            spider.template.provenance["attribute_origin"],
            "pc_reference",
        )
        self.assertEqual(spider.advanced_attributes.life_growth, 50)
        self.assertGreater(
            spider.attributes.perception, spider.attributes.strength
        )
        self.assertEqual(
            dragon.template.provenance["attribute_origin"],
            "pc_reference",
        )
        self.assertEqual(dragon.advanced_attributes.life_growth, 220)
        self.assertGreater(dragon.snapshot.derived.resistances["fire"], 0)

    def test_all_templates_build_and_simulate(self):
        engine = SideviewCombatEngine()
        baseline = self.service.build(
            MonsterSpawnSpec(
                "monster_001", level=20, combatant_pk=-9999
            )
        )
        for item in self.catalog.snapshot.monsters:
            with self.subTest(template_id=item.template_id):
                build = self.service.build(
                    MonsterSpawnSpec(
                        item.template_id,
                        combatant_pk=-item.catalog_id,
                    )
                )
                result = engine.simulate(
                    build.snapshot,
                    baseline.snapshot,
                    build.ai_profile,
                    baseline.ai_profile,
                    9000 + item.catalog_id,
                )
                self.assertIn(
                    result.winner_pk, {-item.catalog_id, -9999}
                )

    def test_player_snapshot_defaults_remain_compatible(self):
        player = FighterSnapshot(
            user_pk=1,
            name="玩家",
            level=1,
            hp=5,
            atk=5,
            defense=5,
            speed=5,
            luck=5,
            strategy="测试",
        )
        self.assertEqual(player.combatant_kind, "player")
        self.assertEqual(player.source_template_id, "")
        self.assertEqual(player.rank, "normal")
        result = SideviewCombatEngine().simulate(
            player, player, AIProfile(), AIProfile(), 1
        )
        self.assertIn(result.winner_pk, {1})

    def test_invalid_spawn_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.build(MonsterSpawnSpec("monster_001", level=0))
        with self.assertRaises(ValueError):
            self.service.build(
                MonsterSpawnSpec("monster_001", combatant_pk=1)
            )
        with self.assertRaises(ValueError):
            self.service.build(
                MonsterSpawnSpec("monster_001", rank="mythic")
            )
