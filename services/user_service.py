import json
import random
from datetime import datetime

try:
    from ..models.user import ExpChangeResult, LevelUpEvent, User, UserIdentity
    from . import config
    from .db import connect_db
except ImportError:
    from models.user import ExpChangeResult, LevelUpEvent, User, UserIdentity
    from services import config
    from services.db import connect_db


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
            return user, False

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
        return row_to_user(row), True

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
        return user

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
            level += 1
            level_up_count += 1
            stat_points += config.STAT_POINTS_PER_LEVEL
            auto_growth = self._roll_auto_growth()
            for stat_name, gain in auto_growth.items():
                stats[stat_name] += gain

            level_up = LevelUpEvent(
                from_level=from_level,
                to_level=level,
                auto_growth=auto_growth,
                stat_points_gain=config.STAT_POINTS_PER_LEVEL,
            )
            level_ups.append(level_up)
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
                user.id,
            ),
        )
        updated = await self.get_user_by_pk_in_db(db, user.id)
        return ExpChangeResult(user=updated, exp_delta=amount, level_ups=level_ups)

    async def deduct_exp_in_db(self, db, user: User, amount: int) -> User:
        loss = max(0, amount)
        exp = max(0, user.exp - loss)
        await db.execute(
            "UPDATE users SET exp = ?, updated_at = ? WHERE id = ?",
            (exp, utc_now_text(), user.id),
        )
        return await self.get_user_by_pk_in_db(db, user.id)

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
