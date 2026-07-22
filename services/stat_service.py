import json
import random

try:
    from ..models.user import StatPointResult, UserIdentity
    from . import config
    from .db import connect_db
    from .user_service import STAT_STORAGE_COLUMNS, UserService, utc_now_text
except ImportError:
    from models.user import StatPointResult, UserIdentity
    from services import config
    from services.db import connect_db
    from services.user_service import STAT_STORAGE_COLUMNS, UserService, utc_now_text


class StatService:
    def __init__(self, db_path: str, user_service: UserService):
        self.db_path = db_path
        self.user_service = user_service

    def normalize_stat_name(self, raw_name: str) -> str | None:
        if not raw_name:
            return None
        text = raw_name.strip()
        return config.STAT_ALIASES.get(text) or config.STAT_ALIASES.get(text.lower())

    async def allocate(
        self,
        identity: UserIdentity,
        raw_stat_name: str,
        points: int,
    ) -> StatPointResult:
        if (raw_stat_name or "").strip().lower() in {
            "速度", "speed", "幸运", "luck",
        }:
            raise ValueError(
                "速度和幸运属于高级属性，不能使用普通属性点；"
                "请使用灵巧或魔力。"
            )
        stat_name = self.normalize_stat_name(raw_stat_name)
        if not stat_name:
            raise ValueError("属性不存在。可用属性：力量、体质、灵巧、感知、魔力、意志")
        if points <= 0:
            raise ValueError("加点数量必须是正整数")

        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            user, _ = await self.user_service.get_or_create_user_in_db(db, identity)
            if user.stat_points < points:
                await db.rollback()
                raise ValueError(f"自定义属性点不足，当前剩余 {user.stat_points} 点")

            rolls = [random.randint(*config.STAT_POINT_RANGES[stat_name]) for _ in range(points)]
            storage_column = STAT_STORAGE_COLUMNS[stat_name]
            total_gain = sum(rolls)
            await db.execute(
                f"""
                UPDATE users
                SET {storage_column} = {storage_column} + ?,
                    stat_points = stat_points - ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (total_gain, points, utc_now_text(), user.id),
            )
            await db.execute(
                """
                INSERT INTO stat_point_logs (
                    user_pk, stat_name, points_spent, rolls_json,
                    total_gain, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    stat_name,
                    points,
                    json.dumps(rolls, ensure_ascii=False),
                    total_gain,
                    utc_now_text(),
                ),
            )
            updated_user = await self.user_service.get_user_by_pk_in_db(db, user.id)
            await db.commit()
            return StatPointResult(
                user=updated_user,
                stat_name=stat_name,
                points_spent=points,
                rolls=rolls,
            )
