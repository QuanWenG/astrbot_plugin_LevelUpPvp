"""Deterministic, anti-spam rewards discovered through ordinary group chat.

This module owns no AstrBot concepts.  A transport adapter passes a
``ChatMessageContext`` to :class:`ChatActivityService`; valid messages reserve a
stable intent.  :class:`ChatActivitySettlementService` verifies that reservation
and commits XP/items/books in one SQLite transaction.

Player-facing pacing (at most sixteen eligible rolls per active day):

* a visible micro-growth moment on 22% of eligible messages, capped at 12% of
  the shared character daily XP budget;
* equipment starts at 1.5% (4% before the first drop), with soft/hard pity at
  18/50 messages (8/18 before the first drop);
* spellbooks start at 1% (2.5% before the first), with soft/hard pity at 25/65
  messages (12/28 before the first drop);
* no more than one equipment and one spellbook drop per active day, and chat
  equipment is at least excellent to avoid an inventory full of junk.

Messages inside the reward cooldown can still be normal conversation, but they
do not consume pity or a reward roll.  Rejected/no-result messages are intended
to stay completely silent at the handler layer.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

try:
    from ..models.chat_activity import (
        CHAT_ACTIVITY_RULESET_ID,
        ChatActivityDecision,
        ChatActivitySettlementResult,
        ChatMessageContext,
        ChatRewardIntent,
        stable_chat_seed,
    )
    from .combat_random import KeyedEntropy
    from .daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
        daily_growth_exp_earned_in_db,
    )
    from .db import connect_db
    from .progression_rules import level_daily_exp_budget
except ImportError:
    from models.chat_activity import (
        CHAT_ACTIVITY_RULESET_ID,
        ChatActivityDecision,
        ChatActivitySettlementResult,
        ChatMessageContext,
        ChatRewardIntent,
        stable_chat_seed,
    )
    from services.combat_random import KeyedEntropy
    from services.daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
        daily_growth_exp_earned_in_db,
    )
    from services.db import connect_db
    from services.progression_rules import level_daily_exp_budget


_ACTIVITY_ZONE = ZoneInfo("Asia/Hong_Kong")
_SEMANTIC_UNIT_RE = re.compile(r"[0-9a-z\u3400-\u9fff]", re.IGNORECASE)
_COMMAND_PREFIXES = ("/", "／", "!", "！", ".", "。")
_SIGNED_SQLITE_SEED_MASK = (1 << 63) - 1
_QUALITY_RANK = {"common": 0, "excellent": 1, "rare": 2, "epic": 3, "mythic": 4}
_QUALITY_LABEL = {
    "common": "普通",
    "excellent": "优秀",
    "rare": "稀有",
    "epic": "史诗",
    "mythic": "神话",
}


@dataclass(frozen=True)
class ChatActivityPolicy:
    ruleset_id: str = CHAT_ACTIVITY_RULESET_ID
    reset_hour: int = 4
    minimum_semantic_units: int = 2
    minimum_valid_interval_seconds: int = 8
    burst_window_seconds: int = 120
    burst_message_limit: int = 5
    duplicate_window_seconds: int = 6 * 60 * 60
    reward_cooldown_seconds: int = 90
    daily_valid_message_limit: int = 60
    daily_reward_roll_limit: int = 16
    daily_exp_event_limit: int = 3
    chat_exp_budget_share: float = 0.12
    exp_probability: float = 0.22
    exp_per_event_budget_share: float = 0.04
    daily_equipment_limit: int = 1
    daily_spellbook_limit: int = 1

    def __post_init__(self) -> None:
        if not self.ruleset_id or self.ruleset_id.casefold() == "latest":
            raise ValueError("chat ruleset_id must be an exact immutable id")
        if not 0 <= self.reset_hour <= 23:
            raise ValueError("reset_hour must be between 0 and 23")
        for name in (
            "minimum_semantic_units",
            "minimum_valid_interval_seconds",
            "burst_window_seconds",
            "burst_message_limit",
            "duplicate_window_seconds",
            "reward_cooldown_seconds",
            "daily_valid_message_limit",
            "daily_reward_roll_limit",
            "daily_exp_event_limit",
            "daily_equipment_limit",
            "daily_spellbook_limit",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "chat_exp_budget_share",
            "exp_probability",
            "exp_per_event_budget_share",
        ):
            if not 0 < float(getattr(self, name)) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")


@dataclass(frozen=True)
class _SpellCandidate:
    spell_id: str
    name: str
    minimum_level: int


# Curated instead of selecting the whole catalog: early books are readable goals,
# while later levels slowly reveal stranger magic.  A duplicate book remains
# useful because SpellService reading restores that spell's potential.
_SPELL_POOL = (
    _SpellCandidate("magic_arrow", "魔法箭", 1),
    _SpellCandidate("minor_heal", "轻伤治疗", 1),
    _SpellCandidate("armor_spell", "护甲术", 1),
    _SpellCandidate("hero", "英雄", 1),
    _SpellCandidate("web", "蛛网术", 5),
    _SpellCandidate("shadow_arrow", "暗影箭", 8),
    _SpellCandidate("fire_ray", "火焰射线", 10),
    _SpellCandidate("ice_ray", "冰冻射线", 10),
    _SpellCandidate("regeneration", "再生", 15),
    _SpellCandidate("lightning_ray", "雷光射线", 20),
    _SpellCandidate("blink", "闪现", 20),
    _SpellCandidate("holy_shield", "神圣之盾", 25),
    _SpellCandidate("confusion_spell", "困惑咒文", 30),
    _SpellCandidate("hell_breath", "地狱吐息", 35),
    _SpellCandidate("healing_rain", "治愈之雨", 40),
    _SpellCandidate("mana_storm", "魔力风暴", 50),
)


_XP_STORIES = (
    "闲谈间，你忽然把一段冒险心得串了起来。",
    "你从群友的一句话里悟到了新的战斗门道。",
    "聊着聊着，过去一次失误的答案突然清晰了。",
    "一阵随口的讨论，让你的冒险直觉悄悄生长。",
)
_EQUIPMENT_STORIES = (
    "路过的行商听得入神，临走前塞给你一件蒙尘的装备。",
    "窗外传来一声轻响，一只白猫把闪亮的旧物推到你脚边。",
    "你们的话题引来一位匿名冒险者，他留下装备后匆匆离开。",
    "群聊正热闹时，一只迷路的快递魔像把包裹交到了你手里。",
)
_BOOK_STORIES = (
    "一句无心之言唤醒了空气里的符文，一册魔法书落在你面前。",
    "你翻看旧消息时，字缝里竟滑出一本还带余温的魔法书。",
    "远处的风把几页咒文卷成书册，恰好停在你的手边。",
    "群友提到的怪谈突然应验：书架深处多出一本陌生魔法书。",
)
_DOUBLE_DROP_STORIES = (
    "一扇只开了片刻的异界小门吐出装备与魔法书，随后悄然合拢。",
    "你们的闲谈似乎取悦了幸运神，两个古老包裹同时落到桌上。",
)


class EquipmentDropPort(Protocol):
    async def grant_in_db(
        self,
        db,
        *,
        user_pk: int,
        player_level: int,
        seed: int,
    ) -> object:
        ...


class SpellbookDropPort(Protocol):
    async def grant_in_db(
        self,
        db,
        *,
        user_pk: int,
        spell_id: str,
        seed: int,
    ) -> object:
        ...


class EquipmentServiceDropAdapter:
    """Adapt the existing catalog service to one useful chat drop."""

    def __init__(self, equipment_service) -> None:
        self._equipment_service = equipment_service

    async def grant_in_db(self, db, *, user_pk, player_level, seed):
        level = max(1, min(100, int(player_level)))
        level_min = max(1, level - 5)
        accepted = None
        # Chat surprises are rarer than operation loot, so common-quality items
        # are rerolled instead of filling inventory with automatic salvage.
        for attempt in range(512):
            candidate = self._equipment_service.generate_reward(
                int(user_pk),
                3001,
                4000,
                level_min,
                level,
                seed=stable_chat_seed(seed, "equipment-candidate", attempt)
                & _SIGNED_SQLITE_SEED_MASK,
            )
            if _QUALITY_RANK.get(str(candidate.quality), -1) >= _QUALITY_RANK["excellent"]:
                accepted = candidate
                break
        if accepted is None:
            raise RuntimeError("聊天掉落在装备目录中找不到优秀及以上品质")
        equipment_id = await self._equipment_service.insert_item_in_db(db, accepted)
        try:
            return replace(accepted, id=equipment_id)
        except TypeError:  # pragma: no cover - compatibility with mutable adapters
            accepted.id = equipment_id
            return accepted


class SpellServiceBookAdapter:
    """Adapt ``SpellService.grant_book_in_db`` to the domain port."""

    def __init__(self, spell_service) -> None:
        self._spell_service = spell_service

    async def grant_in_db(self, db, *, user_pk, spell_id, seed):
        return await self._spell_service.grant_book_in_db(
            db,
            int(user_pk),
            str(spell_id),
            1,
            "chat_serendipity",
            int(seed),
        )


def chat_activity_day_window(
    timestamp: int,
    *,
    reset_hour: int = 4,
) -> tuple[str, int, int]:
    """Return the 04:00-local active-day key and exact Unix bounds."""

    return daily_growth_day_window(int(timestamp), reset_hour=int(reset_hour))


def _normalise_content(content: str) -> tuple[str, str]:
    value = unicodedata.normalize("NFKC", str(content)).casefold()
    value = " ".join(value.split())
    fingerprint = f"{stable_chat_seed('content', value):016x}"
    return value, fingerprint


def _intent_json(intent: ChatRewardIntent) -> str:
    return json.dumps(
        asdict(intent),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _intent_from_json(payload: str) -> ChatRewardIntent | None:
    if not payload or payload == "{}":
        return None
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("stored chat reward intent is not an object")
    return ChatRewardIntent(**raw)


def format_chat_activity_settlement(
    result: ChatActivitySettlementResult,
    *,
    username: str = "",
) -> str:
    """Render one committed result as a compact world event, or silence.

    The transport layer should send this text only when non-empty.  Keeping the
    wording here prevents each handler from drifting back to noisy
    ``system +5 XP`` notifications.
    """

    if not isinstance(result, ChatActivitySettlementResult):
        raise TypeError("result must be a ChatActivitySettlementResult")
    if not result.should_announce:
        return ""

    has_drop = result.equipment is not None or result.spellbook is not None
    display_name = " ".join(str(username or "").split())
    lines = []
    if has_drop and display_name:
        lines.append(f"{display_name}：")
    lines.append(result.story_text or "日常的闲谈里，似乎发生了一件小小的奇遇。")
    if result.experience > 0:
        lines.append(f"你的冒险见识增长了 {result.experience} 点。")
    if result.equipment is not None:
        item = result.equipment
        if isinstance(item, dict):
            name = str(item.get("name", "一件神秘装备"))
            quality = str(item.get("quality", "excellent"))
            item_level = item.get("item_level", item.get("level"))
        else:
            name = str(getattr(item, "name", "一件神秘装备"))
            quality = str(getattr(item, "quality", "excellent"))
            item_level = getattr(item, "item_level", None)
        level_text = "" if item_level is None else f" · Lv.{int(item_level)}"
        lines.append(
            f"你收下了【{_QUALITY_LABEL.get(quality, quality)}】{name}{level_text}。"
        )
    if result.spellbook is not None:
        book_name = result.spell_name or "未知咒文"
        lines.append(
            f"你拾得《{book_name}》魔法书；即使已经会了，回读也能恢复魔法潜力。"
        )
    if result.level_ups:
        final_level = getattr(result.level_ups[-1], "to_level", None)
        if final_level is not None:
            lines.append(f"这份积累让你升到了 Lv.{int(final_level)}。")
    return "\n".join(lines)


async def shared_daily_exp_earned_in_db(
    db,
    *,
    user_pk: int,
    day_key: str,
    day_start_ts: int,
    day_end_ts: int,
) -> int:
    """Return all progression XP already committed inside one active day.

    This is public so check-in, PvP, dungeon and chat composition roots can use
    the same accounting query instead of inventing source-specific caps.  It
    counts check-in rows plus every positive ``reward_ledger`` source; callers
    still own their source-specific limits and pending reservations.
    """

    return await daily_growth_exp_earned_in_db(
        db,
        user_pk=int(user_pk),
        day_key=str(day_key),
        day_start_ts=int(day_start_ts),
        day_end_ts=int(day_end_ts),
    )


class ChatActivityService:
    """Reserve deterministic chat rewards and anti-spam state."""

    def __init__(self, db_path: str, policy: ChatActivityPolicy | None = None):
        self.db_path = db_path
        self.policy = policy or ChatActivityPolicy()

    async def prepare_message(
        self,
        context: ChatMessageContext,
    ) -> ChatActivityDecision:
        self._validate_context(context)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                replay = await self._load_event_in_db(db, context)
                if replay is not None:
                    await db.commit()
                    return replace(replay, replayed=True)

                day_key, day_start_ts, day_end_ts = chat_activity_day_window(
                    context.occurred_at_ts,
                    reset_hour=self.policy.reset_hour,
                )
                basic_rejection = self._basic_rejection(context)
                if basic_rejection:
                    result = ChatActivityDecision(
                        context.event_key,
                        False,
                        basic_rejection,
                        day_key,
                    )
                    await self._store_event_in_db(db, context, result, "")
                    await db.commit()
                    return result

                cursor = await db.execute(
                    "SELECT level, group_id FROM users WHERE id = ?",
                    (int(context.user_pk),),
                )
                user_row = await cursor.fetchone()
                await cursor.close()
                if user_row is None:
                    raise ValueError("chat activity requires an existing user")
                if str(user_row["group_id"] or "") != str(context.group_id):
                    raise ValueError("chat message group does not match user ownership")
                player_level = max(1, min(100, int(user_row["level"])))

                normalised, fingerprint = _normalise_content(context.content)
                content_rejection = self._content_rejection(normalised)
                if content_rejection:
                    result = ChatActivityDecision(
                        context.event_key,
                        False,
                        content_rejection,
                        day_key,
                    )
                    await self._store_event_in_db(db, context, result, fingerprint)
                    await db.commit()
                    return result

                spam_reason = await self._spam_rejection_in_db(
                    db,
                    context,
                    fingerprint,
                )
                if spam_reason:
                    result = ChatActivityDecision(
                        context.event_key,
                        False,
                        spam_reason,
                        day_key,
                    )
                    await self._store_event_in_db(db, context, result, fingerprint)
                    await db.commit()
                    return result

                await db.execute(
                    """
                    INSERT OR IGNORE INTO chat_activity_daily (
                        user_pk, group_id, day_key, updated_at_ts
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(context.user_pk),
                        str(context.group_id),
                        day_key,
                        int(context.occurred_at_ts),
                    ),
                )
                daily = await self._daily_row_in_db(db, context, day_key)
                if int(daily["valid_messages"]) >= self.policy.daily_valid_message_limit:
                    result = ChatActivityDecision(
                        context.event_key,
                        False,
                        "daily_valid_limit",
                        day_key,
                    )
                    await self._store_event_in_db(db, context, result, fingerprint)
                    await db.commit()
                    return result

                valid_index = int(daily["valid_messages"]) + 1
                reward_roll_index = None
                reason = "accepted_no_roll"
                intent = None
                equipment_probability = 0.0
                spellbook_probability = 0.0
                can_roll = (
                    int(daily["reward_rolls"]) < self.policy.daily_reward_roll_limit
                    and (
                        int(daily["last_reward_roll_ts"]) <= 0
                        or int(context.occurred_at_ts)
                        - int(daily["last_reward_roll_ts"])
                        >= self.policy.reward_cooldown_seconds
                    )
                )
                if can_roll:
                    reward_roll_index = int(daily["reward_rolls"]) + 1
                    (
                        intent,
                        equipment_probability,
                        spellbook_probability,
                    ) = await self._build_intent_in_db(
                        db,
                        context=context,
                        day_key=day_key,
                        day_start_ts=day_start_ts,
                        day_end_ts=day_end_ts,
                        player_level=player_level,
                        valid_message_index=valid_index,
                        daily=daily,
                    )
                    reason = "reward_reserved" if intent else "accepted_no_reward"
                    await db.execute(
                        """
                        UPDATE chat_activity_daily
                        SET valid_messages = valid_messages + 1,
                            reward_rolls = reward_rolls + 1,
                            last_reward_roll_ts = ?, updated_at_ts = ?
                        WHERE user_pk = ? AND group_id = ? AND day_key = ?
                        """,
                        (
                            int(context.occurred_at_ts),
                            int(context.occurred_at_ts),
                            int(context.user_pk),
                            str(context.group_id),
                            day_key,
                        ),
                    )
                else:
                    if int(daily["reward_rolls"]) >= self.policy.daily_reward_roll_limit:
                        reason = "accepted_daily_roll_limit"
                    else:
                        reason = "accepted_reward_cooldown"
                    await db.execute(
                        """
                        UPDATE chat_activity_daily
                        SET valid_messages = valid_messages + 1, updated_at_ts = ?
                        WHERE user_pk = ? AND group_id = ? AND day_key = ?
                        """,
                        (
                            int(context.occurred_at_ts),
                            int(context.user_pk),
                            str(context.group_id),
                            day_key,
                        ),
                    )

                result = ChatActivityDecision(
                    event_key=context.event_key,
                    accepted=True,
                    reason=reason,
                    day_key=day_key,
                    valid_message_index=valid_index,
                    reward_roll_index=reward_roll_index,
                    intent=intent,
                    equipment_probability=equipment_probability,
                    spellbook_probability=spellbook_probability,
                )
                await self._store_event_in_db(db, context, result, fingerprint)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    async def pending_intents(
        self,
        *,
        user_pk: int | None = None,
        limit: int = 100,
    ) -> tuple[ChatRewardIntent, ...]:
        """Expose crash-recovery work without granting anything."""

        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        where = "settled = 0 AND reward_key <> ''"
        params: list[object] = []
        if user_pk is not None:
            if isinstance(user_pk, bool) or int(user_pk) <= 0:
                raise ValueError("user_pk must be positive")
            where += " AND user_pk = ?"
            params.append(int(user_pk))
        params.append(int(limit))
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT intent_json FROM chat_activity_events
                WHERE {where}
                ORDER BY occurred_at_ts, event_key
                LIMIT ?
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return tuple(
            intent
            for intent in (_intent_from_json(row["intent_json"]) for row in rows)
            if intent is not None
        )

    async def pending_intent_page(
        self,
        *,
        user_pk: int | None = None,
        limit: int = 100,
        after_reward_key: str | None = None,
        on_decode_error=None,
    ) -> tuple[str | None, tuple[ChatRewardIntent, ...]]:
        """Return one keyset page and its raw-row cursor for startup recovery."""

        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        where = "settled = 0 AND reward_key <> ''"
        params: list[object] = []
        if user_pk is not None:
            if isinstance(user_pk, bool) or int(user_pk) <= 0:
                raise ValueError("user_pk must be positive")
            where += " AND user_pk = ?"
            params.append(int(user_pk))
        if after_reward_key is not None:
            after_reward_key = str(after_reward_key)
            if not after_reward_key or len(after_reward_key) > 200:
                raise ValueError(
                    "after_reward_key must contain 1-200 characters"
                )
            where += " AND reward_key > ?"
            params.append(after_reward_key)
        params.append(int(limit))
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT reward_key, intent_json FROM chat_activity_events
                WHERE {where}
                ORDER BY reward_key
                LIMIT ?
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        if not rows:
            return None, ()
        intents: list[ChatRewardIntent] = []
        for row in rows:
            try:
                intent = _intent_from_json(row["intent_json"])
            except Exception as exc:
                if on_decode_error is None:
                    raise
                on_decode_error(str(row["reward_key"]), exc)
                continue
            if intent is not None:
                intents.append(intent)
        return str(rows[-1]["reward_key"]), tuple(intents)

    async def recover_pending_intents(
        self,
        settlement_service,
        *,
        batch_size: int = 100,
        on_error=None,
    ) -> tuple[int, int]:
        """Attempt every pending reservation once without a poisoned-row loop."""

        cursor = None
        recovered = 0
        failed = 0

        def report_decode_failure(reward_key, exc):
            nonlocal failed
            failed += 1
            if on_error is not None:
                on_error(reward_key, exc)

        while True:
            next_cursor, intents = await self.pending_intent_page(
                limit=batch_size,
                after_reward_key=cursor,
                on_decode_error=report_decode_failure,
            )
            if next_cursor is None:
                break
            if cursor is not None and next_cursor <= cursor:
                raise RuntimeError("chat recovery cursor did not advance")
            for intent in intents:
                try:
                    await settlement_service.settle(intent)
                    recovered += 1
                except Exception as exc:
                    failed += 1
                    if on_error is not None:
                        on_error(intent, exc)
            cursor = next_cursor
        return recovered, failed

    async def _build_intent_in_db(
        self,
        db,
        *,
        context,
        day_key,
        day_start_ts,
        day_end_ts,
        player_level,
        valid_message_index,
        daily,
    ):
        entropy = KeyedEntropy(
            self.policy.ruleset_id,
            f"{context.group_id}|{day_key}|{context.user_pk}|{valid_message_index}",
        )
        cursor = await db.execute(
            """
            SELECT equipment_misses, spellbook_misses,
                   equipment_drops_total, spellbook_drops_total
            FROM chat_activity_pity WHERE user_pk = ?
            """,
            (int(context.user_pk),),
        )
        pity = await cursor.fetchone()
        await cursor.close()
        equipment_misses = 0 if pity is None else int(pity["equipment_misses"])
        spellbook_misses = 0 if pity is None else int(pity["spellbook_misses"])
        equipment_total = 0 if pity is None else int(pity["equipment_drops_total"])
        spellbook_total = 0 if pity is None else int(pity["spellbook_drops_total"])

        equipment_probability = 0.0
        equipment_drop = False
        if int(daily["equipment_drops"]) < self.policy.daily_equipment_limit:
            equipment_probability = self._equipment_probability(
                equipment_misses,
                first_drop=equipment_total == 0,
            )
            equipment_drop = entropy.random(
                stream="equipment-drop",
                actor=context.user_pk,
                action_seq=valid_message_index,
            ) < equipment_probability
            equipment_misses = 0 if equipment_drop else equipment_misses + 1
            if equipment_drop:
                equipment_total += 1

        spellbook_probability = 0.0
        spellbook_drop = False
        if int(daily["spellbook_drops"]) < self.policy.daily_spellbook_limit:
            spellbook_probability = self._spellbook_probability(
                spellbook_misses,
                first_drop=spellbook_total == 0,
            )
            spellbook_drop = entropy.random(
                stream="spellbook-drop",
                actor=context.user_pk,
                action_seq=valid_message_index,
            ) < spellbook_probability
            spellbook_misses = 0 if spellbook_drop else spellbook_misses + 1
            if spellbook_drop:
                spellbook_total += 1

        experience = 0
        if (
            int(daily["exp_events"]) < self.policy.daily_exp_event_limit
            and entropy.random(
                stream="experience",
                actor=context.user_pk,
                action_seq=valid_message_index,
            ) < self.policy.exp_probability
        ):
            budget = level_daily_exp_budget(player_level)
            chat_cap = max(1, math.floor(budget * self.policy.chat_exp_budget_share))
            shared_earned = await shared_daily_exp_earned_in_db(
                db,
                user_pk=int(context.user_pk),
                day_key=day_key,
                day_start_ts=day_start_ts,
                day_end_ts=day_end_ts,
            )
            reserved = int(daily["reserved_exp"])
            remaining_shared = max(0, budget - shared_earned - reserved)
            remaining_chat = max(
                0,
                chat_cap - int(daily["awarded_exp"]) - reserved,
            )
            proposed = max(
                1,
                math.floor(budget * self.policy.exp_per_event_budget_share),
            )
            experience = min(proposed, remaining_shared, remaining_chat)

        equipment_seed = None
        if equipment_drop:
            equipment_seed = stable_chat_seed(
                self.policy.ruleset_id,
                context.group_id,
                day_key,
                context.user_pk,
                valid_message_index,
                "equipment-grant",
            ) & _SIGNED_SQLITE_SEED_MASK

        spell = None
        spellbook_seed = None
        if spellbook_drop:
            available = tuple(
                candidate
                for candidate in _SPELL_POOL
                if candidate.minimum_level <= player_level
            ) or _SPELL_POOL[:1]
            cursor = await db.execute(
                "SELECT spell_id FROM user_spells WHERE user_pk = ?",
                (int(context.user_pk),),
            )
            learned_spell_ids = {
                str(row["spell_id"]) for row in await cursor.fetchall()
            }
            await cursor.close()
            cursor = await db.execute(
                """
                SELECT spell_id FROM spellbook_items
                WHERE owner_pk = ? AND quantity > 0
                """,
                (int(context.user_pk),),
            )
            held_spell_ids = {
                str(row["spell_id"]) for row in await cursor.fetchall()
            }
            await cursor.close()
            novel = tuple(
                candidate
                for candidate in available
                if candidate.spell_id not in learned_spell_ids
                and candidate.spell_id not in held_spell_ids
            )
            learned_repeats = tuple(
                candidate
                for candidate in available
                if candidate.spell_id in learned_spell_ids
                and candidate.spell_id not in held_spell_ids
            )
            novelty_roll = entropy.random(
                stream="spellbook-novelty",
                actor=context.user_pk,
                action_seq=valid_message_index,
            )
            if novel and (not learned_repeats or novelty_roll < 0.85):
                selection_pool = novel
            elif learned_repeats:
                selection_pool = learned_repeats
            else:
                unstacked = tuple(
                    candidate
                    for candidate in available
                    if candidate.spell_id not in held_spell_ids
                )
                selection_pool = unstacked or available
            spell = entropy.choice(
                selection_pool,
                stream="spellbook-kind",
                actor=context.user_pk,
                action_seq=valid_message_index,
            )
            spellbook_seed = stable_chat_seed(
                self.policy.ruleset_id,
                context.group_id,
                day_key,
                context.user_pk,
                valid_message_index,
                "spellbook-grant",
            ) & _SIGNED_SQLITE_SEED_MASK

        await db.execute(
            """
            INSERT INTO chat_activity_pity (
                user_pk, equipment_misses, spellbook_misses,
                equipment_drops_total, spellbook_drops_total, updated_at_ts
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_pk) DO UPDATE SET
                equipment_misses = excluded.equipment_misses,
                spellbook_misses = excluded.spellbook_misses,
                equipment_drops_total = excluded.equipment_drops_total,
                spellbook_drops_total = excluded.spellbook_drops_total,
                updated_at_ts = excluded.updated_at_ts
            """,
            (
                int(context.user_pk),
                equipment_misses,
                spellbook_misses,
                equipment_total,
                spellbook_total,
                int(context.occurred_at_ts),
            ),
        )
        if equipment_drop:
            await db.execute(
                """
                UPDATE chat_activity_daily SET equipment_drops = equipment_drops + 1
                WHERE user_pk = ? AND group_id = ? AND day_key = ?
                """,
                (int(context.user_pk), str(context.group_id), day_key),
            )
        if spellbook_drop:
            await db.execute(
                """
                UPDATE chat_activity_daily SET spellbook_drops = spellbook_drops + 1
                WHERE user_pk = ? AND group_id = ? AND day_key = ?
                """,
                (int(context.user_pk), str(context.group_id), day_key),
            )
        if experience > 0:
            await db.execute(
                """
                UPDATE chat_activity_daily
                SET reserved_exp = reserved_exp + ?, exp_events = exp_events + 1
                WHERE user_pk = ? AND group_id = ? AND day_key = ?
                """,
                (experience, int(context.user_pk), str(context.group_id), day_key),
            )

        if experience <= 0 and not equipment_drop and not spellbook_drop:
            return None, equipment_probability, spellbook_probability

        reward_hash = stable_chat_seed(
            self.policy.ruleset_id,
            context.group_id,
            day_key,
            context.user_pk,
            valid_message_index,
            "reward-key",
        )
        story_id, story_text = self._story(
            entropy,
            context.user_pk,
            valid_message_index,
            experience > 0,
            equipment_drop,
            spellbook_drop,
        )
        intent = ChatRewardIntent(
            reward_key=f"chat:{day_key}:{reward_hash:016x}",
            ruleset_id=self.policy.ruleset_id,
            group_id=str(context.group_id),
            day_key=day_key,
            user_pk=int(context.user_pk),
            valid_message_index=valid_message_index,
            experience=experience,
            equipment_seed=equipment_seed,
            spell_id=None if spell is None else spell.spell_id,
            spell_name=None if spell is None else spell.name,
            spellbook_seed=spellbook_seed,
            story_id=story_id,
            story_text=story_text,
        )
        return intent, equipment_probability, spellbook_probability

    async def _spam_rejection_in_db(self, db, context, fingerprint):
        cursor = await db.execute(
            """
            SELECT occurred_at_ts FROM chat_activity_events
            WHERE user_pk = ? AND group_id = ? AND accepted = 1
              AND occurred_at_ts <= ?
            ORDER BY occurred_at_ts DESC LIMIT 1
            """,
            (int(context.user_pk), str(context.group_id), int(context.occurred_at_ts)),
        )
        last = await cursor.fetchone()
        await cursor.close()
        if (
            last is not None
            and int(context.occurred_at_ts) - int(last["occurred_at_ts"])
            < self.policy.minimum_valid_interval_seconds
        ):
            return "too_fast"

        cursor = await db.execute(
            """
            SELECT COUNT(*) AS count FROM chat_activity_events
            WHERE user_pk = ? AND group_id = ? AND accepted = 1
              AND occurred_at_ts > ? AND occurred_at_ts <= ?
            """,
            (
                int(context.user_pk),
                str(context.group_id),
                int(context.occurred_at_ts) - self.policy.burst_window_seconds,
                int(context.occurred_at_ts),
            ),
        )
        recent_count = int((await cursor.fetchone())["count"])
        await cursor.close()
        if recent_count >= self.policy.burst_message_limit:
            return "burst_suppressed"

        cursor = await db.execute(
            """
            SELECT 1 FROM chat_activity_events
            WHERE user_pk = ? AND group_id = ? AND accepted = 1
              AND content_fingerprint = ?
              AND occurred_at_ts > ? AND occurred_at_ts <= ?
            LIMIT 1
            """,
            (
                int(context.user_pk),
                str(context.group_id),
                fingerprint,
                int(context.occurred_at_ts) - self.policy.duplicate_window_seconds,
                int(context.occurred_at_ts),
            ),
        )
        duplicate = await cursor.fetchone()
        await cursor.close()
        return "duplicate_content" if duplicate is not None else ""

    async def _load_event_in_db(self, db, context):
        cursor = await db.execute(
            """
            SELECT user_pk, group_id, accepted, decision_reason, day_key,
                   valid_message_index, reward_roll_index, intent_json,
                   equipment_probability, spellbook_probability
            FROM chat_activity_events WHERE event_key = ?
            """,
            (context.event_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        if int(row["user_pk"]) != int(context.user_pk) or str(row["group_id"]) != str(context.group_id):
            raise ValueError("chat event key was already used by another owner")
        return ChatActivityDecision(
            event_key=context.event_key,
            accepted=bool(row["accepted"]),
            reason=str(row["decision_reason"]),
            day_key=str(row["day_key"]),
            valid_message_index=(
                None if row["valid_message_index"] is None
                else int(row["valid_message_index"])
            ),
            reward_roll_index=(
                None if row["reward_roll_index"] is None
                else int(row["reward_roll_index"])
            ),
            intent=_intent_from_json(str(row["intent_json"])),
            equipment_probability=float(row["equipment_probability"]),
            spellbook_probability=float(row["spellbook_probability"]),
        )

    @staticmethod
    async def _store_event_in_db(db, context, result, fingerprint):
        intent_payload = "{}" if result.intent is None else _intent_json(result.intent)
        await db.execute(
            """
            INSERT INTO chat_activity_events (
                event_key, user_pk, group_id, occurred_at_ts,
                content_fingerprint, accepted, decision_reason, day_key,
                valid_message_index, reward_roll_index, reward_key,
                intent_json, equipment_probability, spellbook_probability,
                settled, actual_exp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                context.event_key,
                int(context.user_pk),
                str(context.group_id),
                int(context.occurred_at_ts),
                str(fingerprint),
                int(result.accepted),
                result.reason,
                result.day_key,
                result.valid_message_index,
                result.reward_roll_index,
                "" if result.intent is None else result.intent.reward_key,
                intent_payload,
                float(result.equipment_probability),
                float(result.spellbook_probability),
            ),
        )

    @staticmethod
    async def _daily_row_in_db(db, context, day_key):
        cursor = await db.execute(
            """
            SELECT * FROM chat_activity_daily
            WHERE user_pk = ? AND group_id = ? AND day_key = ?
            """,
            (int(context.user_pk), str(context.group_id), str(day_key)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:  # pragma: no cover - protected by INSERT OR IGNORE
            raise RuntimeError("chat activity daily state disappeared")
        return row

    @staticmethod
    def _equipment_probability(misses: int, *, first_drop: bool) -> float:
        if first_drop:
            if misses >= 17:
                return 1.0
            return min(1.0, 0.04 + max(0, misses - 7) * 0.012)
        if misses >= 49:
            return 1.0
        return min(1.0, 0.015 + max(0, misses - 17) * 0.006)

    @staticmethod
    def _spellbook_probability(misses: int, *, first_drop: bool) -> float:
        if first_drop:
            if misses >= 27:
                return 1.0
            return min(1.0, 0.025 + max(0, misses - 11) * 0.007)
        if misses >= 64:
            return 1.0
        return min(1.0, 0.01 + max(0, misses - 24) * 0.003)

    @staticmethod
    def _story(entropy, actor, action_seq, has_exp, has_equipment, has_book):
        if has_equipment and has_book:
            pool = _DOUBLE_DROP_STORIES
            story_id = "rift_cache"
        elif has_book:
            pool = _BOOK_STORIES
            story_id = "wandering_grimoire"
        elif has_equipment:
            pool = _EQUIPMENT_STORIES
            story_id = "travelling_cache"
        else:
            pool = _XP_STORIES
            story_id = "sudden_insight"
        text = entropy.choice(
            pool,
            stream="story",
            actor=actor,
            action_seq=action_seq,
        )
        if has_exp and (has_equipment or has_book):
            text += " 这场小奇遇也让你增长了见识。"
        return story_id, text

    def _basic_rejection(self, context):
        if not context.is_group_message:
            return "not_group_message"
        if context.is_bot:
            return "bot_message"
        if context.is_command or str(context.content).lstrip().startswith(_COMMAND_PREFIXES):
            return "command_message"
        return ""

    def _content_rejection(self, normalised):
        units = _SEMANTIC_UNIT_RE.findall(normalised)
        if len(units) < self.policy.minimum_semantic_units:
            return "too_short"
        if len(set(units)) <= 1:
            return "low_information"
        return ""

    @staticmethod
    def _validate_context(context):
        if not isinstance(context, ChatMessageContext):
            raise TypeError("context must be a ChatMessageContext")
        if not context.event_key or len(context.event_key) > 240:
            raise ValueError("event_key must contain 1-240 characters")
        if not context.group_id or len(context.group_id) > 160:
            raise ValueError("group_id must contain 1-160 characters")
        if isinstance(context.user_pk, bool) or not isinstance(context.user_pk, int) or context.user_pk <= 0:
            raise ValueError("user_pk must be a positive integer")
        if not isinstance(context.content, str) or len(context.content) > 8000:
            raise ValueError("content must be a string no longer than 8000 characters")
        if isinstance(context.occurred_at_ts, bool) or not isinstance(context.occurred_at_ts, int) or context.occurred_at_ts < 0:
            raise ValueError("occurred_at_ts must be a non-negative integer")


class ChatActivitySettlementService:
    """Atomically turn a reserved chat intent into concrete progression."""

    def __init__(
        self,
        db_path: str,
        user_service,
        *,
        equipment_port: EquipmentDropPort | None = None,
        spellbook_port: SpellbookDropPort | None = None,
        policy: ChatActivityPolicy | None = None,
    ) -> None:
        self.db_path = db_path
        self.user_service = user_service
        self.equipment_port = equipment_port
        self.spellbook_port = spellbook_port
        self.policy = policy or ChatActivityPolicy()

    async def settle(self, intent: ChatRewardIntent) -> ChatActivitySettlementResult:
        self._validate_intent(intent)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT event_key, intent_json, settled, occurred_at_ts
                    FROM chat_activity_events
                    WHERE reward_key = ? AND user_pk = ?
                    """,
                    (intent.reward_key, int(intent.user_pk)),
                )
                event_row = await cursor.fetchone()
                await cursor.close()
                if event_row is None or str(event_row["intent_json"]) != _intent_json(intent):
                    raise ValueError("chat reward intent has no exact durable reservation")
                cursor = await db.execute(
                    """
                    SELECT user_pk, source, exp_gain
                    FROM reward_ledger WHERE reward_key = ?
                    """,
                    (intent.reward_key,),
                )
                ledger = await cursor.fetchone()
                await cursor.close()
                if ledger is not None:
                    if int(ledger["user_pk"]) != intent.user_pk or str(ledger["source"]) != "chat_growth":
                        raise RuntimeError("chat reward key collides with another ledger owner")
                    await db.execute(
                        """
                        UPDATE chat_activity_events
                        SET settled = 1, actual_exp = ?
                        WHERE event_key = ?
                        """,
                        (int(ledger["exp_gain"]), str(event_row["event_key"])),
                    )
                    await db.commit()
                    return ChatActivitySettlementResult(
                        reward_key=intent.reward_key,
                        applied=False,
                    )
                if bool(event_row["settled"]):
                    raise RuntimeError("chat event is settled without its reward ledger")

                user = await self.user_service.get_user_by_pk_in_db(db, intent.user_pk)
                if str(user.group_id or "") != intent.group_id:
                    raise ValueError("chat reward group no longer matches user ownership")
                day_key, start_ts, end_ts = chat_activity_day_window(
                    # The day key itself is authoritative.  Noon avoids DST/reset
                    # edge ambiguity; Hong Kong currently has no DST.
                    int(datetime.fromisoformat(intent.day_key).replace(
                        hour=12,
                        tzinfo=_ACTIVITY_ZONE,
                    ).timestamp()),
                    reset_hour=self.policy.reset_hour,
                )
                if day_key != intent.day_key:
                    raise ValueError("chat intent carries an invalid activity day")
                allocation = await allocate_daily_growth_in_db(
                    db,
                    user_pk=intent.user_pk,
                    level=user.level,
                    requested_exp=intent.experience,
                    day_window=(intent.day_key, start_ts, end_ts),
                )
                actual_exp = allocation.granted

                await db.execute(
                    """
                    INSERT INTO reward_ledger (
                        reward_key, user_pk, battle_id, source, exp_gain,
                        currency_gain, reason, created_at_ts
                    ) VALUES (?, ?, NULL, 'chat_growth', ?, 0, ?, ?)
                    """,
                    (
                        intent.reward_key,
                        intent.user_pk,
                        actual_exp,
                        json.dumps(
                            {
                                "ruleset_id": intent.ruleset_id,
                                "story_id": intent.story_id,
                                "day_key": intent.day_key,
                                "valid_message_index": intent.valid_message_index,
                                "equipment": intent.has_equipment,
                                "spell_id": intent.spell_id,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        int(event_row["occurred_at_ts"]),
                    ),
                )
                exp_result = None
                if actual_exp > 0:
                    exp_result = await self.user_service.add_exp_in_db(db, user, actual_exp)

                equipment = None
                if intent.has_equipment:
                    if self.equipment_port is None:
                        raise RuntimeError("equipment drop port is not configured")
                    equipment = await self.equipment_port.grant_in_db(
                        db,
                        user_pk=intent.user_pk,
                        player_level=user.level,
                        seed=int(intent.equipment_seed),
                    )

                spellbook = None
                if intent.has_spellbook:
                    if self.spellbook_port is None:
                        raise RuntimeError("spellbook drop port is not configured")
                    spellbook = await self.spellbook_port.grant_in_db(
                        db,
                        user_pk=intent.user_pk,
                        spell_id=str(intent.spell_id),
                        seed=int(intent.spellbook_seed),
                    )

                await db.execute(
                    """
                    UPDATE chat_activity_daily
                    SET reserved_exp = MAX(0, reserved_exp - ?),
                        awarded_exp = awarded_exp + ?, updated_at_ts = ?
                    WHERE user_pk = ? AND group_id = ? AND day_key = ?
                    """,
                    (
                        int(intent.experience),
                        int(actual_exp),
                        int(datetime.now(tz=_ACTIVITY_ZONE).timestamp()),
                        intent.user_pk,
                        intent.group_id,
                        intent.day_key,
                    ),
                )
                await db.execute(
                    """
                    UPDATE chat_activity_events SET settled = 1, actual_exp = ?
                    WHERE event_key = ?
                    """,
                    (actual_exp, str(event_row["event_key"])),
                )
                await db.commit()
                return ChatActivitySettlementResult(
                    reward_key=intent.reward_key,
                    applied=True,
                    experience=actual_exp,
                    equipment=equipment,
                    spellbook=spellbook,
                    spell_name=intent.spell_name,
                    level_ups=tuple(() if exp_result is None else exp_result.level_ups),
                    story_text=intent.story_text,
                )
            except Exception:
                await db.rollback()
                raise

    def _validate_intent(self, intent):
        if not isinstance(intent, ChatRewardIntent):
            raise TypeError("intent must be a ChatRewardIntent")
        if intent.ruleset_id != self.policy.ruleset_id:
            raise ValueError("chat reward ruleset does not match settlement policy")
        if not intent.reward_key.startswith(f"chat:{intent.day_key}:") or len(intent.reward_key) > 200:
            raise ValueError("chat reward key has an invalid namespace")
        if isinstance(intent.user_pk, bool) or intent.user_pk <= 0:
            raise ValueError("chat reward user_pk must be positive")
        if intent.valid_message_index <= 0 or intent.experience < 0:
            raise ValueError("chat reward numeric fields are invalid")
        if bool(intent.spell_id) != (intent.spellbook_seed is not None):
            raise ValueError("spellbook id and seed must appear together")
        if not intent.has_reward:
            raise ValueError("empty chat reward intents cannot be settled")


__all__ = [
    "ChatActivityPolicy",
    "ChatActivityService",
    "ChatActivitySettlementService",
    "EquipmentDropPort",
    "EquipmentServiceDropAdapter",
    "SpellServiceBookAdapter",
    "SpellbookDropPort",
    "chat_activity_day_window",
    "format_chat_activity_settlement",
    "shared_daily_exp_earned_in_db",
]
