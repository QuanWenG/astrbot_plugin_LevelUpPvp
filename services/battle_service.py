import asyncio
import json
import random
import time

try:
    from ..models.battle import BattleAnalysis, BattleResult
    from ..models.combat import FighterSnapshot
    from ..models.user import User, UserIdentity
    from . import config
    from .attribute_service import AttributeService, normalize_attribute_id
    from .battle_report import BattleReportBuilder
    from .build_service import CombatBuildService
    from .equipment_service import EquipmentService
    from .combat_ai import profile_for_strategy
    from .combat_engine import SideviewCombatEngine
    from .combat_state_service import CombatStateService
    from .db import connect_db
    from .llm_service import LLMService
    from .skill_service import SkillService
    from .spell_service import SpellService
    from .user_service import UserService, utc_now_text
except ImportError:
    from models.battle import BattleAnalysis, BattleResult
    from models.combat import FighterSnapshot
    from models.user import User, UserIdentity
    from services import config
    from services.attribute_service import AttributeService, normalize_attribute_id
    from services.battle_report import BattleReportBuilder
    from services.build_service import CombatBuildService
    from services.equipment_service import EquipmentService
    from services.combat_ai import profile_for_strategy
    from services.combat_engine import SideviewCombatEngine
    from services.combat_state_service import CombatStateService
    from services.db import connect_db
    from services.llm_service import LLMService
    from services.skill_service import SkillService
    from services.spell_service import SpellService
    from services.user_service import UserService, utc_now_text


CUSTOM_STRATEGY_STAT_TARGETS = {
    "strength": "constitution",
    "constitution": "strength",
    "dexterity": "dexterity",
    "perception": "constitution",
    "magic": "magic",
    "willpower": "magic",
}
CUSTOM_STRATEGY_KEYWORDS = (
    ("dexterity", ("速", "闪", "躲", "先手", "拉扯", "走位", "突袭", "游走")),
    ("strength", ("近战", "重击", "斩", "刀", "猛")),
    ("perception", ("攻", "爆", "破", "枪", "射", "精准", "暴击")),
    ("constitution", ("防", "守", "盾", "格挡", "反击", "肉", "扛")),
    ("magic", ("魔", "元素", "法术", "奥术")),
    ("willpower", ("恢复", "治疗", "祝福", "精神", "辅助")),
)
CUSTOM_STRATEGY_DEFAULT_STATS = ("perception", "dexterity", "strength")


