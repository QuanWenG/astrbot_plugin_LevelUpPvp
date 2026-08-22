"""One activity-day budget shared by every source of character EXP.

The module deliberately owns only accounting policy.  Callers keep their own
source caps, idempotency keys, reward wording and persistence, while this
module answers one question inside the caller's SQLite transaction: how much
of the requested character EXP can still fit in today's shared budget?

Concurrency contract
--------------------
The caller must acquire the SQLite writer with ``BEGIN IMMEDIATE`` before
calling :func:`allocate_daily_growth_in_db`, then write its audit row and apply
the returned grant before committing.  That makes query -> clamp -> ledger ->
character update one serial transaction without introducing service-to-service
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from .progression_rules import level_daily_exp_budget
except ImportError:
    from services.progression_rules import level_daily_exp_budget


DEFAULT_ACTIVITY_RESET_HOUR = 4
DEFAULT_ACTIVITY_TIMEZONE = "Asia/Hong_Kong"


@dataclass(frozen=True)
class DailyGrowthAllocation:
    """The auditable result of one in-transaction budget decision."""

    day_key: str
    budget: int
    earned_before: int
    requested: int
    granted: int

    @property
    def remaining_after(self) -> int:
        return max(0, self.budget - self.earned_before - self.granted)

    @property
    def exhausted(self) -> bool:
        return self.remaining_after <= 0


def daily_growth_day_window(
    at: datetime | int | float | None = None,
    *,
    reset_hour: int = DEFAULT_ACTIVITY_RESET_HOUR,
    timezone_name: str = DEFAULT_ACTIVITY_TIMEZONE,
) -> tuple[str, int, int]:
    """Return ``(day_key, start_ts, end_ts)`` for the local 04:00 day.

    Naive datetimes are interpreted as local wall-clock time.  Integer and
    floating values are Unix timestamps.  The end is exclusive.
    """

    if isinstance(reset_hour, bool) or not isinstance(reset_hour, int):
        raise TypeError("reset_hour must be an integer")
    if not 0 <= reset_hour <= 23:
        raise ValueError("reset_hour must be between 0 and 23")
    zone = ZoneInfo(str(timezone_name))
    if at is None:
        local = datetime.now(tz=zone)
    elif isinstance(at, datetime):
        local = at.replace(tzinfo=zone) if at.tzinfo is None else at.astimezone(zone)
    elif isinstance(at, bool) or not isinstance(at, (int, float)):
        raise TypeError("at must be a datetime, Unix timestamp, or None")
    else:
        local = datetime.fromtimestamp(float(at), tz=zone)

    shifted_day = (local - timedelta(hours=reset_hour)).date()
    start = datetime(
        shifted_day.year,
        shifted_day.month,
        shifted_day.day,
        reset_hour,
        tzinfo=zone,
    )
    end = start + timedelta(days=1)
    return shifted_day.isoformat(), int(start.timestamp()), int(end.timestamp())


async def daily_growth_exp_earned_in_db(
    db,
    *,
    user_pk: int,
    day_key: str,
    day_start_ts: int,
    day_end_ts: int,
) -> int:
    """Count every positive character-EXP grant in one activity day.

    Current sources are audited by ``reward_ledger``; check-in intentionally
    retains its dedicated unique daily row.  The three legacy queries cover
    same-day PvP, dungeon and external-plugin grants made immediately before a
    deployment of this shared budget.  New rows have a matching ledger entry
    and are therefore not counted twice.
    """

    if isinstance(user_pk, bool) or not isinstance(user_pk, int) or user_pk <= 0:
        raise ValueError("user_pk must be a positive integer")
    start_ts = int(day_start_ts)
    end_ts = int(day_end_ts)
    if start_ts >= end_ts:
        raise ValueError("activity-day start must be before end")

    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN exp_gain > 0 THEN exp_gain ELSE 0 END), 0)
               AS amount
        FROM reward_ledger
        WHERE user_pk = ? AND created_at_ts >= ? AND created_at_ts < ?
        """,
        (user_pk, start_ts, end_ts),
    )
    ledger_exp = int((await cursor.fetchone())["amount"])
    await cursor.close()

    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN exp_gain > 0 THEN exp_gain ELSE 0 END), 0)
               AS amount
        FROM checkins
        WHERE user_pk = ? AND checkin_date = ?
        """,
        (user_pk, str(day_key)),
    )
    checkin_exp = int((await cursor.fetchone())["amount"])
    await cursor.close()

    # Compatibility for battles created before PvP started writing its own
    # pvp_growth ledger rows.
    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(
            CASE
                WHEN winner_pk = ? AND winner_exp_gain > 0 THEN winner_exp_gain
                WHEN loser_pk = ? AND loser_exp_gain > 0 THEN loser_exp_gain
                ELSE 0
            END
        ), 0) AS amount
        FROM battles AS b
        WHERE b.created_at_ts >= ? AND b.created_at_ts < ?
          AND (b.winner_pk = ? OR b.loser_pk = ?)
          AND NOT EXISTS (
              SELECT 1 FROM reward_ledger AS r
              WHERE r.battle_id = b.id AND r.user_pk = ?
                AND r.source = 'pvp_growth'
          )
        """,
        (user_pk, user_pk, start_ts, end_ts, user_pk, user_pk, user_pk),
    )
    legacy_pvp_exp = int((await cursor.fetchone())["amount"])
    await cursor.close()

    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN d.exp_gain > 0 THEN d.exp_gain ELSE 0 END
        ), 0) AS amount
        FROM dungeon_runs AS d
        WHERE d.user_pk = ?
          AND d.created_at_ts >= ? AND d.created_at_ts < ?
          AND NOT EXISTS (
              SELECT 1 FROM reward_ledger AS r
              WHERE r.reward_key = 'dungeon-growth:' || d.id
          )
        """,
        (user_pk, start_ts, end_ts),
    )
    legacy_dungeon_exp = int((await cursor.fetchone())["amount"])
    await cursor.close()

    # ``created_at`` in this historical table is local ISO wall-clock text.
    # Text comparison is exact because it is persisted in sortable ISO form.
    activity_zone = ZoneInfo(DEFAULT_ACTIVITY_TIMEZONE)
    start_text = datetime.fromtimestamp(start_ts, activity_zone).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")
    end_text = datetime.fromtimestamp(end_ts, activity_zone).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")
    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN e.level_exp_gain > 0 THEN e.level_exp_gain ELSE 0 END
        ), 0) AS amount
        FROM external_activity_rewards AS e
        WHERE e.user_pk = ? AND e.component = 'correct'
          AND e.created_at >= ? AND e.created_at < ?
          AND NOT EXISTS (
              SELECT 1 FROM reward_ledger AS r
              WHERE r.reward_key = 'external-growth:' || e.id
          )
        """,
        (user_pk, start_text, end_text),
    )
    legacy_external_exp = int((await cursor.fetchone())["amount"])
    await cursor.close()

    return max(
        0,
        ledger_exp
        + checkin_exp
        + legacy_pvp_exp
        + legacy_dungeon_exp
        + legacy_external_exp,
    )


