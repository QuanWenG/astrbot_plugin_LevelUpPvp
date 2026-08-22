import asyncio
import json
import logging
import math
import random
import time
from dataclasses import replace
from datetime import datetime, timedelta

try:
    from ..models.battle import BattleResult
    from ..models.user import User, UserIdentity
    from . import config
    from .attribute_service import AttributeService, normalize_attribute_id
    from .battle_report import BattleReportBuilder
    from .build_service import CombatBuildService
    from .equipment_service import EquipmentService
    from .combat_ai import FAMILY_PROFILES, profile_for_strategy
    from .combat_engine import SideviewCombatEngine
    from .combat_random import KeyedEntropy
    from .combat_state_service import CombatStateService
    from .daily_growth_budget import (
        daily_growth_day_window,
        daily_growth_exp_earned_in_db,
    )
    from .db import connect_db
    from .llm_service import LLMService
    from .pvp_economy import RewardContext, decide_pvp_economy
    from .skill_service import SkillService
    from .spell_service import SpellService
    from .tactic_loadout_service import TacticLoadoutService
    from .tactic_rules import TacticPlan
    from .user_service import UserService, utc_now_text
except ImportError:
    from models.battle import BattleResult
    from models.user import User, UserIdentity
    from services import config
    from services.attribute_service import AttributeService, normalize_attribute_id
    from services.battle_report import BattleReportBuilder
    from services.build_service import CombatBuildService
    from services.equipment_service import EquipmentService
    from services.combat_ai import FAMILY_PROFILES, profile_for_strategy
    from services.combat_engine import SideviewCombatEngine
    from services.combat_random import KeyedEntropy
    from services.combat_state_service import CombatStateService
    from services.daily_growth_budget import (
        daily_growth_day_window,
        daily_growth_exp_earned_in_db,
    )
    from services.db import connect_db
    from services.llm_service import LLMService
    from services.pvp_economy import RewardContext, decide_pvp_economy
    from services.skill_service import SkillService
    from services.spell_service import SpellService
    from services.tactic_loadout_service import TacticLoadoutService
    from services.tactic_rules import TacticPlan
    from services.user_service import UserService, utc_now_text