class BattleService:
    def __init__(
        self,
        db_path: str,
        user_service: UserService,
        llm_service: LLMService,
        equipment_service=None,
        skill_service=None,
        attribute_service=None,
        spell_service=None,
    ):
        self.db_path = db_path
        self._identity_locks: dict[
            tuple[str, str, str], asyncio.Lock
        ] = {}
        self.user_service = user_service
        self.llm_service = llm_service
        self.equipment_service = equipment_service or EquipmentService(db_path)
        self.skill_service = skill_service or SkillService(db_path)
        self.attribute_service = attribute_service or AttributeService(db_path)
        self.spell_service = spell_service or SpellService(
            db_path, self.skill_service, self.equipment_service, self.attribute_service
        )
        self.build_service = CombatBuildService(
            self.equipment_service, self.skill_service, self.attribute_service,
            self.spell_service,
        )
        self.combat_engine = SideviewCombatEngine()
        self.combat_state_service = CombatStateService(
            db_path, self.combat_engine
        )
        self.report_builder = BattleReportBuilder()

    async def battle(
        self,
        attacker_identity: UserIdentity,
        defender_identity: UserIdentity,
        strategy: str,
        context=None,
        event=None,
    ) -> BattleResult:
        keys = sorted(
            {
                self._identity_lock_key(attacker_identity),
                self._identity_lock_key(defender_identity),
            }
        )
        locks = [
            self._identity_locks.setdefault(key, asyncio.Lock())
            for key in keys
        ]
        for lock in locks:
            await lock.acquire()
        try:
            return await self._battle_locked(
                attacker_identity,
                defender_identity,
                strategy,
                context,
                event,
            )
        finally:
            for lock in reversed(locks):
                lock.release()

    async def _battle_locked(
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

        # Keep persistence locks short. The first transaction creates/loads a
        # stable fighter snapshot and rejects invalid challenges before any LLM work.
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
            await self._check_challenge_limit(db, attacker.id, defender.id)
            attacker_snapshot = await self.build_service.snapshot_in_db(db, attacker, attacker_strategy)
            defender_snapshot = await self.build_service.snapshot_in_db(db, defender, defender_strategy)
            state_now_ts = int(time.time())
            attacker_initial_state = (
                await self.combat_state_service.load_in_db(
                    db, attacker_snapshot, state_now_ts
                )
            )
            defender_initial_state = (
                await self.combat_state_service.load_in_db(
                    db, defender_snapshot, state_now_ts
                )
            )
            await db.commit()

        custom_strategy_profiles = {}
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
        random_seed = random.SystemRandom().randrange(0, 2**63)
        simulation = self.combat_engine.simulate(
            attacker_snapshot,
            defender_snapshot,
            profile_for_strategy(
                attacker_strategy,
                custom_strategy_profiles.get(attacker_strategy),
            ),
            profile_for_strategy(
                defender_strategy,
                custom_strategy_profiles.get(defender_strategy),
            ),
            random_seed,
            attacker_initial_state,
            defender_initial_state,
        )
        local_battle_log = self.report_builder.build(simulation)
        legacy_roll_value = random.Random(random_seed ^ 0x5DEECE66D).random()
        analysis = (
            f"一维横板模拟持续 {simulation.duration_ticks} Tick，"
            f"结束原因：{simulation.finish_reason}。"
        )

        # Re-check the limit at settlement so direct callers cannot race around
        # challenge restrictions. The queue makes the common path strictly FIFO.
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            challenge_limit = await self._check_challenge_limit(
                db,
                attacker.id,
                defender.id,
            )
            attacker = await self.user_service.get_user_by_pk_in_db(db, attacker.id)
            defender = await self.user_service.get_user_by_pk_in_db(db, defender.id)
            winner = attacker if simulation.winner_pk == attacker.id else defender
            loser = defender if winner.id == attacker.id else attacker
            requested_loser_exp_loss = self._roll_loser_exp_loss(winner, loser)
            loser_exp = await self.user_service.deduct_exp_in_db(
                db,
                loser,
                requested_loser_exp_loss,
            )
            loser_exp_loss = abs(loser_exp.exp_delta)
            winner_exp_gain = self._winner_exp_gain_from_loss(
                winner,
                loser,
                loser_exp_loss,
            )
            winner_exp = await self.user_service.add_exp_in_db(
                db,
                winner,
                winner_exp_gain,
            )
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
            await self.combat_state_service.save_in_db(
                db,
                attacker.id,
                simulation.attacker_final_state,
                now_ts,
            )
            await self.combat_state_service.save_in_db(
                db,
                defender.id,
                simulation.defender_final_state,
                now_ts,
            )
            await db.execute(
                """
                INSERT INTO battles (
                    group_id, attacker_pk, defender_pk, winner_pk, loser_pk,
                    attacker_win_rate, roll_value, strategy, winner_exp_gain,
                    loser_exp_loss, analysis, battle_log, llm_raw_result,
                    source, is_counterattack, countered_battle_id,
                    battle_mode, engine_version, random_seed, duration_ticks,
                    finish_reason, simulation_json, created_at, created_at_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attacker.group_id,
                    attacker.id,
                    defender.id,
                    winner.id,
                    loser.id,
                    local_analysis.attacker_win_rate,
                    legacy_roll_value,
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
                    analysis,
                    json.dumps(local_battle_log, ensure_ascii=False),
                    "",
                    "local",
                    1 if challenge_limit["is_counterattack"] else 0,
                    challenge_limit["countered_battle_id"],
                    "sideview",
                    simulation.engine_version,
                    simulation.random_seed,
                    simulation.duration_ticks,
                    simulation.finish_reason,
                    json.dumps(simulation.to_dict(), ensure_ascii=False),
                    utc_now_text(),
                    now_ts,
                ),
            )
            cursor = await db.execute("SELECT last_insert_rowid() AS id")
            battle_row = await cursor.fetchone()
            await cursor.close()
            battle_id = battle_row["id"]
            usage = self.skill_service.usage_from_simulation(simulation)
            spell_usage = self.spell_service.usage_from_simulation(simulation)
            skill_growths = []
            spell_growths = []
            attribute_growths = []
            for fighter_pk in (attacker.id, defender.id):
                skill_growths.extend(
                    await self.skill_service.apply_growth_in_db(
                        db, fighter_pk, usage.get(fighter_pk, {}), battle_id
                    )
                )
                spell_growths.extend(
                    await self.spell_service.apply_growth_in_db(
                        db, fighter_pk, spell_usage.get(fighter_pk, {}), battle_id
                    )
                )
                attribute_growths.extend(
                    await self.attribute_service.apply_battle_growth_in_db(
                        db, fighter_pk, usage.get(fighter_pk, {}), battle_id
                    )
                )
            updated_attacker = await self.user_service.get_user_by_pk_in_db(
                db, attacker.id
            )
            updated_defender = await self.user_service.get_user_by_pk_in_db(
                db, defender.id
            )
            updated_winner = (
                updated_attacker
                if winner.id == updated_attacker.id else updated_defender
            )
            updated_loser = (
                updated_attacker
                if loser.id == updated_attacker.id else updated_defender
            )
            await db.commit()

        battle_log = local_battle_log
        source = "local"
        if context and event:
            llm_battle_log = await self.llm_service.describe_simulation_result(
                context,
                event,
                simulation,
                local_battle_log,
            )
            if llm_battle_log:
                try:
                    async with await connect_db(self.db_path) as db:
                        await db.execute(
                            "UPDATE battles SET battle_log = ?, source = ? WHERE id = ?",
                            (json.dumps(llm_battle_log, ensure_ascii=False), "llm", battle_id),
                        )
                        await db.commit()
                    battle_log = llm_battle_log
                    source = "llm"
                except Exception:
                    # Settlement is already committed; report persistence must not
                    # turn a completed battle into a user-visible failure.
                    battle_log = local_battle_log

        return BattleResult(
            attacker=updated_attacker,
            defender=updated_defender,
            winner=updated_winner,
            loser=updated_loser,
            attacker_strategy=attacker_strategy,
            defender_strategy=defender_strategy,
            attacker_strategy_random=attacker_strategy_random,
            defender_strategy_random=defender_strategy_random,
            attacker_win_rate=local_analysis.attacker_win_rate,
            roll_value=legacy_roll_value,
            winner_exp_gain=winner_exp_gain,
            loser_exp_loss=loser_exp_loss,
            analysis=analysis,
            battle_log=battle_log,
            level_ups=winner_exp.level_ups,
            level_downs=loser_exp.level_downs,
            source=source,
            target_created=defender_created,
            is_counterattack=challenge_limit["is_counterattack"],
            simulation=simulation,
            skill_growths=skill_growths,
            attribute_growths=attribute_growths,
            spell_growths=spell_growths,
        )

    @staticmethod
    def _identity_lock_key(
        identity: UserIdentity,
    ) -> tuple[str, str, str]:
        return (
            str(identity.platform),
            str(identity.group_id),
            str(identity.user_id),
        )

    async def combat_state_view(self, user: User):
        """Return a read-only, recovered preview for the profile panel."""
        now_ts = int(time.time())
        async with await connect_db(self.db_path) as db:
            current_user = await self.user_service.get_user_by_pk_in_db(
                db, user.id
            )
            snapshot = await self.build_service.snapshot_in_db(
                db, current_user, ""
            )
            state = await self.combat_state_service.load_in_db(
                db,
                snapshot,
                now_ts,
                consume_defeat=False,
            )
        return self.combat_state_service.view(snapshot, state, now_ts)

    def _fighter_snapshot(self, user: User, strategy: str) -> FighterSnapshot:
        return FighterSnapshot(
            user_pk=user.id,
            name=self._display_name(user),
            level=user.level,
            hp=user.hp,
            atk=user.atk,
            defense=user.defense,
            speed=user.speed,
            luck=user.luck,
            strategy=strategy,
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
            (attacker.perception - defender.perception) * config.BATTLE_LUCK_RATE_STEP,
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
        level_diff = loser.level - winner.level
        level_diff_step = (
            config.BATTLE_EXP_TRANSFER_LOWER_LEVEL_RATE_STEP
            if level_diff < 0
            else config.BATTLE_EXP_TRANSFER_LEVEL_DIFF_RATE_STEP
        )
        rate = config.clamp(
            config.BATTLE_EXP_TRANSFER_BASE_RATE
            + level_diff * level_diff_step
            + random.uniform(*config.BATTLE_EXP_TRANSFER_RANDOM_RATE_RANGE),
            *config.BATTLE_EXP_TRANSFER_RATE_RANGE,
        )
        required = config.exp_required_for_next_level(loser.level)
        return max(1, round(required * rate))

    def _winner_exp_gain_from_loss(
        self,
        winner: User,
        loser: User,
        loser_exp_loss: int,
    ) -> int:
        level_cap = round(
            config.exp_required_for_next_level(winner.level)
            * config.BATTLE_WIN_EXP_LEVEL_CAP_RATE
        )
        reward_floor = max(
            config.BATTLE_WIN_EXP_ABSOLUTE_FLOOR,
            round(
                config.exp_required_for_next_level(loser.level)
                * config.BATTLE_WIN_EXP_LOSER_LEVEL_FLOOR_RATE
            ),
        )
        return min(max(0, loser_exp_loss, reward_floor), level_cap)

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
            stat = normalize_attribute_id(str(stat))
            if stat and stat not in normalized:
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
            margin = 1 if index == 0 and stat in {"strength", "dexterity", "perception", "magic"} else 0
            opponent_factor = 0.9 if stat in {"constitution", "willpower"} else 1.0
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
