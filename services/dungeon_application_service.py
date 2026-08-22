"""Persistent, transactional application API for interactive random Nefia.

``DungeonAdventureFacade`` remains the pure domain/gameplay facade.  This
module composes it with player builds, a SQLite snapshot store, growth services
and an idempotent reward settler.  Every state-changing call owns one
``BEGIN IMMEDIATE`` transaction so two QQ events cannot advance the same run or
grant the same loot twice.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, replace

try:
    from ..models.dungeon import (
        DungeonAdventure,
        DungeonAdventureView,
        DungeonApplicationResult,
        DungeonRewardIntent,
        DungeonRewardReceipt,
        DungeonRiskView,
        DungeonRouteView,
    )
    from ..models.user import UserIdentity
    from .ability_catalog import SPELL_DEFINITIONS
    from .combat_ai import profile_for_strategy
    from .combat_engine import SideviewCombatEngine
    from .daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
    )
    from .db import connect_db
    from .dungeon_adventure_service import (
        DungeonAdventureFacade,
        InMemoryDungeonAdventureRepository,
    )
    from .dungeon_catalog import DungeonCatalog
    from .dungeon_nefia_catalog import DungeonNefiaCatalog
    from .dungeon_snapshot_codec import dump_adventure, load_adventure
    from .equipment_service import reward_quality_policy
    from .spell_service import select_spellbook_drop, spellbook_tier_cap
except ImportError:
    from models.dungeon import (
        DungeonAdventure,
        DungeonAdventureView,
        DungeonApplicationResult,
        DungeonRewardIntent,
        DungeonRewardReceipt,
        DungeonRiskView,
        DungeonRouteView,
    )
    from models.user import UserIdentity
    from services.ability_catalog import SPELL_DEFINITIONS
    from services.combat_ai import profile_for_strategy
    from services.combat_engine import SideviewCombatEngine
    from services.daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
    )
    from services.db import connect_db
    from services.dungeon_adventure_service import (
        DungeonAdventureFacade,
        InMemoryDungeonAdventureRepository,
    )
    from services.dungeon_catalog import DungeonCatalog
    from services.dungeon_nefia_catalog import DungeonNefiaCatalog
    from services.dungeon_snapshot_codec import dump_adventure, load_adventure
    from services.equipment_service import reward_quality_policy
    from services.spell_service import select_spellbook_drop, spellbook_tier_cap


_REWARD_SOURCE = "dungeon_nefia"
_OVERFLOW_EXP_PER_SCRAP = 500
_OVERFLOW_SCRAP_PER_INTENT_CAP = 2
_SIGNED_SQLITE_SEED_MASK = (1 << 63) - 1
_QUALITY_LABELS = {
    "common": "普通",
    "excellent": "优秀",
    "rare": "稀有",
    "epic": "史诗",
    "mythic": "神话",
}


class SQLiteDungeonAdventureStore:
    """Async SQLite adapter; transaction ownership stays with its caller."""

    @staticmethod
    async def find_cycle_in_db(
        db,
        *,
        owner_pk: int,
        dungeon_id: str,
        cycle_key: str,
    ) -> DungeonAdventure | None:
        cursor = await db.execute(
            "SELECT snapshot_json, version FROM dungeon_adventures "
            "WHERE owner_pk = ? AND dungeon_id = ? AND cycle_key = ?",
            (int(owner_pk), str(dungeon_id), str(cycle_key)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        adventure = load_adventure(str(row["snapshot_json"]))
        if adventure.version != int(row["version"]):
            raise RuntimeError("奈菲亚存档版本损坏")
        return adventure

    @staticmethod
    async def get_owned_in_db(
        db,
        *,
        owner_pk: int,
        adventure_id: str,
    ) -> DungeonAdventure:
        cursor = await db.execute(
            "SELECT snapshot_json, version FROM dungeon_adventures "
            "WHERE adventure_id = ? AND owner_pk = ?",
            (str(adventure_id), int(owner_pk)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise KeyError("未找到属于你的奈菲亚探险")
        adventure = load_adventure(str(row["snapshot_json"]))
        if adventure.version != int(row["version"]):
            raise RuntimeError("奈菲亚存档版本损坏")
        return adventure

    @staticmethod
    async def add_in_db(db, *, owner_pk: int, adventure: DungeonAdventure, now_ts: int) -> None:
        await db.execute(
            """
            INSERT INTO dungeon_adventures (
                adventure_id, owner_pk, owner_key, group_key, dungeon_id,
                cycle_key, phase, floor_index, version, snapshot_json,
                created_at_ts, updated_at_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                adventure.adventure_id,
                int(owner_pk),
                adventure.owner_key,
                adventure.group_key,
                adventure.dungeon_id,
                adventure.cycle_key,
                adventure.phase,
                adventure.floor_index,
                adventure.version,
                dump_adventure(adventure),
                int(now_ts),
                int(now_ts),
            ),
        )

    @staticmethod
    async def save_in_db(
        db,
        *,
        owner_pk: int,
        adventure: DungeonAdventure,
        expected_version: int,
        now_ts: int,
    ) -> None:
        if adventure.version != int(expected_version) + 1:
            raise ValueError("奈菲亚版本必须严格递增")
        await db.execute(
            """
            UPDATE dungeon_adventures
            SET phase = ?, floor_index = ?, version = ?, snapshot_json = ?,
                updated_at_ts = ?
            WHERE adventure_id = ? AND owner_pk = ? AND version = ?
            """,
            (
                adventure.phase,
                adventure.floor_index,
                adventure.version,
                dump_adventure(adventure),
                int(now_ts),
                adventure.adventure_id,
                int(owner_pk),
                int(expected_version),
            ),
        )
        cursor = await db.execute("SELECT changes() AS count")
        row = await cursor.fetchone()
        await cursor.close()
        if int(row["count"]) != 1:
            raise RuntimeError("奈菲亚状态已被其他操作更新，请重新查看")