CUSTOM_STRATEGY_KEYWORDS = (
    ("dexterity", ("速", "闪", "躲", "先手", "拉扯", "走位", "突袭", "游走")),
    ("strength", ("近战", "重击", "斩", "刀", "猛")),
    ("perception", ("攻", "爆", "破", "枪", "射", "精准", "暴击")),
    ("constitution", ("防", "守", "盾", "格挡", "反击", "肉", "扛")),
    ("magic", ("魔", "元素", "法术", "奥术")),
    ("willpower", ("恢复", "治疗", "祝福", "精神", "辅助")),
)
CUSTOM_STRATEGY_DEFAULT_STATS = ("perception", "dexterity", "strength")
LOGGER = logging.getLogger(__name__)


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
        operation_service=None,
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
        self.operation_service = operation_service
        self.tactic_loadout_service = TacticLoadoutService(db_path)
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
        random_seed = random.SystemRandom().randrange(0, 2**63)
        entropy = KeyedEntropy(
            self.combat_engine.ruleset.ruleset_id,
            random_seed,
        )
        if strategy:
            (
                attacker_strategy,
                attacker_strategy_random,
                attacker_strategy_custom,
            ) = self._resolve_strategy(
                strategy,
                entropy=entropy,
                actor="attacker",
            )
        else:
            attacker_strategy = ""
            attacker_strategy_random = False
            attacker_strategy_custom = False
        defender_strategy = ""
        defender_strategy_random = False
        defender_strategy_custom = False

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
            attacker_saved_plan = (
                await self.tactic_loadout_service.load_or_migrate_in_db(
                    db,
                    attacker.id,
                    "稳扎稳打",
                )
            )
            defender_saved_plan = (
                await self.tactic_loadout_service.load_or_migrate_in_db(
                    db,
                    defender.id,
                    "稳扎稳打",
                )
            )
            attacker_snapshot = await self.build_service.snapshot_in_db(
                db,
                attacker,
                attacker_strategy
                or self.tactic_loadout_service.format_plan(attacker_saved_plan),
            )
            defender_snapshot = await self.build_service.snapshot_in_db(
                db,
                defender,
                self.tactic_loadout_service.format_plan(defender_saved_plan),
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

        if attacker_strategy:
            attacker_profile = profile_for_strategy(
                attacker_strategy,
                custom_strategy_profiles.get(attacker_strategy),
            )
        else:
            attacker_strategy = self.tactic_loadout_service.format_plan(
                attacker_saved_plan
            )
            attacker_profile = self._profile_for_tactic_plan(
                attacker_saved_plan,
                attacker_strategy,
            )
        defender_strategy = self.tactic_loadout_service.format_plan(
            defender_saved_plan
        )
        defender_profile = self._profile_for_tactic_plan(
            defender_saved_plan,
            defender_strategy,
        )
        simulation = self.combat_engine.simulate(
            attacker_snapshot,
            defender_snapshot,
            attacker_profile,
            defender_profile,
            random_seed,
            None,
            None,
        )
        local_battle_log = self.report_builder.build(simulation)
        # Legacy columns remain readable, but no probability roll participates
        # in the winner.  The canonical event simulation is the single truth.
        legacy_attacker_win_rate = 0.5
        legacy_roll_value = entropy.random(stream="compat.legacy_roll")
        analysis = (
            f"{simulation.ruleset_id} 三阶段模拟持续 "
            f"{simulation.duration_ticks} Tick，环境："
            f"{simulation.environment_id}，结束原因："
            f"{simulation.finish_reason}。胜负完全来自事件时间线。"
        )

        # Re-check the limit at settlement so direct callers cannot race around
        # challenge restrictions. The queue makes the common path strictly FIFO.
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            challenge_limit = await self._check_challenge_limit(
                db,
                attacker.id,
                defender.id,
            )
            attacker = await self.user_service.get_user_by_pk_in_db(db, attacker.id)
            defender = await self.user_service.get_user_by_pk_in_db(db, defender.id)
            winner = attacker if simulation.winner_pk == attacker.id else defender
            loser = defender if winner.id == attacker.id else attacker
            settlement_now = datetime.now()
            season_id, rating_rows = await self._season_users_in_db(
                db,
                attacker.group_id,
                (attacker.id, defender.id),
                settlement_now,
            )
            economy_context = await self._pvp_economy_context_in_db(
                db,
                winner,
                loser,
                rating_rows,
                settlement_now,
            )
            economy = decide_pvp_economy(economy_context)
            winner_exp_gain = economy.winner_exp_gain
            loser_exp_gain = economy.loser_exp_gain
            loser_exp_loss = 0
            winner_exp = await self.user_service.add_exp_in_db(
                db,
                winner,
                winner_exp_gain,
            )
            loser_exp = await self.user_service.add_exp_in_db(
                db,
                loser,
                loser_exp_gain,
            )
            if economy.rated:
                await self._apply_rating_in_db(
                    db,
                    season_id,
                    winner.id,
                    loser.id,
                    economy.winner_rating_delta,
                    economy.loser_rating_delta,
                )
            await self.user_service.increment_battle_stats_in_db(db, winner.id, loser.id)

            attacker_rating_before = rating_rows[attacker.id]["rating"]
            defender_rating_before = rating_rows[defender.id]["rating"]
            attacker_rating_delta = (
                economy.winner_rating_delta
                if winner.id == attacker.id else economy.loser_rating_delta
            )
            defender_rating_delta = (
                economy.winner_rating_delta
                if winner.id == defender.id else economy.loser_rating_delta
            )
            attacker_rating_after = (
                attacker_rating_before + attacker_rating_delta
            )
            defender_rating_after = (
                defender_rating_before + defender_rating_delta
            )
            reward_reason = ",".join(economy.reasons)

            updated_attacker = await self.user_service.get_user_by_pk_in_db(db, attacker.id)
            updated_defender = await self.user_service.get_user_by_pk_in_db(db, defender.id)
            updated_winner = (
                updated_attacker if winner.id == updated_attacker.id else updated_defender
            )
            updated_loser = (
                updated_attacker if loser.id == updated_attacker.id else updated_defender
            )

            # Rated/sparring PvP is a fair snapshot duel.  Persistent HP, MP,
            # statuses and cooldowns belong to dungeon/adventure state only.
            now_ts = int(settlement_now.timestamp())
            await db.execute(
                """
                INSERT INTO battles (
                    group_id, attacker_pk, defender_pk, winner_pk, loser_pk,
                    attacker_win_rate, roll_value, strategy, winner_exp_gain,
                    loser_exp_gain, loser_exp_loss, analysis, battle_log,
                    llm_raw_result,
                    source, is_counterattack, countered_battle_id,
                    battle_mode, engine_version, random_seed, duration_ticks,
                    finish_reason, simulation_json, ruleset_id, environment_id,
                    attacker_rating_before, attacker_rating_after,
                    defender_rating_before, defender_rating_after, rated,
                    reward_reason, attacker_tactic_plan_json,
                    defender_tactic_plan_json, created_at, created_at_ts
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    attacker.group_id,
                    attacker.id,
                    defender.id,
                    winner.id,
                    loser.id,
                    legacy_attacker_win_rate,
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
                    loser_exp_gain,
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
                    simulation.ruleset_id,
                    simulation.environment_id,
                    attacker_rating_before,
                    attacker_rating_after,
                    defender_rating_before,
                    defender_rating_after,
                    1 if economy.rated else 0,
                    reward_reason,
                    json.dumps(
                        {
                            "opening": attacker_profile.tactic_plan[0],
                            "midgame": attacker_profile.tactic_plan[1],
                            "endgame": attacker_profile.tactic_plan[2],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "opening": defender_profile.tactic_plan[0],
                            "midgame": defender_profile.tactic_plan[1],
                            "endgame": defender_profile.tactic_plan[2],
                        },
                        ensure_ascii=False,
                    ),
                    utc_now_text(),
                    now_ts,
                ),
            )
            cursor = await db.execute("SELECT last_insert_rowid() AS id")
            battle_row = await cursor.fetchone()
            await cursor.close()
            battle_id = battle_row["id"]
            if economy.rated:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO reward_ledger (
                        reward_key, user_pk, battle_id, source, exp_gain,
                        currency_gain, reason, created_at_ts
                    ) VALUES (?, ?, ?, 'pvp_rating', 0, 0, ?, ?)
                    """,
                    (
                        economy.rating_reward_key,
                        winner.id,
                        battle_id,
                        "first_pair_duel_rated",
                        now_ts,
                    ),
                )
            for user_pk, gain, reward_key, role in (
                (
                    winner.id,
                    winner_exp_gain,
                    economy.winner_growth_reward_key,
                    "winner",
                ),
                (
                    loser.id,
                    loser_exp_gain,
                    economy.loser_growth_reward_key,
                    "loser",
                ),
            ):
                if gain <= 0:
                    continue
                await db.execute(
                    """
                    INSERT OR IGNORE INTO reward_ledger (
                        reward_key, user_pk, battle_id, source, exp_gain,
                        currency_gain, reason, created_at_ts
                    ) VALUES (?, ?, ?, 'pvp_growth', ?, 0, ?, ?)
                    """,
                    (
                        reward_key,
                        user_pk,
                        battle_id,
                        gain,
                        f"{role}_growth",
                        now_ts,
                    ),
                )
            usage = self.skill_service.usage_from_simulation(simulation)
            spell_usage = self.spell_service.usage_from_simulation(simulation)
            skill_growths = []
            spell_growths = []
            attribute_growths = []
            growth_fighters = (
                (attacker.id, defender.id) if economy.rewarded else ()
            )
            for fighter_pk in growth_fighters:
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

        await self._record_operation_progress(
            battle_id=battle_id,
            attacker=updated_attacker,
            defender=updated_defender,
            simulation=simulation,
            # Operations must follow the same anti-farm admission decision as
            # rating/growth.  A first meeting is still only a spar when either
            # account is unqualified or the level gap is out of bounds.
            eligible=economy.rated,
        )

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
            attacker_win_rate=legacy_attacker_win_rate,
            roll_value=legacy_roll_value,
            winner_exp_gain=winner_exp_gain,
            loser_exp_loss=loser_exp_loss,
            analysis=analysis,
            loser_exp_gain=loser_exp_gain,
            attacker_exp_gain=(
                winner_exp_gain if winner.id == attacker.id else loser_exp_gain
            ),
            defender_exp_gain=(
                winner_exp_gain if winner.id == defender.id else loser_exp_gain
            ),
            rated=economy.rated,
            reward_reason=reward_reason,
            attacker_rating_before=attacker_rating_before,
            attacker_rating_after=attacker_rating_after,
            defender_rating_before=defender_rating_before,
            defender_rating_after=defender_rating_after,
            winner_rating_delta=economy.winner_rating_delta,
            loser_rating_delta=economy.loser_rating_delta,
            battle_log=battle_log,
            level_ups=winner_exp.level_ups,
            level_downs=[],
            source=source,
            target_created=defender_created,
            is_counterattack=challenge_limit["is_counterattack"],
            simulation=simulation,
            skill_growths=skill_growths,
            attribute_growths=attribute_growths,
            spell_growths=spell_growths,
            loser_level_ups=loser_exp.level_ups,
        )

    async def _record_operation_progress(
        self,
        *,
        battle_id: int,
        attacker: User,
        defender: User,
        simulation,
        eligible: bool,
    ) -> None:
        """Best-effort, idempotent projection of a duel into daily/weekly tasks.

        Battle settlement is already committed before this projection runs, so
        an operations outage can never turn a completed duel into a failed
        challenge.  Every event key is stable, allowing a repair job or retry to
        safely replay the projection later.
        """

        if self.operation_service is None or not eligible:
            return
        try:
            fighter_pairs = (
                (attacker, defender),
                (defender, attacker),
            )
            for fighter, opponent in fighter_pairs:
                common = {
                    "user_pk": fighter.id,
                    "group_id": fighter.group_id or "global",
                }
                await self.operation_service.record_event(
                    **common,
                    event_type="pvp_battle",
                    event_key=f"battle:{battle_id}",
                )
                await self.operation_service.record_event(
                    **common,
                    event_type="unique_opponent",
                    event_key=f"opponent:{opponent.id}",
                )
                active_uses = sum(
                    event.actor_pk == fighter.id
                    and event.kind in {"skill_use", "spell_cast_start"}
                    for event in simulation.events
                )
                if active_uses:
                    await self.operation_service.record_event(
                        **common,
                        event_type="active_skill",
                        event_key=f"battle:{battle_id}:active_skill",
                        amount=active_uses,
                    )
                for event_type, event_kind in (
                    ("spell_cast", "spell_cast"),
                    ("guard_action", "guard"),
                    ("fortune_trigger", "fortune_swing"),
                ):
                    event_count = sum(
                        event.actor_pk == fighter.id
                        and event.kind == event_kind
                        for event in simulation.events
                    )
                    if event_count:
                        await self.operation_service.record_event(
                            **common,
                            event_type=event_type,
                            event_key=(
                                f"battle:{battle_id}:{event_type}"
                            ),
                            amount=event_count,
                        )
                if simulation.winner_pk == fighter.id:
                    await self.operation_service.record_event(
                        **common,
                        event_type="battle_win",
                        event_key=f"battle:{battle_id}:win",
                    )
                await self.operation_service.record_event(
                    **common,
                    event_type="environment_unique",
                    event_key=f"environment:{simulation.environment_id}",
                )
                tactic_events = [
                    event
                    for event in simulation.events
                    if event.actor_pk == fighter.id
                    and event.kind == "strategy_trigger"
                ]
                if any(event.skill_id == "endgame" for event in tactic_events):
                    await self.operation_service.record_event(
                        **common,
                        event_type="combat_endgame",
                        event_key=f"battle:{battle_id}:endgame",
                    )
                for family in {
                    event.status_id for event in tactic_events if event.status_id
                }:
                    await self.operation_service.record_event(
                        **common,
                        event_type="stance_unique",
                        event_key=f"stance:{family}",
                    )
            attacker_hp = simulation.attacker_final_state.hp_ratio
            defender_hp = simulation.defender_final_state.hp_ratio
            if abs(attacker_hp - defender_hp) <= 0.15:
                for fighter, _ in fighter_pairs:
                    await self.operation_service.record_event(
                        user_pk=fighter.id,
                        group_id=fighter.group_id or "global",
                        event_type="close_fight",
                        event_key=f"battle:{battle_id}:close",
                    )
            record_score = getattr(
                self.operation_service,
                "record_weekly_simulation",
                None,
            )
            if callable(record_score):
                for fighter, _ in fighter_pairs:
                    await record_score(
                        user_pk=fighter.id,
                        group_id=fighter.group_id or "global",
                        submission_key=f"battle:{battle_id}",
                        score=self._weekly_performance_score(
                            simulation,
                            fighter.id,
                        ),
                    )
        except Exception:
            LOGGER.exception(
                "Battle %s settled but operations projection failed",
                battle_id,
            )

    @staticmethod
    def _weekly_performance_score(simulation, fighter_pk: int) -> int:
        """Normalize one duel to a level-agnostic 0-1000 weekly score."""

        attacker = simulation.attacker.user_pk == fighter_pk
        own_damage = (
            simulation.attacker_damage_dealt
            if attacker else simulation.defender_damage_dealt
        )
        other_damage = (
            simulation.defender_damage_dealt
            if attacker else simulation.attacker_damage_dealt
        )
        final_state = (
            simulation.attacker_final_state
            if attacker else simulation.defender_final_state
        )
        total_damage = max(1, own_damage + other_damage)
        damage_share = max(0.0, min(1.0, own_damage / total_damage))
        hp_ratio = max(
            0.0,
            min(1.0, getattr(final_state, "hp_ratio", 0.0)),
        )
        mana_ratio = max(
            0.0,
            min(1.0, getattr(final_state, "mana_ratio", 0.0)),
        )
        stamina_ratio = max(
            0.0,
            min(1.0, getattr(final_state, "stamina_ratio", 0.0)),
        )
        resource_ratio = (mana_ratio + stamina_ratio) / 2.0
        won = 1.0 if simulation.winner_pk == fighter_pk else 0.0
        return max(
            0,
            min(
                1000,
                round(
                    1000
                    * (
                        0.40 * damage_share
                        + 0.25 * hp_ratio
                        + 0.20 * resource_ratio
                        + 0.15 * won
                    )
                ),
            ),
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

    @staticmethod
    def _pvp_day_window(
        now: datetime | None = None,
    ) -> tuple[str, int, int]:
        return daily_growth_day_window(
            now,
            reset_hour=config.CHECKIN_DAY_RESET_HOUR,
        )

    async def _season_users_in_db(
        self,
        db,
        group_id: str,
        user_pks: tuple[int, int],
        now: datetime | None = None,
    ) -> tuple[int, dict[int, dict[str, int]]]:
        current = now or datetime.now()
        _, day_start_ts, _ = self._pvp_day_window(current)
        day_start = datetime.fromtimestamp(day_start_ts)
        epoch = datetime(2026, 1, 5, config.CHECKIN_DAY_RESET_HOUR)
        season_seconds = 28 * 24 * 60 * 60
        season_index = math.floor(
            (day_start - epoch).total_seconds() / season_seconds
        )
        season_start = epoch + timedelta(days=28 * season_index)
        season_end = season_start + timedelta(days=28)
        season_key = f"{season_start.date().isoformat()}-v11"
        timestamp_text = utc_now_text()
        await db.execute(
            """
            INSERT INTO seasons (
                group_id, season_key, ruleset_id, start_at_ts, end_at_ts,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(group_id, season_key) DO UPDATE SET
                status = 'active', updated_at = excluded.updated_at
            """,
            (
                group_id,
                season_key,
                self.combat_engine.ruleset.ruleset_id,
                int(season_start.timestamp()),
                int(season_end.timestamp()),
                timestamp_text,
                timestamp_text,
            ),
        )
        cursor = await db.execute(
            "SELECT id FROM seasons WHERE group_id = ? AND season_key = ?",
            (group_id, season_key),
        )
        season_id = int((await cursor.fetchone())["id"])
        await cursor.close()
        for user_pk in user_pks:
            await db.execute(
                """
                INSERT OR IGNORE INTO season_users (
                    season_id, user_pk, rating, games, wins, losses,
                    provisional_games, updated_at
                ) VALUES (?, ?, 1000, 0, 0, 0, 0, ?)
                """,
                (season_id, user_pk, timestamp_text),
            )
        placeholders = ",".join("?" for _ in user_pks)
        cursor = await db.execute(
            f"""
            SELECT user_pk, rating, games, wins, losses, provisional_games
            FROM season_users
            WHERE season_id = ? AND user_pk IN ({placeholders})
            """,
            (season_id, *user_pks),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return season_id, {
            int(row["user_pk"]): {
                "rating": round(float(row["rating"])),
                "games": int(row["games"]),
                "wins": int(row["wins"]),
                "losses": int(row["losses"]),
                "provisional_games": int(row["provisional_games"]),
            }
            for row in rows
        }

    async def _pvp_economy_context_in_db(
        self,
        db,
        winner: User,
        loser: User,
        rating_rows: dict[int, dict[str, int]],
        now: datetime | None = None,
    ) -> RewardContext:
        battle_date, day_start_ts, day_end_ts = self._pvp_day_window(now)
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS count FROM battles
            WHERE group_id = ? AND created_at_ts >= ? AND created_at_ts < ?
              AND (
                    (attacker_pk = ? AND defender_pk = ?)
                 OR (attacker_pk = ? AND defender_pk = ?)
              )
            """,
            (
                winner.group_id,
                day_start_ts,
                day_end_ts,
                winner.id,
                loser.id,
                loser.id,
                winner.id,
            ),
        )
        pair_battles = int((await cursor.fetchone())["count"])
        await cursor.close()

        async def player_daily(user_pk: int) -> tuple[int, int, int]:
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM checkins WHERE user_pk = ?",
                (user_pk,),
            )
            checkins = int((await cursor.fetchone())["count"])
            await cursor.close()
            cursor = await db.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE
                        WHEN b.attacker_pk = ? THEN b.defender_pk
                        ELSE b.attacker_pk
                    END) AS opponents
                FROM reward_ledger AS r
                LEFT JOIN battles AS b ON b.id = r.battle_id
                WHERE r.user_pk = ? AND r.source = 'pvp_growth'
                  AND r.created_at_ts >= ? AND r.created_at_ts < ?
                  AND r.exp_gain > 0
                """,
                (user_pk, user_pk, day_start_ts, day_end_ts),
            )
            reward_row = await cursor.fetchone()
            await cursor.close()
            shared_exp = await daily_growth_exp_earned_in_db(
                db,
                user_pk=user_pk,
                day_key=battle_date,
                day_start_ts=day_start_ts,
                day_end_ts=day_end_ts,
            )
            return (
                checkins,
                int(reward_row["opponents"]),
                shared_exp,
            )

        winner_daily = await player_daily(winner.id)
        loser_daily = await player_daily(loser.id)
        return RewardContext(
            group_id=winner.group_id or "global",
            battle_date=battle_date,
            winner_id=str(winner.id),
            loser_id=str(loser.id),
            winner_level=winner.level,
            loser_level=loser.level,
            winner_checkin_days=winner_daily[0],
            loser_checkin_days=loser_daily[0],
            pair_battles_today=pair_battles,
            winner_growth_opponents_today=winner_daily[1],
            loser_growth_opponents_today=loser_daily[1],
            winner_daily_exp_earned=winner_daily[2],
            loser_daily_exp_earned=loser_daily[2],
            winner_rating=rating_rows[winner.id]["rating"],
            loser_rating=rating_rows[loser.id]["rating"],
            winner_games_played=rating_rows[winner.id]["games"],
            loser_games_played=rating_rows[loser.id]["games"],
            ruleset_id=self.combat_engine.ruleset.ruleset_id,
        )

    @staticmethod
    async def _apply_rating_in_db(
        db,
        season_id: int,
        winner_pk: int,
        loser_pk: int,
        winner_delta: int,
        loser_delta: int,
    ) -> None:
        timestamp_text = utc_now_text()
        await db.execute(
            """
            UPDATE season_users
            SET rating = rating + ?, games = games + 1, wins = wins + 1,
                provisional_games = MIN(10, provisional_games + 1),
                updated_at = ?
            WHERE season_id = ? AND user_pk = ?
            """,
            (winner_delta, timestamp_text, season_id, winner_pk),
        )
        await db.execute(
            """
            UPDATE season_users
            SET rating = rating + ?, games = games + 1, losses = losses + 1,
                provisional_games = MIN(10, provisional_games + 1),
                updated_at = ?
            WHERE season_id = ? AND user_pk = ?
            """,
            (loser_delta, timestamp_text, season_id, loser_pk),
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

    async def get_tactic_plan(
        self,
        identity: UserIdentity,
    ) -> TacticPlan:
        """Return the player's persistent three-phase PvP plan.

        Existing accounts did not have a tactic row.  Their first read creates
        the neutral sustain preset transactionally, so subsequent defense AI is
        stable and no longer changes randomly between challenges.
        """

        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                user, _ = await self.user_service.get_or_create_user_in_db(
                    db,
                    identity,
                )
                plan = await self.tactic_loadout_service.load_or_migrate_in_db(
                    db,
                    user.id,
                    "稳扎稳打",
                )
                await db.commit()
                return plan
            except Exception:
                await db.rollback()
                raise

    async def set_tactic_plan(
        self,
        identity: UserIdentity,
        opening: str,
        midgame: str,
        endgame: str,
    ) -> TacticPlan:
        """Validate and persist the player's opening/mid/endgame choices."""

        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                user, _ = await self.user_service.get_or_create_user_in_db(
                    db,
                    identity,
                )
                plan = await self.tactic_loadout_service.set_plan_in_db(
                    db,
                    user.id,
                    opening,
                    midgame,
                    endgame,
                )
                await db.commit()
                return plan
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def _profile_for_tactic_plan(plan: TacticPlan, label: str):
        """Adapt persistent plan data to the engine's compatibility profile."""

        base = FAMILY_PROFILES[plan.opening]
        return replace(
            base,
            strategy_name=label,
            tactic_plan=(
                plan.opening.value,
                plan.midgame.value,
                plan.endgame.value,
            ),
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

    def _resolve_strategy(
        self,
        raw_strategy: str,
        *,
        entropy: KeyedEntropy | None = None,
        actor: str = "strategy",
    ) -> tuple[str, bool, bool]:
        text = (raw_strategy or "").strip()
        if not text:
            if entropy is None:
                # Compatibility for direct unit callers; production battles
                # always inject their persisted root entropy above.
                selected = random.choice(config.BATTLE_STRATEGY_NAMES)
            else:
                selected = entropy.choice(
                    config.BATTLE_STRATEGY_NAMES,
                    stream="battle.strategy",
                    actor=actor,
                )
            return selected, True, False
        for strategy in config.BATTLE_STRATEGY_NAMES:
            if text == strategy or strategy in text:
                return strategy, False, False
        for alias, strategy in config.BATTLE_STRATEGY_ALIASES.items():
            if alias in text:
                return strategy, False, False
        return text[:32], False, True

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
        return {"primary_stats": primary_stats}

    def _fallback_custom_strategy_profile(self, strategy: str) -> dict:
        text = strategy or ""
        stats = []
        for stat, keywords in CUSTOM_STRATEGY_KEYWORDS:
            if any(keyword in text for keyword in keywords):
                stats.append(stat)
        primary_stats = self._normalize_custom_stats(stats)
        return {"primary_stats": primary_stats}

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