async def allocate_daily_growth_in_db(
    db,
    *,
    user_pk: int,
    level: int,
    requested_exp: int,
    at: datetime | int | float | None = None,
    day_window: tuple[str, int, int] | None = None,
) -> DailyGrowthAllocation:
    """Clamp one requested grant against the shared character daily budget.

    This function does not write a reservation.  Its result is concurrency-safe
    only when the caller follows the module contract and already owns a
    ``BEGIN IMMEDIATE`` transaction through the subsequent audit and EXP write.
    """

    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("level must be an integer")
    if (
        isinstance(requested_exp, bool)
        or not isinstance(requested_exp, int)
        or requested_exp < 0
    ):
        raise ValueError("requested_exp must be a non-negative integer")
    window = day_window or daily_growth_day_window(at)
    if not isinstance(window, tuple) or len(window) != 3:
        raise ValueError("day_window must be a (day_key, start_ts, end_ts) tuple")
    day_key, start_ts, end_ts = window
    earned = await daily_growth_exp_earned_in_db(
        db,
        user_pk=user_pk,
        day_key=str(day_key),
        day_start_ts=int(start_ts),
        day_end_ts=int(end_ts),
    )
    budget = max(0, int(level_daily_exp_budget(max(1, level))))
    granted = min(requested_exp, max(0, budget - earned))
    return DailyGrowthAllocation(
        day_key=str(day_key),
        budget=budget,
        earned_before=earned,
        requested=requested_exp,
        granted=granted,
    )


__all__ = [
    "DEFAULT_ACTIVITY_RESET_HOUR",
    "DEFAULT_ACTIVITY_TIMEZONE",
    "DailyGrowthAllocation",
    "allocate_daily_growth_in_db",
    "daily_growth_day_window",
    "daily_growth_exp_earned_in_db",
]
