from __future__ import annotations

import json
import random
import time
from dataclasses import asdict
from typing import ClassVar

try:
    from ..models.user import UserIdentity
    from .attribute_service import attribute_exp_required, training_efficiency
    from .daily_growth_budget import allocate_daily_growth_in_db
    from .db import connect_db
    from .progression_rules import (
        RULESET_ID,
        decay_attribute_potential,
        display_exp,
        scaled_exp_gain,
    )
    from .user_service import utc_now_text
except ImportError:
    from models.user import UserIdentity
    from services.attribute_service import (
        attribute_exp_required,
        training_efficiency,
    )
    from services.daily_growth_budget import allocate_daily_growth_in_db
    from services.db import connect_db
    from services.progression_rules import (
        RULESET_ID,
        decay_attribute_potential,
        display_exp,
        scaled_exp_gain,
    )
    from services.user_service import utc_now_text


class ExternalActivityService:
    """Grant idempotent level and primary-attribute rewards to other plugins."""

    ATTRIBUTE_COLUMNS: ClassVar[dict[str, str]] = {
        "perception": "atk",
        "magic": "luck",
    }
    COMPONENT_RAW_TRAINING = 10
    LEVEL_EXP_ROLL_RANGE = (10, 20)

    def __init__(
        self,
        db_path: str,
        user_service,
        attribute_service,
        randint=None,
    ):
        self.db_path = db_path
        self.user_service = user_service
        self.attribute_service = attribute_service
        self.randint = randint or random.randint

    async def grant(
        self,
        *,
        identity: UserIdentity,
        source: str,
        reward_key: str,
        valid_attempt: bool,
        correct: bool,
    ) -> dict:
        """Grant missing attempt/correct reward components atomically.

        Args:
            identity: Platform identity of the rewarded player.
            source: Stable caller plugin identifier.
            reward_key: Caller-scoped idempotency key for one player and round.
            valid_attempt: Whether this call represents a valid answer attempt.
            correct: Whether this call also represents a correct answer.

        Returns:
            Applied components and concrete level/attribute experience gains.

        Raises:
            ValueError: If the source or reward key is empty.
        """
        source = str(source).strip()
        reward_key = str(reward_key).strip()
        if not source or not reward_key:
            raise ValueError("source 和 reward_key 不能为空")

        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                user, _ = await self.user_service.get_or_create_user_in_db(
                    db, identity
                )
                cursor = await db.execute(
                    """
                    SELECT component
                    FROM external_activity_rewards
                    WHERE user_pk = ? AND source = ? AND reward_key = ?
                    """,
                    (user.id, source, reward_key),
                )
                existing = {row["component"] for row in await cursor.fetchall()}
                await cursor.close()

                components = []
                if valid_attempt and "attempt" not in existing:
                    components.append("attempt")
                if correct and "correct" not in existing:
                    components.append("correct")
                if not components:
                    await db.commit()
                    return {
                        "nickname": user.nickname,
                        "applied_components": [],
                        "level_exp": 0,
                        "attribute_exp": {"perception": 0, "magic": 0},
                        "level_ups": [],
                    }

                progress = await self.attribute_service.progress_in_db(db, user.id)
                per_component = {}
                efficiency = training_efficiency(user.willpower)
                for attribute_id in self.ATTRIBUTE_COLUMNS:
                    state = progress[attribute_id]
                    per_component[attribute_id] = scaled_exp_gain(
                        self.COMPONENT_RAW_TRAINING,
                        state.potential,
                        efficiency,
                    )

                level_exp = 0
                requested_level_exp = 0
                growth_now_ts = int(time.time())
                level_ups = []
                if "correct" in components:
                    roll_count = max(
                        1,
                        int(user.perception) + int(user.magic),
                    )
                    requested_level_exp = sum(
                        self.randint(*self.LEVEL_EXP_ROLL_RANGE)
                        for _ in range(roll_count)
                    )
                    allocation = await allocate_daily_growth_in_db(
                        db,
                        user_pk=user.id,
                        level=user.level,
                        requested_exp=requested_level_exp,
                        at=growth_now_ts,
                    )
                    level_exp = allocation.granted
                    exp_result = await self.user_service.add_exp_in_db(
                        db, user, level_exp
                    )
                    user = exp_result.user
                    level_ups = [asdict(event) for event in exp_result.level_ups]

                attribute_exp = {
                    attribute_id: gain * len(components)
                    for attribute_id, gain in per_component.items()
                }
                progress = await self.attribute_service.progress_in_db(db, user.id)
                now = utc_now_text()
                for attribute_id, gain in attribute_exp.items():
                    state = progress[attribute_id]
                    old_value = int(getattr(user, attribute_id))
                    value = old_value
                    exp = state.exp + gain
                    potential = state.potential
                    while exp >= attribute_exp_required(value):
                        exp -= attribute_exp_required(value)
                        value += 1
                        potential = decay_attribute_potential(potential)
                    await db.execute(
                        """
                        UPDATE user_attribute_progress
                        SET exp = ?, potential = ?
                        WHERE user_pk = ? AND attribute_id = ?
                        """,
                        (exp, potential, user.id, attribute_id),
                    )
                    if value != old_value:
                        await db.execute(
                            f"UPDATE users SET {self.ATTRIBUTE_COLUMNS[attribute_id]} = ? "
                            "WHERE id = ?",
                            (value, user.id),
                        )
                    await db.execute(
                        """
                        INSERT INTO attribute_growth_logs (
                            user_pk, battle_id, attribute_id, exp_gain,
                            from_value, to_value, potential_before,
                            potential_after, created_at, rules_version
                        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user.id,
                            attribute_id,
                            gain,
                            old_value,
                            value,
                            state.potential,
                            potential,
                            now,
                            RULESET_ID,
                        ),
                    )

                for component in components:
                    component_level_exp = level_exp if component == "correct" else 0
                    await db.execute(
                        """
                        INSERT INTO external_activity_rewards (
                            user_pk, source, reward_key, component,
                            level_exp_gain, perception_exp_gain,
                            magic_exp_gain, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user.id,
                            source,
                            reward_key,
                            component,
                            component_level_exp,
                            display_exp(per_component["perception"]),
                            display_exp(per_component["magic"]),
                            now,
                        ),
                    )
                if "correct" in components:
                    cursor = await db.execute(
                        """
                        SELECT id FROM external_activity_rewards
                        WHERE user_pk = ? AND source = ? AND reward_key = ?
                          AND component = 'correct'
                        """,
                        (user.id, source, reward_key),
                    )
                    correct_row = await cursor.fetchone()
                    await cursor.close()
                    if correct_row is None:
                        raise RuntimeError("external correct reward audit row is missing")
                    await db.execute(
                        """
                        INSERT INTO reward_ledger (
                            reward_key, user_pk, battle_id, source, exp_gain,
                            currency_gain, reason, created_at_ts
                        ) VALUES (?, ?, NULL, ?, ?, 0, ?, ?)
                        """,
                        (
                            f"external-growth:{int(correct_row['id'])}",
                            user.id,
                            f"external_activity:{source}",
                            level_exp,
                            json.dumps(
                                {
                                    "caller_source": source,
                                    "caller_reward_key": reward_key,
                                    "requested_experience": requested_level_exp,
                                    "granted_experience": level_exp,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            growth_now_ts,
                        ),
                    )
                await db.commit()
                return {
                    "nickname": user.nickname,
                    "applied_components": components,
                    "level_exp": level_exp,
                    "attribute_exp": {
                        attribute_id: display_exp(gain)
                        for attribute_id, gain in attribute_exp.items()
                    },
                    "level_ups": level_ups,
                }
            except Exception:
                await db.rollback()
                raise
