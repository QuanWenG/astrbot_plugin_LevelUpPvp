import random
from datetime import date, datetime, timedelta

try:
    from ..models.user import CheckinResult, UserIdentity
    from . import config
    from .attribute_service import AttributeService
    from .daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
    )
    from .db import connect_db
    from .progression_rules import (
        character_catchup_multiplier,
        round_half_up,
    )
    from .user_service import UserService, utc_now_text
except ImportError:
    from models.user import CheckinResult, UserIdentity
    from services import config
    from services.attribute_service import AttributeService
    from services.daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
    )
    from services.db import connect_db
    from services.progression_rules import (
        character_catchup_multiplier,
        round_half_up,
    )
    from services.user_service import UserService, utc_now_text

def current_checkin_date(now: datetime | None = None) -> date:
    day_key, _, _ = daily_growth_day_window(
        now,
        reset_hour=config.CHECKIN_DAY_RESET_HOUR,
    )
    return date.fromisoformat(day_key)



class CheckinService:
    def __init__(
        self,
        db_path: str,
        user_service: UserService,
        attribute_service=None,
        rng=None,
    ):
        self.db_path = db_path
        self.user_service = user_service
        self.attribute_service = attribute_service or AttributeService(db_path)
        self.rng = rng or random

    async def checkin(self, identity: UserIdentity) -> CheckinResult:
        day_window = daily_growth_day_window(
            reset_hour=config.CHECKIN_DAY_RESET_HOUR,
        )
        today_text = day_window[0]
        today = date.fromisoformat(today_text)
        async with await connect_db(self.db_path) as db:
            # Reserve the SQLite writer before checking the daily row so two
            # simultaneous first messages cannot both award experience.
            await db.execute("BEGIN IMMEDIATE")
            user, _ = await self.user_service.get_or_create_user_in_db(db, identity)
            cursor = await db.execute(
                """
                SELECT streak_days, exp_gain FROM checkins
                WHERE user_pk = ? AND checkin_date = ?
                """,
                (user.id, today_text),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing:
                await db.rollback()
                return CheckinResult(
                    user=user,
                    exp_gain=int(existing["exp_gain"]),
                    streak_days=int(existing["streak_days"]),
                    level_ups=[],
                    already_checked=True,
                )

            streak_days = await self._calculate_streak_in_db(db, user.id, today)
            reference_level = await self._active_group_reference_level_in_db(
                db, user
            )
            proposed_exp = self._roll_exp(
                user.level,
                streak_days,
                reference_level=reference_level,
            )
            allocation = await allocate_daily_growth_in_db(
                db,
                user_pk=user.id,
                level=user.level,
                requested_exp=proposed_exp,
                day_window=day_window,
            )
            exp_gain = allocation.granted
            exp_result = await self.user_service.add_exp_in_db(db, user, exp_gain)
            await db.execute(
                """
                INSERT INTO checkins (
                    user_pk, checkin_date, streak_days, exp_gain, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user.id, today_text, streak_days, exp_gain, utc_now_text()),
            )
            potential_restore = (
                await self.attribute_service.restore_checkin_potential_in_db(
                    db, user.id
                )
            )
            await db.commit()
            return CheckinResult(
                user=exp_result.user,
                exp_gain=exp_gain,
                streak_days=streak_days,
                level_ups=exp_result.level_ups,
                attribute_potential_restore=potential_restore,
            )

    async def _calculate_streak_in_db(self, db, user_pk: int, today: date) -> int:
        cursor = await db.execute(
            """
            SELECT checkin_date, streak_days FROM checkins
            WHERE user_pk = ?
            ORDER BY checkin_date DESC
            LIMIT 1
            """,
            (user_pk,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return 1
        last_date = date.fromisoformat(row["checkin_date"])
        if last_date == today - timedelta(days=1):
            return row["streak_days"] + 1
        return 1

    async def _active_group_reference_level_in_db(self, db, user) -> int:
        """Median level of recently active group members for soft catch-up."""
        threshold = (
            datetime.now()
            - timedelta(days=config.CHECKIN_ACTIVE_REFERENCE_DAYS)
        ).isoformat(timespec="seconds")
        cursor = await db.execute(
            """
            SELECT level FROM users
            WHERE platform = ? AND group_id = ?
              AND (updated_at >= ? OR id = ?)
            ORDER BY level ASC
            """,
            (user.platform, user.group_id or "", threshold, user.id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        levels = [int(row["level"]) for row in rows]
        if not levels:
            return max(1, int(user.level))
        midpoint = len(levels) // 2
        if len(levels) % 2:
            return levels[midpoint]
        return round_half_up((levels[midpoint - 1] + levels[midpoint]) / 2)

    def _roll_exp(
        self,
        level: int,
        streak_days: int,
        reference_level: int | None = None,
    ) -> int:
        """Spend the check-in share of the daily budget with gentle variance."""
        variance = self.rng.uniform(*config.CHECKIN_VARIANCE_RANGE)
        streak_steps = min(
            max(0, int(streak_days) - 1),
            config.CHECKIN_MAX_STREAK_BONUS_DAYS,
        )
        streak_multiplier = (
            1.0 + streak_steps * config.CHECKIN_STREAK_BONUS_RATE_STEP
        )
        catchup_multiplier = character_catchup_multiplier(
            level,
            reference_level,
            config.CHECKIN_CATCHUP_MAX_MULTIPLIER,
        )
        reward = (
            config.exp_daily_budget(level)
            * config.CHECKIN_DAILY_BUDGET_SHARE
            * variance
            * streak_multiplier
            * catchup_multiplier
        )
        return max(1, round_half_up(reward))