class DungeonRewardSettlementService:
    """Verify persisted intents, then atomically grant through owner services."""

    def __init__(
        self,
        user_service,
        equipment_service,
        spell_service,
    ) -> None:
        self.user_service = user_service
        self.equipment_service = equipment_service
        self.spell_service = spell_service

    async def settle_in_db(
        self,
        db,
        *,
        user,
        adventure: DungeonAdventure,
        intents: tuple[DungeonRewardIntent, ...],
        now_ts: int,
    ) -> tuple[tuple[DungeonRewardReceipt, ...], object]:
        canonical = {item.source_key: item for item in adventure.reward_intents}
        if len(canonical) != len(adventure.reward_intents):
            raise RuntimeError("奈菲亚存档包含重复奖励键")
        for intent in intents:
            expected = canonical.get(intent.source_key)
            if expected is None or expected.to_dict() != intent.to_dict():
                raise ValueError("奖励意图不属于该奈菲亚存档，已拒绝结算")
            self._validate_intent(adventure, intent)

        receipts: list[DungeonRewardReceipt] = []
        current_user = user
        for intent in intents:
            existing = await self._existing_receipt_in_db(
                db, int(user.id), intent.source_key
            )
            if existing is not None:
                receipts.append(existing)
                continue

            receipt, current_user = await self._grant_one_in_db(
                db,
                user=current_user,
                adventure=adventure,
                intent=intent,
                now_ts=int(now_ts),
            )
            reason = json.dumps(
                asdict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await db.execute(
                """
                INSERT INTO reward_ledger (
                    reward_key, user_pk, battle_id, source, exp_gain,
                    currency_gain, reason, created_at_ts
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    intent.source_key,
                    int(user.id),
                    _REWARD_SOURCE,
                    receipt.exp_gain,
                    receipt.scrap_gain,
                    reason,
                    int(now_ts),
                ),
            )
            receipts.append(receipt)
        return tuple(receipts), current_user

    async def _grant_one_in_db(
        self,
        db,
        *,
        user,
        adventure: DungeonAdventure,
        intent: DungeonRewardIntent,
        now_ts: int,
    ) -> tuple[DungeonRewardReceipt, object]:
        if intent.reward_type == "experience":
            allocation = await allocate_daily_growth_in_db(
                db,
                user_pk=int(user.id),
                level=int(user.level),
                requested_exp=int(intent.quantity),
                at=int(now_ts),
            )
            granted = int(allocation.granted)
            current_user = user
            if granted > 0:
                result = await self.user_service.add_exp_in_db(db, user, granted)
                current_user = result.user
            blocked = max(0, int(intent.quantity) - granted)
            overflow_scrap = (
                min(
                    _OVERFLOW_SCRAP_PER_INTENT_CAP,
                    max(1, math.ceil(blocked / _OVERFLOW_EXP_PER_SCRAP)),
                )
                if blocked else 0
            )
            if overflow_scrap:
                await self._credit_scrap_in_db(
                    db,
                    user_pk=int(user.id),
                    gained=overflow_scrap,
                    now_ts=int(now_ts),
                )
            if overflow_scrap and granted:
                description = (
                    f"冒险经验 +{granted}，额度外历练转为工坊碎片 "
                    f"+{overflow_scrap}"
                )
            elif overflow_scrap:
                description = (
                    "今日成长额度已满，本层历练沉淀为工坊碎片 "
                    f"+{overflow_scrap}"
                )
            else:
                description = f"冒险经验 +{granted}"
            return (
                DungeonRewardReceipt(
                    intent.source_key,
                    intent.reward_type,
                    intent.quantity,
                    True,
                    description,
                    exp_gain=granted,
                    scrap_gain=overflow_scrap,
                ),
                current_user,
            )

        if intent.reward_type == "equipment":
            ids: list[int] = []
            names: list[str] = []
            for index in range(intent.quantity):
                seed = self._child_seed(intent.random_seed, "equipment", index)
                item = self.equipment_service.generate_reward(
                    int(user.id),
                    intent.catalog_id_min,
                    intent.catalog_id_max,
                    intent.item_level_min,
                    intent.item_level_max,
                    seed=seed,
                    quality_bonus=float(intent.quality_bonus),
                )
                equipment_id = await self.equipment_service.insert_item_in_db(db, item)
                ids.append(int(equipment_id))
                quality_label = _QUALITY_LABELS.get(str(item.quality), str(item.quality))
                names.append(f"【{quality_label}】{item.name}")
            return (
                DungeonRewardReceipt(
                    intent.source_key,
                    intent.reward_type,
                    intent.quantity,
                    True,
                    "获得装备：" + "、".join(names),
                    equipment_ids=tuple(ids),
                    equipment_names=tuple(names),
                ),
                user,
            )

        if intent.reward_type == "spellbook":
            spell_ids: list[str] = []
            spell_names: list[str] = []
            known = set((await self.spell_service.spells_in_db(db, int(user.id))).keys())
            held = {
                item.spell_id
                for item in await self.spell_service.books_in_db(db, int(user.id))
            }
            for index in range(intent.quantity):
                seed = self._child_seed(intent.random_seed, "spellbook", index)
                spell_id = self._select_dungeon_spell(
                    intent,
                    seed,
                    known,
                    held,
                    player_level=adventure.player_level,
                )
                result = await self.spell_service.grant_book_reward_in_db(
                    db,
                    user_pk=int(user.id),
                    spell_id=spell_id,
                    reward_key=(
                        intent.source_key if intent.quantity == 1
                        else f"{intent.source_key}:{index + 1}"
                    ),
                    source=_REWARD_SOURCE,
                    random_seed=seed,
                )
                spell_ids.append(result.drop.spell_id)
                spell_names.append(result.drop.spell_name)
                held.add(result.drop.spell_id)
            return (
                DungeonRewardReceipt(
                    intent.source_key,
                    intent.reward_type,
                    intent.quantity,
                    True,
                    "发现魔法书：" + "、".join(spell_names),
                    spell_ids=tuple(spell_ids),
                    spell_names=tuple(spell_names),
                ),
                user,
            )

        if intent.reward_type == "salvage":
            gained = int(intent.quantity)
            await self._credit_scrap_in_db(
                db,
                user_pk=int(user.id),
                gained=gained,
                now_ts=int(now_ts),
            )
            return (
                DungeonRewardReceipt(
                    intent.source_key,
                    intent.reward_type,
                    intent.quantity,
                    True,
                    f"工坊碎片 +{gained}",
                    scrap_gain=gained,
                ),
                user,
            )
        raise ValueError("未知奈菲亚奖励类型")

    @staticmethod
    async def _credit_scrap_in_db(
        db,
        *,
        user_pk: int,
        gained: int,
        now_ts: int,
    ) -> None:
        if gained <= 0:
            return
        await db.execute(
            """
            INSERT INTO workshop_wallet (
                user_pk, scrap_balance, season_tokens, lifetime_earned,
                lifetime_spent, updated_at_ts
            ) VALUES (?, ?, 0, ?, 0, ?)
            ON CONFLICT(user_pk) DO UPDATE SET
                scrap_balance = scrap_balance + excluded.scrap_balance,
                lifetime_earned = lifetime_earned + excluded.lifetime_earned,
                updated_at_ts = excluded.updated_at_ts
            """,
            (int(user_pk), int(gained), int(gained), int(now_ts)),
        )

    @staticmethod
    def _select_dungeon_spell(
        intent: DungeonRewardIntent,
        seed: int,
        known_spell_ids: set[str],
        held_spell_ids: set[str] | None = None,
        *,
        player_level: int,
    ) -> str:
        """Prefer discoveries, avoid unread stacks, and keep the long tail."""
        learned = {str(spell_id) for spell_id in known_spell_ids}
        held = {str(spell_id) for spell_id in (held_spell_ids or set())}
        tier_cap = spellbook_tier_cap(int(player_level))
        eligible = {
            spell_id
            for spell_id, definition in SPELL_DEFINITIONS.items()
            if definition.unlock_level <= tier_cap
        }
        novel = eligible - learned - held
        learned_repeats = (eligible & learned) - held
        novelty_roll = DungeonRewardSettlementService._child_seed(
            seed, "novelty", 0
        ) % 100
        if novel and (not learned_repeats or novelty_roll < 85):
            selection_pool = novel
        elif learned_repeats:
            selection_pool = learned_repeats
        else:
            selection_pool = (eligible - held) or eligible

        full_pool_drop = select_spellbook_drop(
            random_seed=int(seed),
            player_level=int(player_level),
            known_spell_ids=learned,
            excluded_spell_ids=eligible - selection_pool,
        )
        preferred = tuple(
            spell_id for spell_id in intent.spell_pool
            if (
                spell_id in selection_pool
                and spell_id in SPELL_DEFINITIONS
            )
        )
        # Three in ten books use the local pool.  The remaining seven use the
        # 84-spell selector, so a small Nefia catalog can never make the long
        # tail permanently unobtainable.
        flavour_roll = DungeonRewardSettlementService._child_seed(
            seed, "regional-flavour", 0
        ) % 10
        if preferred and flavour_roll < 3:
            return preferred[
                DungeonRewardSettlementService._child_seed(
                    seed, "regional-choice", 0
                ) % len(preferred)
            ]
        return full_pool_drop.spell_id

    @staticmethod
    def _validate_intent(
        adventure: DungeonAdventure,
        intent: DungeonRewardIntent,
    ) -> None:
        if not intent.source_key.startswith(f"{adventure.settlement_key}:"):
            raise ValueError("奈菲亚奖励命名空间不匹配")
        quantity_cap = {
            "experience": 100_000,
            "equipment": 10,
            "spellbook": 10,
            "salvage": 10_000,
        }.get(intent.reward_type)
        if quantity_cap is None or not 1 <= int(intent.quantity) <= quantity_cap:
            raise ValueError("奈菲亚奖励数量非法")
        if intent.reward_type == "equipment":
            if (
                intent.catalog_id_min < 0
                or intent.catalog_id_max <= intent.catalog_id_min
                or intent.item_level_min < 0
                or intent.item_level_max < intent.item_level_min
            ):
                raise ValueError("奈菲亚装备奖励范围非法")
            if (
                isinstance(intent.quality_bonus, bool)
                or not math.isfinite(float(intent.quality_bonus))
                or float(intent.quality_bonus) < 0
            ):
                raise ValueError("奈菲亚装备品质增益非法")
        if any(spell_id not in SPELL_DEFINITIONS for spell_id in intent.spell_pool):
            raise ValueError("奈菲亚魔法书池包含未知法术")

    @staticmethod
    async def _existing_receipt_in_db(
        db,
        user_pk: int,
        reward_key: str,
    ) -> DungeonRewardReceipt | None:
        cursor = await db.execute(
            "SELECT user_pk, source, reason FROM reward_ledger WHERE reward_key = ?",
            (str(reward_key),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        if int(row["user_pk"]) != int(user_pk) or str(row["source"]) != _REWARD_SOURCE:
            raise ValueError("奖励键已被其他来源占用")
        raw = json.loads(str(row["reason"]))
        return DungeonRewardReceipt(
            reward_key=str(raw["reward_key"]),
            reward_type=str(raw["reward_type"]),
            quantity=int(raw["quantity"]),
            applied=False,
            description=str(raw["description"]),
            exp_gain=int(raw.get("exp_gain", 0)),
            scrap_gain=int(raw.get("scrap_gain", 0)),
            equipment_ids=tuple(int(item) for item in raw.get("equipment_ids", ())),
            equipment_names=tuple(str(item) for item in raw.get("equipment_names", ())),
            spell_ids=tuple(str(item) for item in raw.get("spell_ids", ())),
            spell_names=tuple(str(item) for item in raw.get("spell_names", ())),
        )

    @staticmethod
    def _child_seed(seed: int, component: str, index: int) -> int:
        digest = hashlib.blake2b(
            f"{int(seed)}\0{component}\0{int(index)}".encode("utf-8"),
            digest_size=8,
        ).digest()
        return int.from_bytes(digest, "big") & _SIGNED_SQLITE_SEED_MASK


class DungeonAdventureApplicationService:
    """Thin handler-facing facade over dungeon domain, storage and settlement."""

    def __init__(
        self,
        db_path: str,
        user_service,
        build_service,
        monster_build_service,
        equipment_service,
        skill_service,
        attribute_service,
        spell_service,
        *,
        combat_engine: SideviewCombatEngine | None = None,
        dungeon_catalog: DungeonCatalog | None = None,
        nefia_catalog: DungeonNefiaCatalog | None = None,
    ) -> None:
        self.db_path = db_path
        self.user_service = user_service
        self.build_service = build_service
        self.monster_build_service = monster_build_service
        self.equipment_service = equipment_service
        self.skill_service = skill_service
        self.attribute_service = attribute_service
        self.spell_service = spell_service
        self.combat_engine = combat_engine or SideviewCombatEngine()
        self.dungeon_catalog = dungeon_catalog or DungeonCatalog(
            monster_catalog=monster_build_service.catalog
        )
        self.nefia_catalog = nefia_catalog or DungeonNefiaCatalog(
            monster_catalog=monster_build_service.catalog
        )
        self.store = SQLiteDungeonAdventureStore()
        self.settlement = DungeonRewardSettlementService(
            user_service, equipment_service, spell_service
        )

    async def start_or_resume(
        self,
        identity: UserIdentity,
        dungeon_id: str,
        difficulty: int = 1,
        strategy: str = "",
        *,
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        timestamp = int(time.time() if now_ts is None else now_ts)
        cycle_key = daily_growth_day_window(timestamp)[0]
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                context = await self._context_in_db(db, identity, strategy)
                existing = await self.store.find_cycle_in_db(
                    db,
                    owner_pk=context["user"].id,
                    dungeon_id=dungeon_id,
                    cycle_key=cycle_key,
                )
                if existing is None:
                    facade = self._domain_facade()
                    adventure = facade.start_daily(
                        owner_key=f"user:{context['user'].id}",
                        group_key=str(identity.group_id),
                        dungeon_id=dungeon_id,
                        player_level=int(context["user"].level),
                        cycle_key=cycle_key,
                        difficulty=int(difficulty),
                        capabilities=context["capabilities"],
                        exploration_skills=context["exploration_skills"],
                    )
                    adventure = replace(adventure, strategy=str(strategy or ""))
                    await self.store.add_in_db(
                        db,
                        owner_pk=context["user"].id,
                        adventure=adventure,
                        now_ts=timestamp,
                    )
                else:
                    adventure = existing
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return DungeonApplicationResult(self._view(adventure, context))

    async def view(
        self,
        identity: UserIdentity,
        adventure_id: str = "",
        *,
        dungeon_id: str = "",
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        timestamp = int(time.time() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                context = await self._context_in_db(db, identity, "")
                if adventure_id:
                    adventure = await self.store.get_owned_in_db(
                        db,
                        owner_pk=context["user"].id,
                        adventure_id=adventure_id,
                    )
                elif dungeon_id:
                    adventure = await self.store.find_cycle_in_db(
                        db,
                        owner_pk=context["user"].id,
                        dungeon_id=dungeon_id,
                        cycle_key=daily_growth_day_window(timestamp)[0],
                    )
                    if adventure is None:
                        raise KeyError("今天尚未进入该奈菲亚")
                else:
                    raise ValueError("adventure_id 与 dungeon_id 至少提供一个")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return DungeonApplicationResult(self._view(adventure, context))

    async def choose_route(
        self,
        identity: UserIdentity,
        adventure_id: str,
        option_id: str,
        *,
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        return await self._transition(
            identity,
            adventure_id,
            lambda facade: facade.choose_route(adventure_id, option_id),
            now_ts=now_ts,
        )

    async def choose_risk(
        self,
        identity: UserIdentity,
        adventure_id: str,
        risk_id: str,
        *,
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        return await self._transition(
            identity,
            adventure_id,
            lambda facade: facade.choose_risk(adventure_id, risk_id),
            now_ts=now_ts,
        )

    async def fight(
        self,
        identity: UserIdentity,
        adventure_id: str,
        strategy: str = "",
        *,
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        timestamp = int(time.time() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                user, _ = await self.user_service.get_or_create_user_in_db(db, identity)
                adventure = await self.store.get_owned_in_db(
                    db, owner_pk=user.id, adventure_id=adventure_id
                )
                selected_strategy = str(strategy or adventure.strategy or "")
                context = await self._context_for_user_in_db(
                    db, user, selected_strategy
                )
                if selected_strategy != adventure.strategy:
                    adventure = replace(adventure, strategy=selected_strategy)
                facade = self._domain_facade(adventure)
                action = facade.fight(
                    adventure.adventure_id,
                    context["snapshot"],
                    profile_for_strategy(selected_strategy),
                    capabilities=context["capabilities"],
                    exploration_skills=context["exploration_skills"],
                    rare_equipment_find_bonus=context[
                        "rare_equipment_find_bonus"
                    ],
                    pve_stealth=context["pve_stealth"],
                )
                updated = action.adventure
                if action.simulation is None:
                    skill_growths = spell_growths = attribute_growths = ()
                else:
                    skill_growths, spell_growths, attribute_growths = (
                        await self._apply_growth_in_db(
                            db, user.id, action.simulation
                        )
                    )
                await self.store.save_in_db(
                    db,
                    owner_pk=user.id,
                    adventure=updated,
                    expected_version=adventure.version,
                    now_ts=timestamp,
                )
                rewards, current_user = await self.settlement.settle_in_db(
                    db,
                    user=user,
                    adventure=updated,
                    intents=action.newly_earned_intents,
                    now_ts=timestamp,
                )
                await db.commit()
                context["user"] = current_user
            except Exception:
                await db.rollback()
                raise
        return DungeonApplicationResult(
            self._view(updated, context),
            action.simulation,
            rewards,
            len(skill_growths),
            len(spell_growths),
            len(attribute_growths),
            action.narrative,
        )

    async def retreat(
        self,
        identity: UserIdentity,
        adventure_id: str,
        *,
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        timestamp = int(time.time() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                context = await self._context_in_db(db, identity, "")
                adventure = await self.store.get_owned_in_db(
                    db,
                    owner_pk=context["user"].id,
                    adventure_id=adventure_id,
                )
                facade = self._domain_facade(adventure)
                action = facade.retreat(adventure_id)
                await self.store.save_in_db(
                    db,
                    owner_pk=context["user"].id,
                    adventure=action.adventure,
                    expected_version=adventure.version,
                    now_ts=timestamp,
                )
                rewards, current_user = await self.settlement.settle_in_db(
                    db,
                    user=context["user"],
                    adventure=action.adventure,
                    intents=action.newly_earned_intents,
                    now_ts=timestamp,
                )
                await db.commit()
                context["user"] = current_user
            except Exception:
                await db.rollback()
                raise
        return DungeonApplicationResult(
            self._view(action.adventure, context), rewards=rewards
        )

    async def settle(
        self,
        identity: UserIdentity,
        adventure_id: str,
        intents: tuple[DungeonRewardIntent, ...] | None = None,
        *,
        now_ts: int | None = None,
    ) -> DungeonApplicationResult:
        """Idempotent recovery endpoint; supplied intents must match the snapshot."""
        timestamp = int(time.time() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                context = await self._context_in_db(db, identity, "")
                adventure = await self.store.get_owned_in_db(
                    db,
                    owner_pk=context["user"].id,
                    adventure_id=adventure_id,
                )
                requested = (
                    tuple(adventure.reward_intents)
                    if intents is None else tuple(intents)
                )
                rewards, current_user = await self.settlement.settle_in_db(
                    db,
                    user=context["user"],
                    adventure=adventure,
                    intents=requested,
                    now_ts=timestamp,
                )
                await db.commit()
                context["user"] = current_user
            except Exception:
                await db.rollback()
                raise
        return DungeonApplicationResult(self._view(adventure, context), rewards=rewards)

    async def _transition(
        self,
        identity: UserIdentity,
        adventure_id: str,
        action,
        *,
        now_ts: int | None,
    ) -> DungeonApplicationResult:
        timestamp = int(time.time() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                context = await self._context_in_db(db, identity, "")
                adventure = await self.store.get_owned_in_db(
                    db,
                    owner_pk=context["user"].id,
                    adventure_id=adventure_id,
                )
                facade = self._domain_facade(adventure)
                updated = action(facade)
                await self.store.save_in_db(
                    db,
                    owner_pk=context["user"].id,
                    adventure=updated,
                    expected_version=adventure.version,
                    now_ts=timestamp,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return DungeonApplicationResult(self._view(updated, context))

    async def _context_in_db(self, db, identity: UserIdentity, strategy: str):
        user, _ = await self.user_service.get_or_create_user_in_db(db, identity)
        return await self._context_for_user_in_db(db, user, strategy)

    async def _context_for_user_in_db(self, db, user, strategy: str):
        snapshot = await self.build_service.snapshot_in_db(db, user, strategy)
        skills = await self.skill_service.skills_in_db(db, user.id)
        spells = await self.spell_service.spells_in_db(db, user.id)
        return {
            "user": user,
            "snapshot": snapshot,
            "skills": skills,
            "spells": spells,
            "capabilities": tuple(sorted(
                set(spells)
                | set(snapshot.skill_ids)
                | set(
                    snapshot.equipment.exploration_capabilities
                    if snapshot.equipment else ()
                )
            )),
            "exploration_skills": {
                skill_id: int(value.level) for skill_id, value in skills.items()
            },
            "rare_equipment_find_bonus": (
                snapshot.equipment.rare_equipment_find_bonus
                if snapshot.equipment else 0.0
            ),
            "pve_stealth": (
                max(
                    0.0,
                    min(
                        0.50,
                        float(
                            getattr(
                                getattr(snapshot, "derived", None),
                                "pve_stealth",
                                0.0,
                            )
                        ),
                    ),
                )
            ),
        }

    async def _apply_growth_in_db(self, db, user_pk: int, simulation):
        skill_usage = self.skill_service.usage_from_simulation(simulation).get(
            int(user_pk), {}
        )
        spell_usage = self.spell_service.usage_from_simulation(simulation).get(
            int(user_pk), {}
        )
        skill_growths = await self.skill_service.apply_growth_in_db(
            db, int(user_pk), skill_usage, None
        )
        spell_growths = await self.spell_service.apply_growth_in_db(
            db, int(user_pk), spell_usage, None
        )
        attribute_growths = await self.attribute_service.apply_battle_growth_in_db(
            db, int(user_pk), skill_usage, None
        )
        return skill_growths, spell_growths, attribute_growths

    def _domain_facade(
        self, adventure: DungeonAdventure | None = None
    ) -> DungeonAdventureFacade:
        repository = InMemoryDungeonAdventureRepository()
        if adventure is not None:
            repository.add(adventure)
        return DungeonAdventureFacade(
            self.monster_build_service,
            self.dungeon_catalog,
            combat_engine=self.combat_engine,
            nefia_catalog=self.nefia_catalog,
            repository=repository,
        )

    def _view(self, adventure: DungeonAdventure, context) -> DungeonAdventureView:
        floor = adventure.current_floor
        routes: tuple[DungeonRouteView, ...] = ()
        if floor is not None:
            route_views: list[DungeonRouteView] = []
            for route in floor.routes:
                discovery_accessible = DungeonAdventureFacade.can_access_discovery(
                    route,
                    capabilities=context["capabilities"],
                    exploration_skills=context["exploration_skills"],
                )
                requires_combat = DungeonAdventureFacade.requires_combat(route)
                risk_views: list[DungeonRiskView] = []
                for risk in route.risk_choices:
                    hp_cost, mp_cost, mitigated = (
                        DungeonAdventureFacade.effective_entry_cost(
                            route,
                            risk,
                            discovery_accessible=discovery_accessible,
                        )
                    )
                    access_granted = (
                        None
                        if requires_combat
                        else DungeonAdventureFacade.event_access_granted(
                            route,
                            risk,
                            discovery_accessible=discovery_accessible,
                        )
                    )
                    reward_multiplier = (
                        DungeonAdventureFacade.effective_reward_multiplier(
                            route,
                            risk,
                            discovery_accessible=discovery_accessible,
                            access_granted=access_granted,
                            exploration_skills=context["exploration_skills"],
                        )
                    )
                    _find_chance_bonus, find_quality_bonus = (
                        DungeonAdventureFacade._rare_equipment_find_bonuses(
                            context["rare_equipment_find_bonus"]
                        )
                    )
                    quality_bonus = (
                        max(0.0, reward_multiplier - 1.0)
                        + find_quality_bonus
                    )
                    quality_policy = reward_quality_policy(quality_bonus)
                    risk_views.append(
                        DungeonRiskView(
                            risk_id=risk.risk_id,
                            name=risk.name,
                            description=risk.description,
                            monster_level=(
                                DungeonAdventureFacade.effective_monster_level(
                                    route, risk
                                )
                                if requires_combat
                                else 0
                            ),
                            monster_level_delta=risk.monster_level_delta,
                            reward_multiplier=reward_multiplier,
                            entry_hp_cost_ratio=hp_cost,
                            entry_mp_cost_ratio=mp_cost,
                            capability_mitigated=mitigated,
                            reward_quality_bonus=quality_bonus,
                            reward_effective_quality_bonus=(
                                quality_policy.effective_bonus
                            ),
                            reward_quality_progress=(
                                quality_policy.quality_progress
                            ),
                            reward_minimum_quality=(
                                quality_policy.minimum_quality
                            ),
                            reward_guaranteed_upgrades=(
                                quality_policy.guaranteed_upgrades
                            ),
                            reward_upgrade_chance=(
                                quality_policy.upgrade_chance
                            ),
                            rare_find_quality_bonus=find_quality_bonus,
                        )
                    )
                route_views.append(
                    DungeonRouteView(
                    option_id=route.option_id,
                    name=route.name,
                    description=route.description,
                    node_kind=route.node_kind,
                    monster_level=route.monster_level,
                    monster_rank=route.monster_rank,
                    environment_id=route.environment.combat_environment_id,
                    environment_name=route.environment.name,
                    affix_names=tuple(item.name for item in route.affixes),
                    terrain_name=route.terrain_name,
                    discovery_name=(route.discovery.name if route.discovery else ""),
                    discovery_accessible=discovery_accessible,
                    risk_choices=tuple(risk_views),
                    monster_name=(
                        self.monster_build_service.catalog.get(
                            route.monster_template_id
                        ).name
                        if requires_combat
                        else ""
                    ),
                    requires_combat=requires_combat,
                    )
                )
            routes = tuple(route_views)
        state = adventure.continuation_state
        return DungeonAdventureView(
            adventure_id=adventure.adventure_id,
            dungeon_id=adventure.dungeon_id,
            cycle_key=adventure.cycle_key,
            phase=adventure.phase,
            floor_number=min(adventure.floor_index + 1, len(adventure.floors)),
            floor_count=len(adventure.floors),
            completed_floors=adventure.completed_floors,
            difficulty=adventure.difficulty,
            strategy=adventure.strategy,
            routes=routes,
            selected_route_id=adventure.selected_route_id,
            selected_risk_id=adventure.selected_risk_id,
            hp_ratio=(state.hp_ratio if state else 1.0),
            mana_ratio=(state.mana_ratio if state else 1.0),
            stamina_ratio=(state.stamina_ratio if state else 1.0),
            equipment_misses=adventure.equipment_misses,
            spellbook_misses=adventure.spellbook_misses,
            version=adventure.version,
        )
