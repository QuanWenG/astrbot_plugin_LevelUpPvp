import json
import random
import time

try:
    from ..models.battle import BattleAnalysis, BattleResult
    from ..models.user import User, UserIdentity
    from . import config
    from .db import connect_db
    from .llm_service import LLMService
    from .user_service import UserService, utc_now_text
except ImportError:
    from models.battle import BattleAnalysis, BattleResult
    from models.user import User, UserIdentity
    from services import config
    from services.db import connect_db
    from services.llm_service import LLMService
    from services.user_service import UserService, utc_now_text


class BattleService:
    def __init__(
        self,
        db_path: str,
        user_service: UserService,
        llm_service: LLMService,
    ):
        self.db_path = db_path
        self.user_service = user_service
        self.llm_service = llm_service

    async def battle(
        self,
        attacker_identity: UserIdentity,
        defender_identity: UserIdentity,
        strategy: str,
        context=None,
        event=None,
    ) -> BattleResult:
        strategy = (strategy or "").strip()
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            attacker, _ = await self.user_service.get_or_create_user_in_db(
                db, attacker_identity
            )
            defender, defender_created = await self.user_service.get_or_create_user_in_db(
                db, defender_identity
            )
            if attacker.id == defender.id:
                await db.rollback()
                raise ValueError("不能挑战自己")

            await self._check_cooldown(db, attacker.id, defender.id)

            local_analysis = self._local_analysis(attacker, defender, strategy)
            final_analysis = local_analysis
            if context and event:
                llm_analysis = await self.llm_service.analyze_battle(
                    context,
                    event,
                    attacker,
                    defender,
                    strategy,
                    local_analysis.attacker_win_rate,
                )
                if llm_analysis:
                    mixed_rate = (
                        local_analysis.attacker_win_rate * (1 - config.LLM_RATE_WEIGHT)
                        + llm_analysis.attacker_win_rate * config.LLM_RATE_WEIGHT
                    )
                    final_analysis = BattleAnalysis(
                        attacker_win_rate=config.clamp(
                            mixed_rate,
                            config.BATTLE_MIN_WIN_RATE,
                            config.BATTLE_MAX_WIN_RATE,
                        ),
                        analysis=llm_analysis.analysis,
                        battle_log=llm_analysis.battle_log or local_analysis.battle_log,
                        raw_result=llm_analysis.raw_result,
                        source="llm",
                    )

            roll_value = random.random()
            attacker_wins = roll_value < final_analysis.attacker_win_rate
            winner = attacker if attacker_wins else defender
            loser = defender if attacker_wins else attacker
            battle_log = self._result_battle_log(
                attacker,
                defender,
                winner,
                loser,
                strategy,
            )
            winner_exp_gain = (
                config.BATTLE_WIN_EXP_BASE
                + loser.level * config.BATTLE_WIN_EXP_PER_LOSER_LEVEL
            )
            loser_exp_loss = (
                config.BATTLE_LOSE_EXP_BASE
                + loser.level * config.BATTLE_LOSE_EXP_PER_LOSER_LEVEL
            )

            winner_exp = await self.user_service.add_exp_in_db(db, winner, winner_exp_gain)
            await self.user_service.deduct_exp_in_db(db, loser, loser_exp_loss)
            await self.user_service.increment_battle_stats_in_db(db, winner.id, loser.id)

            updated_attacker = await self.user_service.get_user_by_pk_in_db(db, attacker.id)
            updated_defender = await self.user_service.get_user_by_pk_in_db(db, defender.id)
            updated_winner = (
                updated_attacker if winner.id == updated_attacker.id else updated_defender
            )
            updated_loser = (
                updated_attacker if loser.id == updated_attacker.id else updated_defender
            )

            now_ts = int(time.time())
            await db.execute(
                """
                INSERT INTO battles (
                    group_id, attacker_pk, defender_pk, winner_pk, loser_pk,
                    attacker_win_rate, roll_value, strategy, winner_exp_gain,
                    loser_exp_loss, analysis, battle_log, llm_raw_result,
                    source, created_at, created_at_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attacker.group_id,
                    attacker.id,
                    defender.id,
                    winner.id,
                    loser.id,
                    final_analysis.attacker_win_rate,
                    roll_value,
                    strategy,
                    winner_exp_gain,
                    loser_exp_loss,
                    final_analysis.analysis,
                    json.dumps(battle_log, ensure_ascii=False),
                    final_analysis.raw_result,
                    final_analysis.source,
                    utc_now_text(),
                    now_ts,
                ),
            )
            await db.commit()

            return BattleResult(
                attacker=updated_attacker,
                defender=updated_defender,
                winner=updated_winner,
                loser=updated_loser,
                attacker_win_rate=final_analysis.attacker_win_rate,
                roll_value=roll_value,
                winner_exp_gain=winner_exp_gain,
                loser_exp_loss=loser_exp_loss,
                analysis=final_analysis.analysis,
                battle_log=battle_log,
                level_ups=winner_exp.level_ups,
                source=final_analysis.source,
                target_created=defender_created,
            )

    async def _check_cooldown(self, db, attacker_id: int, defender_id: int) -> None:
        now_ts = int(time.time())
        cursor = await db.execute(
            """
            SELECT created_at_ts FROM battles
            WHERE (attacker_pk = ? OR defender_pk = ?)
            ORDER BY created_at_ts DESC
            LIMIT 1
            """,
            (attacker_id, attacker_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row and now_ts - row["created_at_ts"] < config.BATTLE_USER_COOLDOWN_SECONDS:
            remain = config.BATTLE_USER_COOLDOWN_SECONDS - (now_ts - row["created_at_ts"])
            raise ValueError(f"你还在战斗冷却中，约 {remain // 60 + 1} 分钟后再试")

        cursor = await db.execute(
            """
            SELECT created_at_ts FROM battles
            WHERE (
                (attacker_pk = ? AND defender_pk = ?)
                OR (attacker_pk = ? AND defender_pk = ?)
            )
            ORDER BY created_at_ts DESC
            LIMIT 1
            """,
            (attacker_id, defender_id, defender_id, attacker_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row and now_ts - row["created_at_ts"] < config.BATTLE_PAIR_COOLDOWN_SECONDS:
            remain = config.BATTLE_PAIR_COOLDOWN_SECONDS - (now_ts - row["created_at_ts"])
            raise ValueError(f"双方刚打过，约 {remain // 60 + 1} 分钟后再挑战")

    def _local_analysis(self, attacker: User, defender: User, strategy: str) -> BattleAnalysis:
        rate = 0.5
        rate += config.clamp(
            (attacker.level - defender.level) * config.BATTLE_LEVEL_RATE_STEP,
            -config.BATTLE_LEVEL_RATE_MAX,
            config.BATTLE_LEVEL_RATE_MAX,
        )
        rate += config.clamp(
            (attacker.luck - defender.luck) * config.BATTLE_LUCK_RATE_STEP,
            -config.BATTLE_LUCK_RATE_MAX,
            config.BATTLE_LUCK_RATE_MAX,
        )
        rate += self._matchup_bonus(attacker, defender)
        rate -= self._matchup_bonus(defender, attacker)
        rate += self._strategy_bonus(attacker, defender, strategy)
        rate = config.clamp(rate, config.BATTLE_MIN_WIN_RATE, config.BATTLE_MAX_WIN_RATE)

        attacker_type = self._build_type(attacker)
        defender_type = self._build_type(defender)
        attacker_name = self._display_name(attacker)
        defender_name = self._display_name(defender)
        analysis = (
            f"{attacker_name} 偏{attacker_type}，{defender_name} 偏{defender_type}。"
            f"策略{'契合' if self._strategy_bonus(attacker, defender, strategy) >= 0 else '略显别扭'}，"
            "胜负仍由临场发挥决定。"
        )
        battle_log = [
            f"{attacker_name} 按照「{strategy or '稳扎稳打'}」展开试探。",
            f"{defender_name} 依靠{defender_type}构筑稳住节奏。",
            "战局在属性克制和随机变数之间摇摆，最后一击决定了结果。",
        ]
        return BattleAnalysis(
            attacker_win_rate=rate,
            analysis=analysis,
            battle_log=battle_log,
            source="local",
        )

    def _result_battle_log(
        self,
        attacker: User,
        defender: User,
        winner: User,
        loser: User,
        strategy: str,
    ) -> list[str]:
        attacker_wins = winner.id == attacker.id
        attacker_type = self._build_type(attacker)
        defender_type = self._build_type(defender)
        attacker_name = self._display_name(attacker)
        defender_name = self._display_name(defender)
        opening = f"{attacker_name} 采用「{strategy or '稳扎稳打'}」试探，{defender_name} 以{defender_type}构筑应对。"
        swing = (
            f"{attacker_name} 利用{attacker_type}优势逐步抢到节奏。"
            if attacker_wins
            else f"{defender_name} 扛住开局压力，抓住随机变数完成反制。"
        )
        finish = (
            f"最终回合：{attacker_name} 打穿防线，攻击方获胜。"
            if attacker_wins
            else f"最终回合：{defender_name} 反击得手，防守方获胜。"
        )
        return [opening, swing, finish]

    def _display_name(self, user: User) -> str:
        name = user.nickname or user.user_id
        if name == user.user_id and len(name) > 8:
            return f"{name[:3]}...{name[-2:]}"
        return name

    def _build_type(self, user: User) -> str:
        stats = user.stats()
        values = list(stats.values())
        if max(values) - min(values) <= 3:
            return "均衡"
        top = max(stats, key=stats.get)
        return config.STAT_LABELS[top]

    def _matchup_bonus(self, user: User, opponent: User) -> float:
        user_type = self._build_type(user)
        opponent_type = self._build_type(opponent)
        bonus = 0.0
        if user_type == "速度" and opponent_type == "攻击":
            bonus += 0.09
        if user_type == "防御" and opponent_type == "攻击":
            bonus += 0.08
        if user_type == "攻击" and opponent_type in {"生命", "幸运"}:
            bonus += 0.06
        if user_type == "幸运" and opponent_type in {"防御", "生命"}:
            bonus += 0.05
        if user_type == "生命" and opponent_type == "速度":
            bonus += 0.04
        return bonus

    def _strategy_bonus(self, user: User, opponent: User, strategy: str) -> float:
        text = (strategy or "").lower()
        if not text:
            return 0.0
        user_type = self._build_type(user)
        bonus = 0.0
        if user_type == "速度" and any(k in text for k in ["游走", "闪避", "拉扯", "先手", "消耗"]):
            bonus += 0.06
        if user_type == "攻击" and any(k in text for k in ["爆发", "强攻", "速攻", "进攻"]):
            bonus += 0.06
        if user_type in {"防御", "生命"} and any(k in text for k in ["防守", "拖", "反击", "消耗", "持久"]):
            bonus += 0.06
        if user_type == "幸运" and any(k in text for k in ["奇袭", "冒险", "扰乱", "赌"]):
            bonus += 0.04
        if user.hp < opponent.atk * 2 and any(k in text for k in ["硬扛", "硬抗", "正面硬拼"]):
            bonus -= 0.05
        return bonus
