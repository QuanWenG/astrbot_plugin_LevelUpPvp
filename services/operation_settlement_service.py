"""Atomic settlement for deterministic operation reward intents.

``OperationService`` decides *whether* a bundle is claimable.  This service is
the small transactional boundary that turns its stable ``RewardIntent`` into
wallet currency and catalog equipment.  Retrying the same intent is safe: the
reward ledger key is unique and the complete grant is committed together.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

try:
    from ..models.operation import (
        OPERATION_REWARD_DEFINITIONS,
        OperationSettlementResult,
        RewardIntent,
        operation_reward_definition,
        operation_reward_intent,
        stable_operation_seed,
    )
    from .daily_growth_budget import allocate_daily_growth_in_db
    from .db import connect_db
except ImportError:
    from models.operation import (
        OPERATION_REWARD_DEFINITIONS,
        OperationSettlementResult,
        RewardIntent,
        operation_reward_definition,
        operation_reward_intent,
        stable_operation_seed,
    )
    from services.daily_growth_budget import allocate_daily_growth_in_db
    from services.db import connect_db


_ALLOWED_SOURCES = frozenset(
    definition.source for definition in OPERATION_REWARD_DEFINITIONS
)
_EPIC_QUALITIES = frozenset({"epic", "mythic"})
_MYTHIC_QUALITIES = frozenset({"mythic"})
EPIC_PITY_DRAWS = 10
MYTHIC_PITY_DRAWS = 40


class OperationSettlementService:
    """Settle operation rewards without leaking partial grants."""

    def __init__(self, db_path, user_service, equipment_service):
        self.db_path = db_path
        self.user_service = user_service
        self.equipment_service = equipment_service

    async def settle(
        self,
        *,
        user_pk: int,
        intent: RewardIntent,
    ) -> OperationSettlementResult:
        self._validate_intent(user_pk, intent)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._assert_reserved_intent_in_db(
                    db,
                    user_pk=int(user_pk),
                    intent=intent,
                )
                cursor = await db.execute(
                    "SELECT 1 FROM reward_ledger WHERE reward_key = ?",
                    (intent.reward_key,),
                )
                already_applied = await cursor.fetchone()
                await cursor.close()
                if already_applied:
                    await db.commit()
                    return OperationSettlementResult(
                        reward_key=intent.reward_key,
                        applied=False,
                    )

                user = await self.user_service.get_user_by_pk_in_db(
                    db,
                    int(user_pk),
                )
                now_ts = int(time.time())
                allocation = await allocate_daily_growth_in_db(
                    db,
                    user_pk=int(user_pk),
                    level=user.level,
                    requested_exp=intent.experience,
                    at=now_ts,
                )
                actual_exp = allocation.granted
                metadata = dict(intent.metadata)
                reason = json.dumps(
                    {
                        "reason": intent.reason,
                        "requested_experience": intent.experience,
                        "granted_experience": actual_exp,
                        "scrap": intent.scrap,
                        "loot_rolls": intent.loot_rolls,
                        "season_tokens": intent.season_tokens,
                        "metadata": metadata,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                await db.execute(
                    """
                    INSERT INTO reward_ledger (
                        reward_key, user_pk, battle_id, source, exp_gain,
                        currency_gain, reason, created_at_ts
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.reward_key,
                        int(user_pk),
                        intent.source,
                        actual_exp,
                        intent.scrap,
                        reason,
                        now_ts,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO workshop_wallet (
                        user_pk, scrap_balance, lifetime_earned,
                        lifetime_spent, season_tokens, updated_at_ts
                    ) VALUES (?, ?, ?, 0, ?, ?)
                    ON CONFLICT(user_pk) DO UPDATE SET
                        scrap_balance = scrap_balance + excluded.scrap_balance,
                        lifetime_earned = lifetime_earned + excluded.lifetime_earned,
                        season_tokens = season_tokens + excluded.season_tokens,
                        updated_at_ts = excluded.updated_at_ts
                    """,
                    (
                        int(user_pk),
                        intent.scrap,
                        intent.scrap,
                        intent.season_tokens,
                        now_ts,
                    ),
                )
                if actual_exp:
                    await self.user_service.add_exp_in_db(
                        db,
                        user,
                        actual_exp,
                    )
                equipment = await self._roll_loot_in_db(
                    db,
                    user,
                    intent,
                    now_ts,
                )
                await db.commit()
                return OperationSettlementResult(
                    reward_key=intent.reward_key,
                    applied=True,
                    experience=actual_exp,
                    scrap=intent.scrap,
                    season_tokens=intent.season_tokens,
                    equipment=tuple(equipment),
                )
            except Exception:
                await db.rollback()
                raise

    async def _roll_loot_in_db(self, db, user, intent, now_ts: int):
        if intent.loot_rolls <= 0:
            return []
        pool_id = f"operation:{intent.source}:v11"
        cursor = await db.execute(
            """
            SELECT epic_misses, legendary_misses, total_draws
            FROM loot_pity WHERE user_pk = ? AND pool_id = ?
            """,
            (user.id, pool_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        epic_misses = 0 if row is None else int(row["epic_misses"])
        mythic_misses = 0 if row is None else int(row["legendary_misses"])
        total_draws = 0 if row is None else int(row["total_draws"])
        results = []
        player_level = max(1, min(100, int(user.level)))
        level_min = max(1, player_level - 10)
        level_max = player_level
        for draw_index in range(intent.loot_rolls):
            force_mythic = mythic_misses >= MYTHIC_PITY_DRAWS - 1
            force_epic = epic_misses >= EPIC_PITY_DRAWS - 1
            accepted = None
            for attempt in range(2048):
                seed = stable_operation_seed(
                    intent.reward_key,
                    draw_index,
                    attempt,
                )
                candidate = self.equipment_service.generate_reward(
                    user.id,
                    3001,
                    4000,
                    level_min,
                    level_max,
                    seed=seed,
                )
                if int(candidate.owner_pk) != int(user.id):
                    raise RuntimeError("运营掉落生成了错误的装备归属")
                if not level_min <= int(candidate.item_level) <= level_max:
                    raise RuntimeError("运营掉落装备等级越过玩家等级边界")
                if force_mythic and candidate.quality not in _MYTHIC_QUALITIES:
                    continue
                if force_epic and candidate.quality not in _EPIC_QUALITIES:
                    continue
                accepted = candidate
                break
            if accepted is None:  # Catalog probability/schema corruption.
                raise RuntimeError("运营保底在装备目录中找不到目标品质")
            equipment_id = await self.equipment_service.insert_item_in_db(
                db,
                accepted,
            )
            accepted = replace(accepted, id=equipment_id)
            results.append(accepted)
            total_draws += 1
            if accepted.quality in _EPIC_QUALITIES:
                epic_misses = 0
            else:
                epic_misses += 1
            if accepted.quality in _MYTHIC_QUALITIES:
                mythic_misses = 0
            else:
                mythic_misses += 1
        await db.execute(
            """
            INSERT INTO loot_pity (
                user_pk, pool_id, epic_misses, legendary_misses,
                total_draws, updated_at_ts
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_pk, pool_id) DO UPDATE SET
                epic_misses = excluded.epic_misses,
                legendary_misses = excluded.legendary_misses,
                total_draws = excluded.total_draws,
                updated_at_ts = excluded.updated_at_ts
            """,
            (
                user.id,
                pool_id,
                epic_misses,
                mythic_misses,
                total_draws,
                now_ts,
            ),
        )
        return results

    async def _assert_reserved_intent_in_db(self, db, *, user_pk, intent) -> None:
        """Require an exact canonical intent backed by a claimed bundle row."""

        cursor = await db.execute(
            """
            SELECT group_id, period_kind, period_key, operation_key,
                   metadata_json
            FROM operation_progress
            WHERE user_pk = ? AND completed = 1 AND claimed = 1
              AND operation_key IN ('daily:choice-two', 'weekly:five-of-seven')
            """,
            (int(user_pk),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            try:
                definition = operation_reward_definition(row["period_kind"])
                metadata = json.loads(row["metadata_json"] or "{}")
                if not isinstance(metadata, dict):
                    continue
                ruleset_id = metadata.get("ruleset_id", "")
                if not isinstance(ruleset_id, str) or not ruleset_id.strip():
                    # Compatibility for reservations written by the first v11
                    # preview, whose intent carried the ruleset while its
                    # progress metadata did not yet persist it.
                    ruleset_id = dict(intent.metadata).get("ruleset_id", "")
                if row["operation_key"] != definition.bundle_key:
                    continue
                expected = operation_reward_intent(
                    period_kind=definition.period_kind,
                    user_pk=int(user_pk),
                    group_id=str(row["group_id"]),
                    period_key=str(row["period_key"]),
                    ruleset_id=ruleset_id,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if expected == intent:
                return
        raise ValueError("operation reward intent has no matching claimed reservation")

    @staticmethod
    def _validate_intent(user_pk: int, intent: RewardIntent) -> None:
        if isinstance(user_pk, bool) or not isinstance(user_pk, int) or user_pk <= 0:
            raise ValueError("user_pk must be a positive integer")
        if not isinstance(intent, RewardIntent):
            raise TypeError("intent must be a RewardIntent")
        if intent.source not in _ALLOWED_SOURCES:
            raise ValueError("unsupported operation reward source")
        if (
            not isinstance(intent.reward_key, str)
            or not intent.reward_key.startswith("operation:")
            or len(intent.reward_key) > 200
        ):
            raise ValueError("operation reward key has an invalid namespace")
        if not isinstance(intent.reason, str) or not intent.reason.strip():
            raise ValueError("operation reward reason must not be empty")
        for name in ("experience", "scrap", "loot_rolls", "season_tokens"):
            value = getattr(intent, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(intent.metadata, tuple):
            raise ValueError("metadata must be an immutable tuple")
        metadata_keys = []
        for item in intent.metadata:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
            ):
                raise ValueError("metadata entries must be string pairs")
            metadata_keys.append(item[0])
        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError("metadata keys must be unique")


__all__ = [
    "EPIC_PITY_DRAWS",
    "MYTHIC_PITY_DRAWS",
    "OperationSettlementService",
]
