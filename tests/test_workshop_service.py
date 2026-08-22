import asyncio
import json
import os
import random
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from models.user import UserIdentity
from services.db import connect_db, init_db
from services.equipment_catalog import EquipmentFactory
from services.equipment_service import EquipmentService
from services.user_service import UserService
from services.workshop_service import (
    SEASON_REWORK_MODE,
    SEASON_REWORK_TOKEN_COST,
    WorkshopService,
    affix_matches_direction,
    direction_match_score,
    normalize_rework_direction,
    rework_cost,
    salvage_scrap_value,
)


class WorkshopRuleTests(unittest.TestCase):
    def test_salvage_and_rework_curves_are_quality_and_level_driven(self):
        self.assertGreater(
            salvage_scrap_value("rare", 50),
            salvage_scrap_value("rare", 10),
        )
        self.assertGreater(
            salvage_scrap_value("epic", 10),
            salvage_scrap_value("excellent", 10),
        )
        cost = rework_cost("rare", 35)
        self.assertEqual(cost.total, cost.quality_base + cost.level_surcharge)
        self.assertGreater(rework_cost("epic", 35).total, cost.total)

    def test_direction_aliases_and_capacity_weighted_score_are_transparent(self):
        self.assertEqual(normalize_rework_direction("奇运"), "fortune")
        self.assertEqual(normalize_rework_direction("远程"), "shooting")
        affixes = (
            {"type": "skill_level", "skill_id": "bow", "value": 2, "capacity": 1},
            {"type": "stat_flat", "stat": "strength", "value": 2, "capacity": 3},
        )
        self.assertTrue(affix_matches_direction(affixes[0], "射击"))
        self.assertEqual(direction_match_score(affixes, "射击"), 25)
        with self.assertRaises(ValueError):
            normalize_rework_direction("不存在")


class WorkshopServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.owner = await self.users.get_or_create_user(
            UserIdentity("test", "group", "owner", "Owner")
        )
        self.other = await self.users.get_or_create_user(
            UserIdentity("test", "group", "other", "Other")
        )
        self.equipment = EquipmentService(self.db_path, seed_source=lambda: 777)
        self.workshop = WorkshopService(
            self.db_path,
            self.equipment,
            seed_source=lambda: 12345,
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def _create_item(
        self,
        quality="rare",
        level=30,
        seed=1,
        owner_pk=None,
        template_id="generated",
        **item_changes,
    ):
        entry = next(
            entry
            for entry in self.equipment.catalog.snapshot.entries
            if entry.mode == "generated"
        )
        item = EquipmentFactory().generate(
            owner_pk or self.owner.id,
            entry.template,
            level,
            quality,
            seed,
        )
        if template_id != "generated":
            item = replace(item, template_id=template_id)
        if item_changes:
            item = replace(item, **item_changes)
        async with await connect_db(self.db_path) as db:
            item_id = await self.equipment.insert_item_in_db(db, item)
            await db.commit()
        return replace(item, id=item_id)

    async def _fund_by_salvage(self, amount_at_least=1):
        gained = 0
        index = 0
        while gained < amount_at_least:
            item = await self._create_item(
                quality="mythic",
                level=100,
                seed=900 + index,
                template_id=f"salvage-fund-{index}",
            )
            result = await self.workshop.salvage(self.owner.id, item.id)
            gained = result.balance_after
            index += 1
        return gained

    async def _set_season_tokens(self, amount):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO workshop_wallet (
                    user_pk, scrap_balance, season_tokens, lifetime_earned,
                    lifetime_spent, updated_at_ts
                ) VALUES (?, 0, ?, 0, 0, 0)
                ON CONFLICT(user_pk) DO UPDATE SET
                    season_tokens = excluded.season_tokens
                """,
                (self.owner.id, int(amount)),
            )
            await db.commit()

    async def test_salvage_deletes_item_and_credits_auditable_wallet(self):
        item = await self._create_item(quality="epic", level=40)
        result = await self.workshop.salvage(self.owner.id, item.id)
        wallet = await self.workshop.wallet(self.owner.id)

        self.assertEqual(result.scrap_gained, salvage_scrap_value("epic", 40))
        self.assertEqual(wallet.scrap_balance, result.scrap_gained)
        self.assertEqual(wallet.lifetime_earned, result.scrap_gained)
        self.assertEqual(wallet.lifetime_spent, 0)
        with self.assertRaises(ValueError):
            await self.equipment.item_detail(self.owner.id, item.id)

    async def test_bulk_salvage_requires_an_exact_preview_and_is_atomic(self):
        first = await self._create_item(
            quality="common", level=12, seed=301, template_id="bulk-first"
        )
        second = await self._create_item(
            quality="common", level=18, seed=302, template_id="bulk-second"
        )
        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id, "普通"
        )
        self.assertEqual(
            {item_id for item_id, _name, _level in preview.items},
            {first.id, second.id},
        )
        self.assertEqual(
            preview.scrap_total,
            salvage_scrap_value("common", 12)
            + salvage_scrap_value("common", 18),
        )

        late = await self._create_item(
            quality="common", level=22, seed=303, template_id="bulk-late"
        )
        with self.assertRaisesRegex(ValueError, "背包内容已变化"):
            await self.workshop.bulk_salvage(
                self.owner.id, "common", preview.confirmation_token
            )
        self.assertEqual(
            (await self.workshop.wallet(self.owner.id)).scrap_balance,
            0,
        )

        current = await self.workshop.preview_bulk_salvage(
            self.owner.id, "common"
        )
        result = await self.workshop.bulk_salvage(
            self.owner.id, "普通", current.confirmation_token
        )
        self.assertEqual(result.item_count, 3)
        self.assertEqual(
            set(result.equipment_ids), {first.id, second.id, late.id}
        )
        self.assertEqual(result.scrap_gained, current.scrap_total)
        self.assertEqual(result.balance_after, current.scrap_total)
        with self.assertRaisesRegex(ValueError, "没有可批量分解"):
            await self.workshop.bulk_salvage(
                self.owner.id, "普通", current.confirmation_token
            )

    async def test_bulk_salvage_excludes_equipped_and_higher_quality_items(self):
        equipped = await self._create_item(
            quality="common", level=20, seed=311, template_id="bulk-equipped"
        )
        eligible = await self._create_item(
            quality="common", level=20, seed=312, template_id="bulk-eligible"
        )
        rare = await self._create_item(
            quality="rare", level=20, seed=313, template_id="bulk-rare"
        )
        await self.equipment.equip(self.owner.id, equipped.id)

        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id, "普通"
        )
        self.assertEqual(preview.item_count, 1)
        self.assertEqual(preview.items[0][0], eligible.id)
        await self.workshop.bulk_salvage(
            self.owner.id, "普通", preview.confirmation_token
        )
        self.assertEqual(
            (await self.equipment.item_detail(self.owner.id, equipped.id)).id,
            equipped.id,
        )
        self.assertEqual(
            (await self.equipment.item_detail(self.owner.id, rare.id)).id,
            rare.id,
        )
        with self.assertRaisesRegex(ValueError, "只支持普通、优秀或支配"):
            await self.workshop.preview_bulk_salvage(
                self.owner.id, "史诗"
            )

    async def test_excellent_cleanup_only_selects_safe_common_and_excellent(self):
        safe_common = await self._create_item(
            quality="common",
            level=20,
            seed=351,
            template_id="excellent-safe-common",
        )
        safe_excellent = await self._create_item(
            quality="excellent",
            level=22,
            seed=352,
            template_id="excellent-safe-excellent",
            random_affixes=(),
            used_capacity=0,
        )
        rare = await self._create_item(
            quality="rare",
            level=22,
            seed=353,
            template_id="excellent-protected-rare",
        )
        epic = await self._create_item(
            quality="epic",
            level=22,
            seed=363,
            template_id="excellent-protected-epic",
        )
        mythic = await self._create_item(
            quality="mythic",
            level=22,
            seed=364,
            template_id="excellent-protected-mythic",
        )
        equipped = await self._create_item(
            quality="common",
            level=20,
            seed=354,
            template_id="excellent-protected-equipped",
        )
        await self.equipment.equip(self.owner.id, equipped.id)
        locked = await self._create_item(
            quality="excellent",
            level=20,
            seed=355,
            template_id="excellent-protected-locked",
            random_affixes=(),
            used_capacity=0,
        )
        await self.equipment.set_item_locked(self.owner.id, locked.id, True)
        white_star = await self._create_item(
            quality="excellent",
            level=20,
            seed=356,
            template_id="excellent-protected-white",
            star_type="white_star",
            random_affixes=(),
            used_capacity=0,
        )
        black_star = await self._create_item(
            quality="excellent",
            level=20,
            seed=365,
            template_id="excellent-protected-black",
            star_type="black_star",
            random_affixes=(),
            used_capacity=0,
        )
        special = await self._create_item(
            quality="excellent",
            level=20,
            seed=357,
            template_id="excellent-protected-source",
            source_effects=("识破隐形",),
            random_affixes=(),
            used_capacity=0,
        )
        triggered = await self._create_item(
            quality="excellent",
            level=20,
            seed=358,
            template_id="excellent-protected-trigger",
            random_affixes=(
                {
                    "type": "trigger_ability",
                    "ability_id": "fire_ray",
                    "value": 0.1,
                    "capacity": 1,
                },
            ),
            used_capacity=1,
        )
        blessed = await self._create_item(
            quality="common",
            level=20,
            seed=359,
            template_id="excellent-protected-blessed",
            blessing_state="blessed",
        )
        enhanced = await self._create_item(
            quality="common",
            level=20,
            seed=360,
            template_id="excellent-protected-enhanced",
            enhancement_level=1,
        )
        pending = await self._create_item(
            quality="excellent",
            level=20,
            seed=361,
            template_id="excellent-protected-pending",
            random_affixes=(),
            used_capacity=0,
        )
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO equipment_rework_state "
                "(equipment_id, ruleset_id, status) VALUES (?, ?, 'pending')",
                (pending.id, "test-pending-ruleset"),
            )
            await db.commit()

        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "优秀",
        )

        self.assertEqual(preview.policy_id, "excellent")
        self.assertEqual(
            {item_id for item_id, _name, _level in preview.items},
            {safe_common.id, safe_excellent.id},
        )
        self.assertEqual(
            preview.scrap_total,
            salvage_scrap_value("common", 20)
            + salvage_scrap_value("excellent", 22),
        )

        result = await self.workshop.bulk_salvage(
            self.owner.id,
            "优秀",
            preview.confirmation_token,
        )
        self.assertEqual(
            set(result.equipment_ids),
            {safe_common.id, safe_excellent.id},
        )
        for protected in (
            rare,
            epic,
            mythic,
            equipped,
            locked,
            white_star,
            black_star,
            special,
            triggered,
            blessed,
            enhanced,
            pending,
        ):
            self.assertEqual(
                (await self.equipment.item_detail(self.owner.id, protected.id)).id,
                protected.id,
            )

    async def test_excellent_cleanup_token_binds_full_item_snapshot(self):
        item = await self._create_item(
            quality="excellent",
            level=25,
            seed=362,
            template_id="excellent-snapshot",
            random_affixes=(),
            used_capacity=0,
        )
        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "excellent",
        )
        replacement_affixes = (
            {
                "type": "stat_flat",
                "stat": "dexterity",
                "value": 3,
                "capacity": 1,
            },
        )
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE equipment_items SET random_affixes_json = ?, "
                "used_capacity = 1 WHERE id = ?",
                (json.dumps(replacement_affixes), item.id),
            )
            await db.commit()

        with self.assertRaisesRegex(ValueError, "背包内容已变化"):
            await self.workshop.bulk_salvage(
                self.owner.id,
                "优秀",
                preview.confirmation_token,
            )
        self.assertEqual(
            (await self.workshop.wallet(self.owner.id)).scrap_balance,
            0,
        )
        self.assertEqual(
            (await self.equipment.item_detail(self.owner.id, item.id)).random_affixes,
            replacement_affixes,
        )

        current = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "优秀",
        )
        self.assertNotEqual(current.confirmation_token, preview.confirmation_token)
        result = await self.workshop.bulk_salvage(
            self.owner.id,
            "优秀",
            current.confirmation_token,
        )
        self.assertEqual(result.equipment_ids, (item.id,))

    async def test_lock_persists_and_blocks_single_and_common_salvage(self):
        item = await self._create_item(
            quality="common",
            level=20,
            seed=321,
            template_id="locked-common",
        )
        locked = await self.equipment.set_item_locked(
            self.owner.id,
            item.id,
            True,
        )
        self.assertTrue(locked.is_locked)
        self.assertTrue(
            (await self.equipment.item_detail(self.owner.id, item.id)).is_locked
        )
        with self.assertRaisesRegex(ValueError, "收藏锁定"):
            await self.workshop.salvage(self.owner.id, item.id)
        with self.assertRaisesRegex(ValueError, "没有可批量分解"):
            await self.workshop.preview_bulk_salvage(self.owner.id, "普通")

        unlocked = await self.equipment.set_item_locked(
            self.owner.id,
            item.id,
            False,
        )
        self.assertFalse(unlocked.is_locked)
        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "普通",
        )
        self.assertEqual(preview.items[0][0], item.id)

    async def test_dominated_cleanup_is_conservative_preview_and_exact_token(self):
        safe_shape = {
            "inherent_affixes": (),
            "random_affixes": (),
            "fusion_affixes": (),
            "enchant_capacity": 0,
            "used_capacity": 0,
            "material": "iron",
        }
        weak = await self._create_item(
            quality="excellent",
            level=20,
            seed=331,
            template_id="dominated-weak",
            **safe_shape,
        )
        keeper = await self._create_item(
            quality="rare",
            level=20,
            seed=332,
            template_id="dominated-keeper",
            **safe_shape,
        )
        locked = await self._create_item(
            quality="common",
            level=20,
            seed=333,
            template_id="dominated-locked",
            **safe_shape,
        )
        await self.equipment.set_item_locked(self.owner.id, locked.id, True)
        equipped = await self._create_item(
            quality="common",
            level=20,
            seed=338,
            template_id="dominated-equipped",
            **safe_shape,
        )
        await self.equipment.equip(self.owner.id, equipped.id)
        epic = await self._create_item(
            quality="epic",
            level=20,
            seed=334,
            template_id="dominated-epic",
            weapon_type="staff",
            **safe_shape,
        )
        mythic = await self._create_item(
            quality="mythic",
            level=20,
            seed=339,
            template_id="dominated-mythic",
            weapon_type="staff",
            **safe_shape,
        )
        white_star = await self._create_item(
            quality="common",
            level=20,
            seed=335,
            template_id="dominated-white",
            star_type="white_star",
            **safe_shape,
        )
        special = await self._create_item(
            quality="common",
            level=20,
            seed=336,
            template_id="dominated-special",
            source_effects=("稀有装备发现率+15%",),
            **safe_shape,
        )

        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "支配",
        )
        self.assertEqual(preview.policy_id, "dominated")
        self.assertEqual(
            {item_id for item_id, _name, _level in preview.items},
            {weak.id},
        )
        self.assertEqual(preview.dominated_items[0].keeper_id, keeper.id)
        self.assertEqual(
            preview.dominated_items[0].direction_labels,
            ("灵巧",),
        )

        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE equipment_items SET enhancement_level = 1 WHERE id = ?",
                (keeper.id,),
            )
            await db.commit()
        with self.assertRaisesRegex(ValueError, "背包内容已变化"):
            await self.workshop.bulk_salvage(
                self.owner.id,
                "支配",
                preview.confirmation_token,
            )
        preview = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "支配",
        )

        late = await self._create_item(
            quality="common",
            level=20,
            seed=337,
            template_id="dominated-late",
            **safe_shape,
        )
        with self.assertRaisesRegex(ValueError, "背包内容已变化"):
            await self.workshop.bulk_salvage(
                self.owner.id,
                "支配",
                preview.confirmation_token,
            )
        current = await self.workshop.preview_bulk_salvage(
            self.owner.id,
            "支配",
        )
        self.assertEqual(
            {item_id for item_id, _name, _level in current.items},
            {weak.id, late.id},
        )
        result = await self.workshop.bulk_salvage(
            self.owner.id,
            "支配",
            current.confirmation_token,
        )
        self.assertEqual(set(result.equipment_ids), {weak.id, late.id})
        for protected in (
            keeper,
            locked,
            equipped,
            epic,
            mythic,
            white_star,
            special,
        ):
            self.assertEqual(
                (await self.equipment.item_detail(self.owner.id, protected.id)).id,
                protected.id,
            )

    async def test_dominated_cleanup_preserves_build_tradeoffs(self):
        weak = await self._create_item(
            quality="excellent",
            level=20,
            seed=341,
            template_id="tradeoff-weak",
            inherent_affixes=(),
            random_affixes=(
                {
                    "type": "stat_flat",
                    "stat": "dexterity",
                    "value": 5,
                    "capacity": 1,
                },
            ),
            fusion_affixes=(),
            enchant_capacity=1,
            used_capacity=1,
            material="iron",
        )
        keeper = await self._create_item(
            quality="rare",
            level=20,
            seed=342,
            template_id="tradeoff-keeper",
            inherent_affixes=(),
            random_affixes=(),
            fusion_affixes=(),
            enchant_capacity=1,
            used_capacity=0,
            material="iron",
        )
        with self.assertRaisesRegex(ValueError, "没有符合安全规则"):
            await self.workshop.preview_bulk_salvage(
                self.owner.id,
                "支配",
            )
        self.assertEqual(
            (await self.equipment.item_detail(self.owner.id, weak.id)).id,
            weak.id,
        )
        self.assertEqual(
            (await self.equipment.item_detail(self.owner.id, keeper.id)).id,
            keeper.id,
        )

    async def test_salvage_rejects_wrong_owner_equipped_starter_and_black_star(self):
        item = await self._create_item(quality="rare", level=20)
        with self.assertRaisesRegex(ValueError, "不属于"):
            await self.workshop.salvage(self.other.id, item.id)

        await self.equipment.equip(self.owner.id, item.id)
        with self.assertRaisesRegex(ValueError, "装备中"):
            await self.workshop.salvage(self.owner.id, item.id)
        self.assertEqual((await self.workshop.wallet(self.owner.id)).scrap_balance, 0)

        starter = next(
            item
            for item in await self.equipment.list_items(self.owner.id)
            if item.item_level == 0
        )
        await self.equipment.unequip_all(self.owner.id)
        with self.assertRaisesRegex(ValueError, "新手"):
            await self.workshop.salvage(self.owner.id, starter.id)

        black_entry = self.equipment.catalog.get(2001)
        black = self.equipment.factory.create_from_catalog(
            self.owner.id, black_entry, seed=5
        )
        async with await connect_db(self.db_path) as db:
            black_id = await self.equipment.insert_item_in_db(db, black)
            await db.commit()
        with self.assertRaisesRegex(ValueError, "黑星"):
            await self.workshop.salvage(self.owner.id, black_id)

    async def test_preview_charges_once_and_reject_keeps_item_unchanged(self):
        item = await self._create_item(quality="rare", level=30, seed=11)
        await self._fund_by_salvage(100)
        balance_before = (await self.workshop.wallet(self.owner.id)).scrap_balance

        preview = await self.workshop.preview_rework(
            self.owner.id, item.id, "力量", seed=19
        )
        self.assertEqual(
            preview.balance_after,
            balance_before - rework_cost("rare", 30).total,
        )
        with self.assertRaisesRegex(ValueError, "待决定"):
            await self.workshop.preview_rework(
                self.owner.id, item.id, "力量", seed=20
            )
        self.assertEqual(
            (await self.workshop.wallet(self.owner.id)).scrap_balance,
            preview.balance_after,
        )

        decision = await self.workshop.reject_rework(self.owner.id, item.id)
        persisted = await self.equipment.item_detail(self.owner.id, item.id)
        self.assertFalse(decision.accepted)
        self.assertEqual(persisted.random_affixes, item.random_affixes)
        self.assertEqual(
            (await self.workshop.wallet(self.owner.id)).scrap_balance,
            preview.balance_after,
        )

    async def test_accept_changes_only_random_affixes(self):
        item = await self._create_item(quality="epic", level=45, seed=33)
        await self._fund_by_salvage(200)
        preview = await self.workshop.preview_rework(
            self.owner.id, item.id, "奥术", seed=42
        )
        result = await self.workshop.accept_rework(self.owner.id, item.id)
        persisted = await self.equipment.item_detail(self.owner.id, item.id)

        self.assertTrue(result.accepted)
        self.assertEqual(persisted.random_affixes, preview.candidate_affixes)
        before = item.to_dict()
        after = persisted.to_dict()
        before.pop("random_affixes")
        after.pop("random_affixes")
        self.assertEqual(after, before)
        self.assertLessEqual(
            len(persisted.random_affixes),
            {"excellent": 1, "rare": 2, "epic": 3, "mythic": 4}[
                persisted.quality
            ],
        )

    async def test_fifth_consecutive_miss_guarantees_target_affix(self):
        item = await self._create_item(quality="excellent", level=20, seed=71)
        await self._fund_by_salvage(500)
        miss_seed = next(
            seed
            for seed in range(1000)
            if direction_match_score(
                self.workshop._roll_candidate_affixes(
                    item, "fortune", random.Random(seed), force_target=False
                ),
                "fortune",
            )
            == 0
        )

        for expected_streak in range(1, 5):
            preview = await self.workshop.preview_rework(
                self.owner.id, item.id, "奇运", seed=miss_seed
            )
            self.assertFalse(preview.pity_guaranteed)
            self.assertEqual(preview.match_score, 0)
            self.assertEqual(preview.miss_streak_after, expected_streak)
            await self.workshop.reject_rework(self.owner.id, item.id)

        guaranteed = await self.workshop.preview_rework(
            self.owner.id, item.id, "奇运", seed=miss_seed
        )
        self.assertTrue(guaranteed.pity_guaranteed)
        self.assertGreater(guaranteed.match_score, 0)
        self.assertEqual(guaranteed.miss_streak_after, 0)

    async def test_rework_validations_are_atomic(self):
        item = await self._create_item(quality="rare", level=25, seed=80)
        with self.assertRaisesRegex(ValueError, "碎片不足"):
            await self.workshop.preview_rework(
                self.owner.id, item.id, "防御", seed=1
            )
        self.assertEqual((await self.workshop.wallet(self.owner.id)).scrap_balance, 0)

        common = await self._create_item(quality="common", level=25, seed=81)
        with self.assertRaisesRegex(ValueError, "优秀及以上"):
            await self.workshop.preview_rework(
                self.owner.id, common.id, "防御", seed=1
            )

        over_slots = replace(
            item,
            random_affixes=item.random_affixes
            + ({"type": "accuracy", "value": 1, "capacity": 1},),
            used_capacity=item.used_capacity + 1,
        )
        async with await connect_db(self.db_path) as db:
            over_id = await self.equipment.insert_item_in_db(
                db, replace(over_slots, id=None, template_id="over-slots")
            )
            await db.commit()
        await self._fund_by_salvage(100)
        with self.assertRaisesRegex(ValueError, "品质上限"):
            await self.workshop.preview_rework(
                self.owner.id, over_id, "防御", seed=1
            )

    async def test_accept_rechecks_equipped_state_and_preserves_pending_preview(self):
        item = await self._create_item(quality="rare", level=30, seed=91)
        await self._fund_by_salvage(100)
        preview = await self.workshop.preview_rework(
            self.owner.id, item.id, "射击", seed=4
        )
        await self.equipment.equip(self.owner.id, item.id)
        with self.assertRaisesRegex(ValueError, "装备中"):
            await self.workshop.accept_rework(self.owner.id, item.id)
        unchanged = await self.equipment.item_detail(self.owner.id, item.id)
        self.assertEqual(unchanged.random_affixes, item.random_affixes)

        await self.equipment.unequip_all(self.owner.id)
        accepted = await self.workshop.accept_rework(self.owner.id, item.id)
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.item.random_affixes, preview.candidate_affixes)

    async def test_paid_pending_preview_cannot_be_destroyed_by_salvage(self):
        item = await self._create_item(quality="rare", level=30, seed=97)
        await self._fund_by_salvage(100)
        preview = await self.workshop.preview_rework(
            self.owner.id, item.id, "力量", seed=10
        )
        with self.assertRaisesRegex(ValueError, "待决定"):
            await self.workshop.salvage(self.owner.id, item.id)
        persisted = await self.equipment.item_detail(self.owner.id, item.id)
        self.assertEqual(persisted.random_affixes, item.random_affixes)
        self.assertEqual(
            (await self.workshop.wallet(self.owner.id)).scrap_balance,
            preview.balance_after,
        )

    async def test_season_imprint_guarantees_direction_and_is_auditable(self):
        item = await self._create_item(quality="excellent", level=25, seed=201)
        await self._fund_by_salvage(100)
        await self._set_season_tokens(SEASON_REWORK_TOKEN_COST)
        miss_seed = next(
            seed
            for seed in range(2000)
            if direction_match_score(
                self.workshop._roll_candidate_affixes(
                    item,
                    "fortune",
                    random.Random(seed),
                    force_target=False,
                ),
                "fortune",
            )
            == 0
        )
        wallet_before = await self.workshop.wallet(self.owner.id)

        preview = await self.workshop.preview_season_rework(
            self.owner.id,
            item.id,
            "奇运",
            seed=miss_seed,
        )
        decision = await self.workshop.reject_rework(self.owner.id, item.id)

        self.assertEqual(preview.mode, SEASON_REWORK_MODE)
        self.assertTrue(preview.target_guaranteed)
        self.assertFalse(preview.pity_guaranteed)
        self.assertGreater(preview.match_score, 0)
        self.assertEqual(preview.cost.season_tokens, SEASON_REWORK_TOKEN_COST)
        self.assertEqual(
            preview.balance_after,
            wallet_before.scrap_balance - preview.cost.total,
        )
        self.assertEqual(preview.season_tokens_after, 0)
        self.assertEqual(decision.mode, SEASON_REWORK_MODE)
        self.assertEqual(
            decision.season_tokens_spent,
            SEASON_REWORK_TOKEN_COST,
        )
        self.assertEqual(decision.season_tokens_balance, 0)
        wallet_after_decision = await self.workshop.wallet(self.owner.id)
        self.assertEqual(wallet_after_decision.scrap_balance, preview.balance_after)
        self.assertEqual(
            wallet_after_decision.season_tokens,
            preview.season_tokens_after,
        )

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT status, original_snapshot_json, reworked_snapshot_json
                FROM equipment_rework_state WHERE equipment_id = ?
                """,
                (item.id,),
            ).fetchone()
        finally:
            connection.close()
        original = json.loads(row["original_snapshot_json"])
        candidate = json.loads(row["reworked_snapshot_json"])
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(original["mode"], SEASON_REWORK_MODE)
        self.assertEqual(candidate["guarantee_source"], "season_imprint")
        self.assertEqual(candidate["season_tokens_cost"], 20)
        self.assertEqual(candidate["decision"], "rejected")

    async def test_season_imprint_insufficient_balance_and_late_failure_roll_back(self):
        item = await self._create_item(quality="rare", level=35, seed=211)
        await self._fund_by_salvage(120)
        await self._set_season_tokens(SEASON_REWORK_TOKEN_COST - 1)
        before = await self.workshop.wallet(self.owner.id)

        with self.assertRaisesRegex(ValueError, "赛季币不足"):
            await self.workshop.preview_season_rework(
                self.owner.id,
                item.id,
                "防御",
                seed=4,
            )
        self.assertEqual(await self.workshop.wallet(self.owner.id), before)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM equipment_rework_state WHERE equipment_id = ?",
                    (item.id,),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

        await self._set_season_tokens(SEASON_REWORK_TOKEN_COST)
        funded = await self.workshop.wallet(self.owner.id)
        original_dump = self.workshop._dump

        def fail_after_wallet_debit(_value):
            raise RuntimeError("injected snapshot failure")

        self.workshop._dump = fail_after_wallet_debit
        try:
            with self.assertRaisesRegex(RuntimeError, "snapshot failure"):
                await self.workshop.preview_season_rework(
                    self.owner.id,
                    item.id,
                    "防御",
                    seed=5,
                )
        finally:
            self.workshop._dump = original_dump
        self.assertEqual(await self.workshop.wallet(self.owner.id), funded)
        persisted = await self.equipment.item_detail(self.owner.id, item.id)
        self.assertEqual(persisted.random_affixes, item.random_affixes)

    async def test_concurrent_season_previews_charge_only_one_pending_candidate(self):
        item = await self._create_item(quality="rare", level=30, seed=221)
        await self._fund_by_salvage(200)
        await self._set_season_tokens(SEASON_REWORK_TOKEN_COST * 2)
        before = await self.workshop.wallet(self.owner.id)

        def preview_in_independent_connection(seed):
            return asyncio.run(
                self.workshop.preview_season_rework(
                    self.owner.id,
                    item.id,
                    "射击",
                    seed=seed,
                )
            )

        results = await asyncio.gather(
            asyncio.to_thread(preview_in_independent_connection, 31),
            asyncio.to_thread(preview_in_independent_connection, 32),
            return_exceptions=True,
        )
        previews = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, Exception)]

        self.assertEqual(len(previews), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertIn("待决定", str(failures[0]))
        after = await self.workshop.wallet(self.owner.id)
        self.assertEqual(
            after.scrap_balance,
            before.scrap_balance - previews[0].cost.total,
        )
        self.assertEqual(
            after.season_tokens,
            before.season_tokens - SEASON_REWORK_TOKEN_COST,
        )

    async def test_accepting_season_imprint_only_changes_random_affixes(self):
        item = await self._create_item(quality="epic", level=50, seed=231)
        await self._fund_by_salvage(250)
        await self._set_season_tokens(SEASON_REWORK_TOKEN_COST)
        preview = await self.workshop.preview_season_rework(
            self.owner.id,
            item.id,
            "奥术",
            seed=77,
        )
        result = await self.workshop.accept_rework(self.owner.id, item.id)
        persisted = await self.equipment.item_detail(self.owner.id, item.id)

        self.assertTrue(result.accepted)
        self.assertEqual(persisted.random_affixes, preview.candidate_affixes)
        before = item.to_dict()
        after = persisted.to_dict()
        before.pop("random_affixes")
        after.pop("random_affixes")
        self.assertEqual(after, before)
        wallet = await self.workshop.wallet(self.owner.id)
        self.assertEqual(wallet.season_tokens, 0)
        self.assertEqual(result.season_tokens_balance, 0)

    async def test_season_imprint_does_not_consume_standard_four_miss_pity(self):
        item = await self._create_item(quality="excellent", level=20, seed=241)
        await self._fund_by_salvage(600)
        await self._set_season_tokens(SEASON_REWORK_TOKEN_COST)
        miss_seed = next(
            seed
            for seed in range(2000)
            if direction_match_score(
                self.workshop._roll_candidate_affixes(
                    item,
                    "fortune",
                    random.Random(seed),
                    force_target=False,
                ),
                "fortune",
            )
            == 0
        )
        for _ in range(4):
            preview = await self.workshop.preview_rework(
                self.owner.id,
                item.id,
                "奇运",
                seed=miss_seed,
            )
            self.assertFalse(preview.pity_guaranteed)
            await self.workshop.reject_rework(self.owner.id, item.id)

        imprint = await self.workshop.preview_season_rework(
            self.owner.id,
            item.id,
            "奇运",
            seed=miss_seed,
        )
        self.assertEqual(imprint.miss_streak_before, 4)
        self.assertEqual(imprint.miss_streak_after, 4)
        await self.workshop.reject_rework(self.owner.id, item.id)

        guaranteed = await self.workshop.preview_rework(
            self.owner.id,
            item.id,
            "奇运",
            seed=miss_seed,
        )
        self.assertTrue(guaranteed.pity_guaranteed)
        self.assertGreater(guaranteed.match_score, 0)


if __name__ == "__main__":
    unittest.main()
