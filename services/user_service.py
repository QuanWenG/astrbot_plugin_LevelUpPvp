import json
import random
from datetime import datetime

try:
    from ..models.user import (
        ExpChangeResult,
        LevelDownEvent,
        LevelUpEvent,
        User,
        UserIdentity,
    )
    from . import config
    from .db import connect_db
except ImportError:
    from models.user import (
        ExpChangeResult,
        LevelDownEvent,
        LevelUpEvent,
        User,
        UserIdentity,
    )
    from services import config
    from services.db import connect_db


STAT_NAMES = tuple(config.INITIAL_STATS.keys())


def utc_now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def row_to_user(row) -> User:
    return User(
        id=row["id"],
        platform=row["platform"],
        group_id=row["group_id"],
        user_id=row["user_id"],
        nickname=row["nickname"],
        level=row["level"],
        exp=row["exp"],
        total_exp=row["total_exp"],
        stat_points=row["stat_points"],
        level_up_count=row["level_up_count"],
        hp=row["hp"],
        atk=row["atk"],
        defense=row["defense"],
        speed=row["speed"],
        luck=row["luck"],
        wins=row["wins"],
        losses=row["losses"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class UserService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def get_or_create_user(self, identity: UserIdentity) -> User:
        async with await connect_db(self.db_path) as db:
            user, _ = await self.get_or_create_user_in_db(db, identity)
            await db.commit()
            return user

    async def get_or_create_user_in_db(self, db, identity: UserIdentity) -> tuple[User, bool]:
        group_id = identity.group_id or ""
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE platform = ? AND group_id = ? AND user_id = ?
            """,
            (identity.platform, group_id, identity.user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        now = utc_now_text()
        if row:
            user = row_to_user(row)
            registered_nickname = await self.get_registered_nickname_in_db(
                db,
                identity.platform,
                group_id,
                identity.user_id,
            )
            if registered_nickname and registered_nickname != user.nickname:
                await db.execute(
                    "UPDATE users SET nickname = ?, updated_at = ? WHERE id = ?",
                    (registered_nickname, now, user.id),
                )
                user.nickname = registered_nickname
                user.updated_at = now
            elif (
                not registered_nickname
                and identity.platform != "qq_official"
                and identity.nickname
                and identity.nickname != user.nickname
            ):
                await db.execute(
                    "UPDATE users SET nickname = ?, updated_at = ? WHERE id = ?",
                    (identity.nickname, now, user.id),
                )
                user.nickname = identity.nickname
                user.updated_at = now
            return await self._attach_freeze_summary_in_db(db, user), False

        registered_nickname = await self.get_registered_nickname_in_db(
            db,
            identity.platform,
            group_id,
            identity.user_id,
        )
        nickname = registered_nickname or self._default_nickname(identity)
        await db.execute(
            """
            INSERT INTO users (
                platform, group_id, user_id, nickname, level, exp, total_exp,
                stat_points, level_up_count, hp, atk, defense, speed, luck,
                wins, losses, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                identity.platform,
                group_id,
                identity.user_id,
                nickname,
                config.INITIAL_LEVEL,
                config.INITIAL_EXP,
                config.INITIAL_TOTAL_EXP,
                config.INITIAL_STAT_POINTS,
                0,
                config.INITIAL_STATS["hp"],
                config.INITIAL_STATS["atk"],
                config.INITIAL_STATS["defense"],
                config.INITIAL_STATS["speed"],
                config.INITIAL_STATS["luck"],
                now,
                now,
            ),
        )
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE platform = ? AND group_id = ? AND user_id = ?
            """,
            (identity.platform, group_id, identity.user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return await self._attach_freeze_summary_in_db(db, row_to_user(row)), True

    async def register_nickname(self, identity: UserIdentity, nickname: str) -> User:
        nickname = " ".join((nickname or "").split())
        if not nickname:
            raise ValueError("用法：/登记 昵称")
        if len(nickname) > 32:
            raise ValueError("昵称最多 32 个字符")

        group_id = identity.group_id or ""
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            user, _ = await self.get_or_create_user_in_db(db, identity)
            now = utc_now_text()
            mapped_group_ids = {group_id, ""}
            for mapped_group_id in mapped_group_ids:
                await self.set_registered_nickname_in_db(
                    db,
                    identity.platform,
                    mapped_group_id,
                    identity.user_id,
                    nickname,
                    now,
                )
            await db.execute(
                "UPDATE users SET nickname = ?, updated_at = ? WHERE id = ?",
                (nickname, now, user.id),
            )
            await db.commit()
            return await self.get_user_by_pk_in_db(db, user.id)

    async def has_registered_nickname(self, identity: UserIdentity) -> bool:
        async with await connect_db(self.db_path) as db:
            nickname = await self.get_registered_nickname_in_db(
                db,
                identity.platform,
                identity.group_id,
                identity.user_id,
            )
            return bool(nickname)

    async def get_user_by_pk_in_db(self, db, user_pk: int) -> User:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_pk,))
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError("用户不存在")
        user = row_to_user(row)
        registered_nickname = await self.get_registered_nickname_in_db(
            db,
            user.platform,
            user.group_id,
            user.user_id,
        )
        if registered_nickname:
            user.nickname = registered_nickname
        return await self._attach_freeze_summary_in_db(db, user)

    async def get_registered_nickname_in_db(
        self,
        db,
        platform: str,
        group_id: str,
        user_id: str,
    ) -> str:
        cursor = await db.execute(
            """
            SELECT nickname FROM nickname_mappings
            WHERE platform = ?
              AND user_id = ?
              AND group_id IN (?, '')
            ORDER BY CASE WHEN group_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (platform, user_id, group_id or "", group_id or ""),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["nickname"] if row else ""

    async def set_registered_nickname_in_db(
        self,
        db,
        platform: str,
        group_id: str,
        user_id: str,
        nickname: str,
        now: str,
    ) -> None:
        cursor = await db.execute(
            """
            SELECT id FROM nickname_mappings
            WHERE platform = ? AND group_id = ? AND user_id = ?
            """,
            (platform, group_id or "", user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            await db.execute(
                """
                UPDATE nickname_mappings
                SET nickname = ?, updated_at = ?
                WHERE id = ?
                """,
                (nickname, now, row["id"]),
            )
            return
        await db.execute(
            """
            INSERT INTO nickname_mappings (
                platform, group_id, user_id, nickname, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (platform, group_id or "", user_id, nickname, now, now),
        )

    async def get_top_users(
        self,
        platform: str,
        group_id: str,
        limit: int = 10,
    ) -> list[tuple[int, User]]:
        group_id = group_id or ""
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT * FROM users
                WHERE platform = ? AND group_id = ?
                ORDER BY level DESC, exp DESC, total_exp DESC, id ASC
                LIMIT ?
                """,
                (platform, group_id, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            ranked_users = []
            for index, row in enumerate(rows, start=1):
                user = row_to_user(row)
                registered_nickname = await self.get_registered_nickname_in_db(
                    db,
                    user.platform,
                    user.group_id,
                    user.user_id,
                )
                if registered_nickname:
                    user.nickname = registered_nickname
                user = await self._attach_freeze_summary_in_db(db, user)
                ranked_users.append((index, user))
        return ranked_users

    async def get_user_rank(
        self,
        identity: UserIdentity,
    ) -> tuple[int, User] | None:
        group_id = identity.group_id or ""
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT * FROM users
                WHERE platform = ? AND group_id = ?
                ORDER BY level DESC, exp DESC, total_exp DESC, id ASC
                """,
                (identity.platform, group_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for index, row in enumerate(rows, start=1):
                user = row_to_user(row)
                registered_nickname = await self.get_registered_nickname_in_db(
                    db,
                    user.platform,
                    user.group_id,
                    user.user_id,
                )
                if registered_nickname:
                    user.nickname = registered_nickname
                user = await self._attach_freeze_summary_in_db(db, user)
                if user.user_id == identity.user_id:
                    return index, user
        return None

    async def add_exp_in_db(self, db, user: User, amount: int) -> ExpChangeResult:
        if amount <= 0:
            return ExpChangeResult(user=user, exp_delta=0, level_ups=[])

        level = user.level
        exp = user.exp + amount
        total_exp = user.total_exp + amount
        stat_points = user.stat_points
        level_up_count = user.level_up_count
        stats = user.stats()
        level_ups: list[LevelUpEvent] = []
        now = utc_now_text()

        while exp >= config.exp_required_for_next_level(level):
            required = config.exp_required_for_next_level(level)
            exp -= required
            from_level = level
            to_level = level + 1
            released = await self._release_freeze_for_level_in_db(
                db,
                user.id,
                to_level,
                stats,
                now,
            )
            level = to_level
            if released:
                stat_points += released["frozen_stat_points"]
                level_ups.append(
                    LevelUpEvent(
                        from_level=from_level,
                        to_level=level,
                        auto_growth=released["frozen_stats"],
                        stat_points_gain=released["frozen_stat_points"],
                        restored_from_freeze=True,
                    )
                )
                continue

            level_up_count += 1
            stat_points += config.STAT_POINTS_PER_LEVEL
            auto_growth = self._roll_auto_growth()
            for stat_name, gain in auto_growth.items():
                stats[stat_name] += gain

            level_ups.append(
                LevelUpEvent(
                    from_level=from_level,
                    to_level=level,
                    auto_growth=auto_growth,
                    stat_points_gain=config.STAT_POINTS_PER_LEVEL,
                )
            )
            await db.execute(
                """
                INSERT INTO level_up_logs (
                    user_pk, from_level, to_level, auto_growth_json,
                    stat_points_gain, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    from_level,
                    level,
                    json.dumps(auto_growth, ensure_ascii=False),
                    config.STAT_POINTS_PER_LEVEL,
                    now,
                ),
            )

        await self._update_user_progress_in_db(
            db,
            user.id,
            level,
            exp,
            total_exp,
            stat_points,
            level_up_count,
            stats,
            now,
        )
        updated = await self.get_user_by_pk_in_db(db, user.id)
        return ExpChangeResult(user=updated, exp_delta=amount, level_ups=level_ups)

    async def deduct_exp_in_db(self, db, user: User, amount: int) -> ExpChangeResult:
        if amount <= 0:
            return ExpChangeResult(user=user, exp_delta=0, level_downs=[])

        remaining = amount
        actual_loss = 0
        level = user.level
        exp = user.exp
        total_exp = user.total_exp
        stat_points = user.stat_points
        level_up_count = user.level_up_count
        stats = user.stats()
        level_downs: list[LevelDownEvent] = []
        now = utc_now_text()

        while remaining > 0:
            if remaining <= exp:
                exp -= remaining
                actual_loss += remaining
                remaining = 0
                break

            remaining -= exp
            actual_loss += exp
            exp = 0
            if level <= config.INITIAL_LEVEL:
                break

            from_level = level
            to_level = level - 1
            level_down = await self._freeze_level_in_db(
                db,
                user.id,
                from_level,
                to_level,
                stats,
                stat_points,
                now,
            )
            stat_points = max(0, stat_points - level_down.frozen_stat_points)
            level_downs.append(level_down)
            level = to_level
            exp = config.exp_required_for_next_level(level)

        await self._update_user_progress_in_db(
            db,
            user.id,
            level,
            exp,
            total_exp,
            stat_points,
            level_up_count,
            stats,
            now,
        )
        updated = await self.get_user_by_pk_in_db(db, user.id)
        return ExpChangeResult(
            user=updated,
            exp_delta=-actual_loss,
            level_downs=level_downs,
        )

    async def increment_battle_stats_in_db(
        self,
        db,
        winner_id: int,
        loser_id: int,
    ) -> None:
        now = utc_now_text()
        await db.execute(
            "UPDATE users SET wins = wins + 1, updated_at = ? WHERE id = ?",
            (now, winner_id),
        )
        await db.execute(
            "UPDATE users SET losses = losses + 1, updated_at = ? WHERE id = ?",
            (now, loser_id),
        )

    async def _attach_freeze_summary_in_db(self, db, user: User) -> User:
        cursor = await db.execute(
            """
            SELECT frozen_level, frozen_stats_json, frozen_stat_points
            FROM level_freezes
            WHERE user_pk = ? AND status = 'frozen'
            ORDER BY frozen_level DESC, id DESC
            """,
            (user.id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        frozen_stats = {stat_name: 0 for stat_name in STAT_NAMES}
        frozen_levels = []
        frozen_stat_points = 0
        for row in rows:
            frozen_levels.append(int(row["frozen_level"]))
            frozen_stat_points += int(row["frozen_stat_points"])
            for stat_name, amount in self._load_stats_json(
                row["frozen_stats_json"]
            ).items():
                if stat_name in frozen_stats:
                    frozen_stats[stat_name] += amount
        user.frozen_stats = {
            stat_name: amount
            for stat_name, amount in frozen_stats.items()
            if amount > 0
        }
        user.frozen_stat_points = frozen_stat_points
        user.frozen_levels = sorted(set(frozen_levels), reverse=True)
        return user

    async def _update_user_progress_in_db(
        self,
        db,
        user_id: int,
        level: int,
        exp: int,
        total_exp: int,
        stat_points: int,
        level_up_count: int,
        stats: dict[str, int],
        now: str,
    ) -> None:
        await db.execute(
            """
            UPDATE users
            SET level = ?, exp = ?, total_exp = ?, stat_points = ?,
                level_up_count = ?, hp = ?, atk = ?, defense = ?,
                speed = ?, luck = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                level,
                exp,
                total_exp,
                stat_points,
                level_up_count,
                stats["hp"],
                stats["atk"],
                stats["defense"],
                stats["speed"],
                stats["luck"],
                now,
                user_id,
            ),
        )

    async def _release_freeze_for_level_in_db(
        self,
        db,
        user_id: int,
        restored_level: int,
        stats: dict[str, int],
        now: str,
    ) -> dict | None:
        cursor = await db.execute(
            """
            SELECT id, frozen_stats_json, frozen_stat_points
            FROM level_freezes
            WHERE user_pk = ?
              AND frozen_level = ?
              AND status = 'frozen'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, restored_level),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None

        frozen_stats = self._load_stats_json(row["frozen_stats_json"])
        for stat_name, amount in frozen_stats.items():
            stats[stat_name] += amount
        await db.execute(
            """
            UPDATE level_freezes
            SET status = 'released', released_at = ?
            WHERE id = ?
            """,
            (now, row["id"]),
        )
        return {
            "frozen_stats": frozen_stats,
            "frozen_stat_points": int(row["frozen_stat_points"]),
        }

    async def _freeze_level_in_db(
        self,
        db,
        user_id: int,
        from_level: int,
        to_level: int,
        stats: dict[str, int],
        stat_points: int,
        now: str,
    ) -> LevelDownEvent:
        frozen_stats = await self._level_auto_growth_in_db(db, user_id, from_level)
        for stat_name, amount in frozen_stats.items():
            stats[stat_name] = max(
                config.INITIAL_STATS[stat_name],
                stats[stat_name] - amount,
            )

        frozen_stat_points = min(stat_points, config.STAT_POINTS_PER_LEVEL)
        spent_points = config.STAT_POINTS_PER_LEVEL - frozen_stat_points
        spent_freeze = self._roll_spent_stat_freeze(stats, spent_points)
        for stat_name, amount in spent_freeze.items():
            frozen_stats[stat_name] = frozen_stats.get(stat_name, 0) + amount

        frozen_stats = {
            stat_name: amount
            for stat_name, amount in frozen_stats.items()
            if amount > 0
        }
        await db.execute(
            """
            INSERT INTO level_freezes (
                user_pk, frozen_level, from_level, to_level, frozen_stats_json,
                frozen_stat_points, status, created_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'frozen', ?, NULL)
            """,
            (
                user_id,
                from_level,
                from_level,
                to_level,
                json.dumps(frozen_stats, ensure_ascii=False),
                frozen_stat_points,
                now,
            ),
        )
        return LevelDownEvent(
            from_level=from_level,
            to_level=to_level,
            frozen_stats=frozen_stats,
            frozen_stat_points=frozen_stat_points,
        )

    async def _level_auto_growth_in_db(
        self,
        db,
        user_id: int,
        level: int,
    ) -> dict[str, int]:
        cursor = await db.execute(
            """
            SELECT auto_growth_json
            FROM level_up_logs
            WHERE user_pk = ? AND to_level = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, level),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return {}
        return self._load_stats_json(row["auto_growth_json"])

    def _roll_spent_stat_freeze(
        self,
        stats: dict[str, int],
        spent_points: int,
    ) -> dict[str, int]:
        frozen_stats: dict[str, int] = {}
        for _ in range(spent_points):
            eligible_stats = [
                stat_name
                for stat_name in STAT_NAMES
                if stats[stat_name] > config.INITIAL_STATS[stat_name]
            ]
            if not eligible_stats:
                break
            stat_name = random.choice(eligible_stats)
            rolled = random.randint(*config.STAT_POINT_RANGES[stat_name])
            available = stats[stat_name] - config.INITIAL_STATS[stat_name]
            amount = min(rolled, available)
            if amount <= 0:
                continue
            stats[stat_name] -= amount
            frozen_stats[stat_name] = frozen_stats.get(stat_name, 0) + amount
        return frozen_stats

    def _load_stats_json(self, raw_json: str) -> dict[str, int]:
        try:
            raw_stats = json.loads(raw_json or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(raw_stats, dict):
            return {}
        stats: dict[str, int] = {}
        for stat_name, amount in raw_stats.items():
            if stat_name not in STAT_NAMES:
                continue
            try:
                normalized_amount = int(amount)
            except (TypeError, ValueError):
                continue
            if normalized_amount > 0:
                stats[stat_name] = normalized_amount
        return stats

    def _roll_auto_growth(self) -> dict[str, int]:
        min_count, max_count = config.AUTO_GROWTH_STAT_COUNT_RANGE
        count = random.randint(min_count, max_count)
        selected = random.sample(list(config.AUTO_GROWTH_RANGES.keys()), count)
        return {
            stat_name: random.randint(*config.AUTO_GROWTH_RANGES[stat_name])
            for stat_name in selected
        }

    def _default_nickname(self, identity: UserIdentity) -> str:
        if identity.platform == "qq_official":
            return identity.user_id
        return identity.nickname or identity.user_id