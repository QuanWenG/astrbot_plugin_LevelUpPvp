import random
from datetime import date, timedelta

try:
    from ..models.user import CheckinResult, UserIdentity
    from . import config
    from .db import connect_db
    from .user_service import UserService, utc_now_text
except ImportError:
    from models.user import CheckinResult, UserIdentity
    from services import config
    from services.db import connect_db
    from services.user_service import UserService, utc_now_text


class CheckinService:
    def __init__(self, db_path: str, user_service: UserService):
        self.db_path = db_path
        self.user_service = user_service

    async def checkin(self, identity: UserIdentity) -> CheckinResult:
        today = date.today()
        today_text = today.isoformat()
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            user, _ = await self.user_service.get_or_create_user_in_db(db, identity)
            cursor = await db.execute(
                """
                SELECT id FROM checkins
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
                    exp_gain=0,
                    streak_days=await self._get_latest_streak(user.id),
                    level_ups=[],
                    already_checked=True,
                )

            streak_days = await self._calculate_streak_in_db(db, user.id, today)
            exp_gain = self._roll_exp(streak_days)
            exp_result = await self.user_service.add_exp_in_db(db, user, exp_gain)
            await db.execute(
                """
                INSERT INTO checkins (
                    user_pk, checkin_date, streak_days, exp_gain, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user.id, today_text, streak_days, exp_gain, utc_now_text()),
            )
            await db.commit()
            return CheckinResult(
                user=exp_result.user,
                exp_gain=exp_gain,
                streak_days=streak_days,
                level_ups=exp_result.level_ups,
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

    async def _get_latest_streak(self, user_pk: int) -> int:
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT streak_days FROM checkins
                WHERE user_pk = ?
                ORDER BY checkin_date DESC
                LIMIT 1
                """,
                (user_pk,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return int(row["streak_days"]) if row else 0

    def _roll_exp(self, streak_days: int) -> int:
        base_min, base_max = config.CHECKIN_BASE_EXP_RANGE
        base = random.randint(base_min, base_max)
        bonus_cap = (
            min(streak_days - 1, config.CHECKIN_MAX_STREAK_BONUS_DAYS)
            * config.CHECKIN_STREAK_BONUS_STEP
        )
        return base + random.randint(0, max(0, bonus_cap))
