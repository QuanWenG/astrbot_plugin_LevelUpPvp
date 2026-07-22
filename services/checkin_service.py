import math
import random
from datetime import date, datetime, timedelta

try:
    from ..models.user import CheckinResult, UserIdentity
    from . import config
    from .attribute_service import AttributeService
    from .db import connect_db
    from .user_service import UserService, utc_now_text
except ImportError:
    from models.user import CheckinResult, UserIdentity
    from services import config
    from services.attribute_service import AttributeService
    from services.db import connect_db
    from services.user_service import UserService, utc_now_text

def current_checkin_date(now: datetime | None = None) -> date:
    current_time = now or datetime.now()
    return (
        current_time - timedelta(hours=config.CHECKIN_DAY_RESET_HOUR)
    ).date()



class CheckinService:
    def __init__(
        self, db_path: str, user_service: UserService, attribute_service=None
    ):
        self.db_path = db_path
        self.user_service = user_service
        self.attribute_service = attribute_service or AttributeService(db_path)

    async def checkin(self, identity: UserIdentity) -> CheckinResult:
        today = current_checkin_date()
        today_text = today.isoformat()
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
            exp_gain = self._roll_exp(user.level, streak_days)
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

    def _roll_exp(self, level: int, streak_days: int) -> int:
        required = config.exp_required_for_next_level(level)
        roll_min, roll_max = config.CHECKIN_ROLL_EXP_RANGE
        base = sum(random.randint(roll_min, roll_max) for _ in range(level))
        if base < required * config.CHECKIN_FALLBACK_THRESHOLD_RATE:
            fallback_min, fallback_max = config.CHECKIN_FALLBACK_EXP_RATE_RANGE
            base = round(required * random.uniform(fallback_min, fallback_max))
        base = min(
            base,
            math.floor(required * config.CHECKIN_BASE_EXP_CAP_RATE),
        )
        bonus_cap = (
            min(streak_days - 1, config.CHECKIN_MAX_STREAK_BONUS_DAYS)
            * config.CHECKIN_STREAK_BONUS_STEP
        )
        return base + random.randint(0, max(0, bonus_cap))
