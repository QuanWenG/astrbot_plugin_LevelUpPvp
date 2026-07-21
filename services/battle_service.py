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


CUSTOM_STRATEGY_STAT_TARGETS = {
    "hp": "hp",
    "atk": "defense",
    "defense": "atk",
    "speed": "speed",
    "luck": "luck",
}
CUSTOM_STRATEGY_KEYWORDS = (
    ("speed", ("速", "闪", "躲", "先手", "拉扯", "走位", "突袭", "游走")),
    ("atk", ("攻", "爆", "破", "打", "斩", "刀", "杀", "压制", "猛")),
    ("defense", ("防", "守", "盾", "格挡", "反击", "架", "稳")),
    ("hp", ("血", "肉", "耗", "拖", "持久", "续航", "扛")),
    ("luck", ("赌", "运", "奇", "骗", "诈", "乱", "玄", "反转")),
)
CUSTOM_STRATEGY_DEFAULT_STATS = ("atk", "speed", "luck")


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
        (
            attacker_strategy,
            attacker_strategy_random,
            attacker_strategy_custom,
        ) = self._resolve_strategy(strategy)
        (
            defender_strategy,
            defender_strategy_random,
            defender_strategy_custom,
        ) = self._resolve_strategy("")
        custom_strategy_profiles = {}
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

            challenge_limit = await self._check_challenge_limit(
                db,
                attacker.id,
                defender.id,
            )

            if attacker_strategy_custom:
                custom_strategy_profiles[attacker_strategy] = (
                    await self._build_custom_strategy_profile(
                        attacker_strategy,
                        context,
                        event,
                    )
                )
            if defender_strategy_custom:
                custom_strategy_profiles[defender_strategy] = (
                    await self._build_custom_strategy_profile(
                        defender_strategy,
                        context,
                        event,
                    )
                )

            local_analysis = self._local_analysis(
                attacker,
                defender,
                attacker_strategy,
                defender_strategy,
                custom_strategy_profiles,
            )
            final_analysis = local_analysis
            if context and event:
                llm_analysis = await self.llm_service.analyze_battle(
                    context,
                    event,
                    attacker,
                    defender,
                    attacker_strategy,
                    defender_strategy,
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
            requested_loser_exp_loss = self._roll_loser_exp_loss(winner, loser)
            loser_exp = await self.user_service.deduct_exp_in_db(
                db,
                loser,
                requested_loser_exp_loss,
            )
            loser_exp_loss = abs(loser_exp.exp_delta)
            winner_exp_gain = self._winner_exp_gain_from_loss(winner, loser_exp_loss)
            winner_exp = await self.user_service.add_exp_in_db(db, winner, winner_exp_gain)
            await self.user_service.increment_battle_stats_in_db(db, winner.id, loser.id)

            updated_attacker = await self.user_service.get_user_by_pk_in_db(db, attacker.id)
            updated_defender = await self.user_service.get_user_by_pk_in_db(db, defender.id)
            updated_winner = (
                updated_attacker if winner.id == updated_attacker.id else updated_defender
            )
            updated_loser = (
                updated_attacker if loser.id == updated_attacker.id else updated_defender
            )
            battle_log = self._result_battle_log(
                updated_attacker,
                updated_defender,
                updated_winner,
                updated_loser,
                attacker_strategy,
                defender_strategy,
            )
            if context and event:
                llm_battle_log = await self.llm_service.describe_battle_result(
                    context,
                    event,
                    updated_attacker,
                    updated_defender,
                    updated_winner,
                    updated_loser,
                    attacker_strategy,
                    defender_strategy,
                    winner_exp_gain,
                    loser_exp_loss,
                )
                if llm_battle_log:
                    battle_log = llm_battle_log

            now_ts = int(time.time())
            await db.execute(
                """
                INSERT INTO battles (
                    group_id, attacker_pk, defender_pk, winner_pk, loser_pk,
                    attacker_win_rate, roll_value, strategy, winner_exp_gain,
                    loser_exp_loss, analysis, battle_log, llm_raw_result,
                    source, is_counterattack, countered_battle_id,
                    created_at, created_at_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attacker.group_id,
                    attacker.id,
                    defender.id,
                    winner.id,
                    loser.id,
                    final_analysis.attacker_win_rate,
                    roll_value,
                    json.dumps(
                        {
                            "attacker": attacker_strategy,
                            "defender": defender_strategy,
                            "attacker_random": attacker_strategy_random,
                            "defender_random": defender_strategy_random,
                            "attacker_custom": attacker_strategy_custom,
                            "defender_custom": defender_strategy_custom,
                            "custom_profiles": custom_strategy_profiles,
                        },
                        ensure_ascii=False,
                    ),
                    winner_exp_gain,
                    loser_exp_loss,
                    final_analysis.analysis,
                    json.dumps(battle_log, ensure_ascii=False),
                    final_analysis.raw_result,
                    final_analysis.source,
                    1 if challenge_limit["is_counterattack"] else 0,
                    challenge_limit["countered_battle_id"],
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
                attacker_strategy=attacker_strategy,
                defender_strategy=defender_strategy,
                attacker_strategy_random=attacker_strategy_random,
                defender_strategy_random=defender_strategy_random,
                attacker_win_rate=final_analysis.attacker_win_rate,
                roll_value=roll_value,
                winner_exp_gain=winner_exp_gain,
                loser_exp_loss=loser_exp_loss,
                analysis=final_analysis.analysis,
                battle_log=battle_log,
                level_ups=winner_exp.level_ups,
                level_downs=loser_exp.level_downs,
                source=final_analysis.source,
                target_created=defender_created,
                is_counterattack=challenge_limit["is_counterattack"],
            )

    async def _check_challenge_limit(
        self,
        db,
        attacker_id: int,
        defender_id: int,
    ) -> dict:
        now_ts = int(time.time())
        counterattack = await self._find_counterattack_opportunity(
            db,
            attacker_id,
            defender_id,
            now_ts,
        )
        if counterattack:
            return counterattack

        cursor = await db.execute(
            """
            SELECT created_at_ts FROM battles
            WHERE attacker_pk = ?
              AND created_at_ts >= ?
              AND COALESCE(is_counterattack, 0) = 0
            ORDER BY created_at_ts ASC
            """,
            (attacker_id, now_ts - config.BATTLE_ACTIVE_CHALLENGE_WINDOW_SECONDS),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if len(rows) >= config.BATTLE_ACTIVE_CHALLENGE_LIMIT:
            remain = (
                config.BATTLE_ACTIVE_CHALLENGE_WINDOW_SECONDS
                - (now_ts - rows[0]["created_at_ts"])
            )
            raise ValueError(
                "10 分钟内最多主动挑战别人 3 次，"
                f"约 {max(1, (remain + 59) // 60)} 分钟后再试"
            )
        return {"is_counterattack": False, "countered_battle_id": None}

    async def _find_counterattack_opportunity(
        self,
        db,
        attacker_id: int,
        defender_id: int,
        now_ts: int,
    ) -> dict | None:
        cursor = await db.execute(
            """
            SELECT id, attacker_pk, created_at_ts FROM battles
            WHERE defender_pk = ?
              AND COALESCE(is_counterattack, 0) = 0
            ORDER BY created_at_ts DESC, id DESC
            LIMIT 1
            """,
            (attacker_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        if now_ts - row["created_at_ts"] >= config.BATTLE_COUNTERATTACK_WINDOW_SECONDS:
            return None
        if row["attacker_pk"] != defender_id:
            return None

        cursor = await db.execute(
            """
            SELECT id FROM battles
            WHERE countered_battle_id = ?
            LIMIT 1
            """,
            (row["id"],),
        )
        used_row = await cursor.fetchone()
        await cursor.close()
        if used_row:
            return None
        return {"is_counterattack": True, "countered_battle_id": row["id"]}

    def _local_analysis(
        self,
        attacker: User,
        defender: User,
        attacker_strategy: str,
        defender_strategy: str,
        custom_strategy_profiles: dict | None = None,
    ) -> BattleAnalysis:
        custom_strategy_profiles = custom_strategy_profiles or {}
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
        attacker_effect = self._strategy_attribute_effect(
            attacker,
            defender,
            attacker_strategy,
            custom_strategy_profiles,
        )
        defender_effect = self._strategy_attribute_effect(
            defender,
            attacker,
            defender_strategy,
            custom_strategy_profiles,
        )
        strategy_bonus = self._strategy_counter_bonus(
            attacker_strategy,
            defender_strategy,
            attacker_effect,
            defender_effect,
            custom_strategy_profiles,
        )
        rate += strategy_bonus + attacker_effect["score"] - defender_effect["score"]
        rate = config.clamp(rate, config.BATTLE_MIN_WIN_RATE, config.BATTLE_MAX_WIN_RATE)

        attacker_type = self._build_type(attacker)
        defender_type = self._build_type(defender)
        attacker_name = self._display_name(attacker)
        defender_name = self._display_name(defender)
        strategy_text = self._strategy_relation_text(
            attacker_strategy,
            defender_strategy,
            strategy_bonus,
            attacker_effect,
            defender_effect,
            custom_strategy_profiles,
        )
        fit_text = (
            f"{self._strategy_effect_text(attacker_name, attacker_strategy, attacker_effect)}"
            f"{self._strategy_effect_text(defender_name, defender_strategy, defender_effect)}"
        )
        analysis = (
            f"{attacker_name} 偏{attacker_type}，{defender_name} 偏{defender_type}。"
            f"{strategy_text}{fit_text}胜负仍由临场发挥决定。"
        )
        battle_log = [
            f"{attacker_name} 按照「{attacker_strategy}」展开试探。",
            f"{defender_name} 选择「{defender_strategy}」，依靠{defender_type}构筑应对。",
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
        attacker_strategy: str,
        defender_strategy: str,
    ) -> list[str]:
        attacker_type = self._build_type(attacker)
        defender_type = self._build_type(defender)
        attacker_name = self._display_name(attacker)
        defender_name = self._display_name(defender)
        winner_name = self._display_name(winner)
        loser_name = self._display_name(loser)
        winner_type = self._build_type(winner)
        loser_type = self._build_type(loser)
        opening = random.choice(
            [
                (
                    f"{attacker_name} 以「{attacker_strategy}」开局压近，"
                    f"{defender_name} 立刻摆出「{defender_strategy}」。"
                ),
                (
                    f"{attacker_name} 先声夺人试探破绽，"
                    f"{defender_name} 用「{defender_strategy}」稳住阵脚。"
                ),
                (
                    f"战斗一触即发，{attacker_name} 的{attacker_type}节奏"
                    f"撞上{defender_name} 的{defender_type}应对。"
                ),
            ]
        )
        swing = random.choice(
            [
                f"{winner_name} 抓住一瞬空档，把{winner_type}优势滚成连续攻势。",
                f"{loser_name} 一度稳住局面，却被{winner_name} 读到下一步动作。",
                f"双方节奏几次互换，{winner_name} 靠临场判断抢回主动。",
            ]
        )
        finish = random.choice(
            [
                f"最终回合：{winner_name} 完成关键一击，{loser_name} 被迫退场。",
                f"尘埃落定，{winner_name} 以更稳的执行拿下胜利。",
                f"最后的破绽只出现一瞬，{winner_name} 把它变成胜负手。",
            ]
        )
        return [opening, swing, finish]

    def _roll_loser_exp_loss(self, winner: User, loser: User) -> int:
        rate = config.clamp(
            config.BATTLE_EXP_TRANSFER_BASE_RATE
            + (loser.level - winner.level)
            * config.BATTLE_EXP_TRANSFER_LEVEL_DIFF_RATE_STEP
            + random.uniform(*config.BATTLE_EXP_TRANSFER_RANDOM_RATE_RANGE),
            *config.BATTLE_EXP_TRANSFER_RATE_RANGE,
        )
        required = config.exp_required_for_next_level(loser.level)
        return max(1, round(required * rate))

    def _winner_exp_gain_from_loss(self, winner: User, loser_exp_loss: int) -> int:
        level_cap = round(
            config.exp_required_for_next_level(winner.level)
            * config.BATTLE_WIN_EXP_LEVEL_CAP_RATE
        )
        return min(max(0, loser_exp_loss), level_cap)

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

    def _resolve_strategy(self, raw_strategy: str) -> tuple[str, bool, bool]:
        text = (raw_strategy or "").strip()
        if not text:
            return random.choice(config.BATTLE_STRATEGY_NAMES), True, False
        for strategy in config.BATTLE_STRATEGY_NAMES:
            if text == strategy or strategy in text:
                return strategy, False, False
        for alias, strategy in config.BATTLE_STRATEGY_ALIASES.items():
            if alias in text:
                return strategy, False, False
        return text[:32], False, True

    def _strategy_counter_bonus(
        self,
        attacker_strategy: str,
        defender_strategy: str,
        attacker_effect: dict,
        defender_effect: dict,
        custom_strategy_profiles: dict | None = None,
    ) -> float:
        custom_strategy_profiles = custom_strategy_profiles or {}
        bonus = 0.0
        attacker_counters = self._strategy_counters(
            attacker_strategy,
            custom_strategy_profiles,
        )
        defender_counters = self._strategy_counters(
            defender_strategy,
            custom_strategy_profiles,
        )
        if defender_strategy in attacker_counters:
            bonus += 0.08 if attacker_effect["ready"] else -0.06
        if attacker_strategy in defender_counters:
            bonus -= 0.08 if defender_effect["ready"] else -0.06
        return bonus

    def _strategy_attribute_effect(
        self,
        user: User,
        opponent: User,
        strategy: str,
        custom_strategy_profiles: dict | None = None,
    ) -> dict:
        custom_strategy_profiles = custom_strategy_profiles or {}
        rules = self._strategy_attribute_rules(strategy, custom_strategy_profiles)
        score = 0.0
        passed = []
        failed = []
        critical_failed = False
        for rule in rules:
            (
                own_stat,
                opponent_stat,
                own_factor,
                opponent_factor,
                margin,
                success_bonus,
                fail_penalty,
                critical,
            ) = rule
            own_value = getattr(user, own_stat) * own_factor
            required_value = getattr(opponent, opponent_stat) * opponent_factor + margin
            label = config.STAT_LABELS.get(own_stat, own_stat)
            if own_value >= required_value:
                score += success_bonus
                passed.append(label)
            else:
                score += fail_penalty
                failed.append(label)
                if critical:
                    critical_failed = True
        required_pass_count = max(1, (len(rules) + 1) // 2)
        ready = not critical_failed and len(passed) >= required_pass_count
        return {
            "score": score,
            "ready": ready,
            "passed": passed,
            "failed": failed,
            "critical_failed": critical_failed,
        }

    async def _build_custom_strategy_profile(
        self,
        strategy: str,
        context=None,
        event=None,
    ) -> dict:
        llm_profile = None
        if context and event:
            llm_profile = await self.llm_service.analyze_custom_strategy(
                context,
                event,
                strategy,
            )
        if not llm_profile:
            llm_profile = self._fallback_custom_strategy_profile(strategy)

        primary_stats = self._normalize_custom_stats(
            llm_profile.get("primary_stats", ()),
        )
        counters = tuple(
            strategy_name
            for strategy_name in llm_profile.get("counters", ())
            if strategy_name in config.BATTLE_STRATEGY_NAMES
        )
        return {
            "primary_stats": primary_stats,
            "counters": counters,
            "rules": self._custom_strategy_rules(primary_stats),
        }

    def _fallback_custom_strategy_profile(self, strategy: str) -> dict:
        text = strategy or ""
        stats = []
        for stat, keywords in CUSTOM_STRATEGY_KEYWORDS:
            if any(keyword in text for keyword in keywords):
                stats.append(stat)
        primary_stats = self._normalize_custom_stats(stats)
        counters = self._infer_custom_counters(primary_stats)
        return {
            "primary_stats": primary_stats,
            "counters": counters,
        }

    def _normalize_custom_stats(self, stats) -> tuple[str, str, str]:
        normalized = []
        for stat in stats:
            if stat in config.STAT_LABELS and stat not in normalized:
                normalized.append(stat)
        for stat in CUSTOM_STRATEGY_DEFAULT_STATS:
            if stat not in normalized:
                normalized.append(stat)
            if len(normalized) >= 3:
                break
        return tuple(normalized[:3])

    def _custom_strategy_rules(self, primary_stats: tuple[str, str, str]) -> tuple:
        rules = []
        for index, stat in enumerate(primary_stats):
            opponent_stat = CUSTOM_STRATEGY_STAT_TARGETS[stat]
            success_bonus = 0.028 if index == 0 else 0.018
            fail_penalty = -0.038 if index == 0 else -0.02
            margin = 1 if index == 0 and stat in {"atk", "speed", "luck"} else 0
            opponent_factor = 0.9 if stat in {"defense", "hp"} else 1.0
            rules.append(
                (
                    stat,
                    opponent_stat,
                    1.0,
                    opponent_factor,
                    margin,
                    success_bonus,
                    fail_penalty,
                    index == 0,
                )
            )
        return tuple(rules)

    def _infer_custom_counters(self, primary_stats: tuple[str, ...]) -> tuple[str, ...]:
        counter_candidates = []
        custom_types = {config.STAT_LABELS[stat] for stat in primary_stats}
        for strategy, build_types in config.BATTLE_STRATEGY_BUILD_TYPES.items():
            overlap = len(custom_types.intersection(build_types))
            if overlap:
                counter_candidates.append((overlap, strategy))
        counter_candidates.sort(key=lambda item: (-item[0], item[1]))
        return tuple(strategy for _, strategy in counter_candidates[:3])

    def _strategy_attribute_rules(
        self,
        strategy: str,
        custom_strategy_profiles: dict,
    ) -> tuple:
        custom_profile = custom_strategy_profiles.get(strategy)
        if custom_profile:
            return custom_profile["rules"]
        return config.BATTLE_STRATEGY_ATTRIBUTE_RULES.get(strategy, ())

    def _strategy_counters(
        self,
        strategy: str,
        custom_strategy_profiles: dict,
    ) -> tuple:
        custom_profile = custom_strategy_profiles.get(strategy)
        if custom_profile:
            return custom_profile["counters"]
        return config.BATTLE_STRATEGY_COUNTERS.get(strategy, ())

    def _strategy_relation_text(
        self,
        attacker_strategy: str,
        defender_strategy: str,
        bonus: float,
        attacker_effect: dict,
        defender_effect: dict,
        custom_strategy_profiles: dict | None = None,
    ) -> str:
        custom_strategy_profiles = custom_strategy_profiles or {}
        attacker_names_counter = defender_strategy in self._strategy_counters(
            attacker_strategy,
            custom_strategy_profiles,
        )
        defender_names_counter = attacker_strategy in self._strategy_counters(
            defender_strategy,
            custom_strategy_profiles,
        )
        if bonus > 0:
            if attacker_names_counter and attacker_effect["ready"]:
                return f"「{attacker_strategy}」条件达标，对「{defender_strategy}」形成克制。"
            if defender_names_counter and not defender_effect["ready"]:
                return f"「{defender_strategy}」尝试应对但条件不足，反给攻击方机会。"
            return "攻击方策略执行质量略好。"
        if bonus < 0:
            if defender_names_counter and defender_effect["ready"]:
                return f"「{defender_strategy}」条件达标，对「{attacker_strategy}」形成克制。"
            if attacker_names_counter and not attacker_effect["ready"]:
                return f"「{attacker_strategy}」条件不足，强行执行受到反噬。"
            return "防守方策略执行质量略好。"
        return f"「{attacker_strategy}」与「{defender_strategy}」没有明显克制。"

    def _strategy_effect_text(self, name: str, strategy: str, effect: dict) -> str:
        passed = "、".join(effect["passed"][:2]) or "无明显属性"
        failed = "、".join(effect["failed"][:2])
        if failed:
            return f"{name}执行「{strategy}」时{passed}达标，{failed}不足。"
        return f"{name}执行「{strategy}」时{passed}达标。"
