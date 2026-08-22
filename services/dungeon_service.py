import json
import random
import time
from dataclasses import dataclass, field

try:
    from ..models.combat import SimulationResult
    from ..models.equipment import EquipmentItem
    from ..models.monster import MonsterSpawnSpec
    from ..models.user import User, UserIdentity
    from . import config
    from .combat_ai import profile_for_strategy
    from .combat_engine import SideviewCombatEngine
    from .combat_state_service import CombatStateService
    from .daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
    )
    from .db import connect_db
    from .dungeon_catalog import DungeonCatalog, DungeonDefinition
    from .dungeon_application_service import DungeonAdventureApplicationService
    from .equipment_service import EquipmentService
    from .monster_build_service import MonsterBuildService
    from .skill_service import SkillService
    from .attribute_service import AttributeService
    from .spell_service import SpellService
    from .user_service import UserService, utc_now_text
    from .build_service import CombatBuildService
except ImportError:
    from models.combat import SimulationResult
    from models.equipment import EquipmentItem
    from models.monster import MonsterSpawnSpec
    from models.user import User, UserIdentity
    from services import config
    from services.combat_ai import profile_for_strategy
    from services.combat_engine import SideviewCombatEngine
    from services.combat_state_service import CombatStateService
    from services.daily_growth_budget import (
        allocate_daily_growth_in_db,
        daily_growth_day_window,
    )
    from services.db import connect_db
    from services.dungeon_catalog import DungeonCatalog, DungeonDefinition
    from services.dungeon_application_service import DungeonAdventureApplicationService
    from services.equipment_service import EquipmentService
    from services.monster_build_service import MonsterBuildService
    from services.skill_service import SkillService
    from services.attribute_service import AttributeService
    from services.spell_service import SpellService
    from services.user_service import UserService, utc_now_text
    from services.build_service import CombatBuildService


@dataclass
class DungeonRunResult:
    dungeon: DungeonDefinition
    user: User
    cleared: bool
    monsters_killed: int
    total_monsters: int
    simulations: list[SimulationResult] = field(default_factory=list)
    rewards: list[EquipmentItem] = field(default_factory=list)
    exp_gain: int = 0
    level_ups: list = field(default_factory=list)
    skill_growths: list = field(default_factory=list)
    attribute_growths: list = field(default_factory=list)
    spell_growths: list = field(default_factory=list)
    player_defeated: bool = False


class DungeonService:
    """PvE dungeon runs reusing the sideview combat engine.

    The legacy command fights monsters one by one in a gauntlet. HP/MP/stamina
    carry over between its waves but never leak into PvP. Killing a
    monster grants skill/attribute growth and a discounted chunk of level EXP.
    """

    def __init__(
        self,
        db_path: str,
        user_service: UserService,
        build_service: CombatBuildService,
        monster_build_service: MonsterBuildService,
        equipment_service: EquipmentService,
        skill_service: SkillService,
        attribute_service: AttributeService,
        spell_service: SpellService,
        combat_engine: SideviewCombatEngine | None = None,
        combat_state_service: CombatStateService | None = None,
        dungeon_catalog: DungeonCatalog | None = None,
    ):
        self.db_path = db_path
        self.user_service = user_service
        self.build_service = build_service
        self.monster_build_service = monster_build_service
        self.equipment_service = equipment_service
        self.skill_service = skill_service
        self.attribute_service = attribute_service
        self.spell_service = spell_service
        self.combat_engine = combat_engine or SideviewCombatEngine()
        self.combat_state_service = combat_state_service or CombatStateService(
            db_path, self.combat_engine
        )
        self.dungeon_catalog = dungeon_catalog or DungeonCatalog(
            monster_catalog=monster_build_service.catalog,
        )
        # Handler-facing persistent application facade for the interactive
        # 3--5 node Nefia.  The legacy one-shot ``run_dungeon`` stays available
        # so old /挑战 副本名 integrations do not break during migration.
        self.adventures = DungeonAdventureApplicationService(
            db_path,
            user_service,
            build_service,
            monster_build_service,
            equipment_service,
            skill_service,
            attribute_service,
            spell_service,
            combat_engine=self.combat_engine,
            dungeon_catalog=self.dungeon_catalog,
        )

    def list_dungeons(self) -> tuple[DungeonDefinition, ...]:
        return self.dungeon_catalog.list()

    def get_dungeon(self, dungeon_id: str) -> DungeonDefinition:
        return self.dungeon_catalog.get(dungeon_id)

    def get_dungeon_by_name(self, name: str) -> DungeonDefinition | None:
        return self.dungeon_catalog.get_by_name(name)

    async def start_nefia(
        self,
        identity: UserIdentity,
        dungeon_id: str,
        difficulty: int = 1,
        strategy: str = "",
        *,
        now_ts: int | None = None,
    ):
        return await self.adventures.start_or_resume(
            identity,
            dungeon_id,
            difficulty,
            strategy,
            now_ts=now_ts,
        )

    async def view_nefia(
        self,
        identity: UserIdentity,
        adventure_id: str = "",
        *,
        dungeon_id: str = "",
        now_ts: int | None = None,
    ):
        return await self.adventures.view(
            identity,
            adventure_id,
            dungeon_id=dungeon_id,
            now_ts=now_ts,
        )

    async def choose_nefia_route(
        self,
        identity: UserIdentity,
        adventure_id: str,
        option_id: str,
        *,
        now_ts: int | None = None,
    ):
        return await self.adventures.choose_route(
            identity, adventure_id, option_id, now_ts=now_ts
        )

    async def choose_nefia_risk(
        self,
        identity: UserIdentity,
        adventure_id: str,
        risk_id: str,
        *,
        now_ts: int | None = None,
    ):
        return await self.adventures.choose_risk(
            identity, adventure_id, risk_id, now_ts=now_ts
        )

    async def fight_nefia(
        self,
        identity: UserIdentity,
        adventure_id: str,
        strategy: str = "",
        *,
        now_ts: int | None = None,
    ):
        return await self.adventures.fight(
            identity, adventure_id, strategy, now_ts=now_ts
        )

    async def retreat_nefia(
        self,
        identity: UserIdentity,
        adventure_id: str,
        *,
        now_ts: int | None = None,
    ):
        return await self.adventures.retreat(
            identity, adventure_id, now_ts=now_ts
        )

    async def run_dungeon(
        self,
        identity: UserIdentity,
        dungeon_id: str,
        strategy: str,
    ) -> DungeonRunResult:
        dungeon = self.dungeon_catalog.get(dungeon_id)
        strategy = (strategy or "").strip()
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                result = await self._run_locked(db, identity, dungeon, strategy)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    async def _run_locked(
        self,
        db,
        identity: UserIdentity,
        dungeon: DungeonDefinition,
        strategy: str,
    ) -> DungeonRunResult:
        user, _ = await self.user_service.get_or_create_user_in_db(db, identity)
        now_ts = int(time.time())
        await self._check_legacy_daily_limit(db, user.id, now_ts)
        await self._check_challenge_limit(db, user.id, now_ts)
        player_snapshot = await self.build_service.snapshot_in_db(db, user, strategy)
        # A dungeon owns its continuation state.  Starting a PvE run is always
        # pristine and the carried state is discarded at the terminal result,
        # preventing dungeon damage/statuses from contaminating PvP.
        player_state = self.combat_state_service.pristine(now_ts)
        # Record the run up-front to obtain a stable id for growth logs.
        now_text = utc_now_text()
        cursor = await db.execute(
            """
            INSERT INTO dungeon_runs
                (user_pk, dungeon_id, cleared, monsters_killed, total_monsters,
                 exp_gain, rewards_json, strategy, created_at, created_at_ts)
            VALUES (?, ?, 0, 0, ?, 0, '[]', ?, ?, ?)
            """,
            (
                user.id,
                dungeon.dungeon_id,
                len(dungeon.waves),
                strategy,
                now_text,
                now_ts,
            ),
        )
        await cursor.close()
        cursor = await db.execute("SELECT last_insert_rowid() AS id")
        run_row = await cursor.fetchone()
        await cursor.close()
        run_id = int(run_row["id"])
        growth_reward_key = f"dungeon-growth:{run_id}"
        await db.execute(
            """
            INSERT INTO reward_ledger (
                reward_key, user_pk, battle_id, source, exp_gain,
                currency_gain, reason, created_at_ts
            ) VALUES (?, ?, NULL, 'dungeon_growth', 0, 0, ?, ?)
            """,
            (
                growth_reward_key,
                user.id,
                json.dumps(
                    {
                        "dungeon_id": dungeon.dungeon_id,
                        "run_id": run_id,
                        "requested_experience": 0,
                        "granted_experience": 0,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now_ts,
            ),
        )

        player_ai = profile_for_strategy(strategy)
        rng = random.SystemRandom()
        simulations: list[SimulationResult] = []
        monsters_killed = 0
        total_exp_gain = 0
        total_requested_exp = 0
        all_skill_growths: list = []
        all_spell_growths: list = []
        all_attribute_growths: list = []
        all_level_ups: list = []
        player_defeated = False

        for wave_index, wave in enumerate(dungeon.waves):
            combatant_pk = -(100_000 + run_id * 100 + wave_index)
            spec = MonsterSpawnSpec(
                template_id=wave.template_id,
                level=wave.level,
                rank=wave.rank,
                combatant_pk=combatant_pk,
            )
            monster_build = self.monster_build_service.build(spec)
            monster_snapshot = monster_build.snapshot
            monster_ai = monster_build.ai_profile
            seed = rng.randrange(0, 2**63)
            simulation = self.combat_engine.simulate(
                player_snapshot,
                monster_snapshot,
                player_ai,
                monster_ai,
                seed,
                player_state,
                None,
                random_environment_pool=(
                    SideviewCombatEngine.SUPPORTED_ENVIRONMENTS
                ),
            )
            simulations.append(simulation)
            player_won = simulation.winner_pk == player_snapshot.user_pk

            # Apply skill / spell / attribute growth for the player only.
            usage = self.skill_service.usage_from_simulation(simulation)
            spell_usage = self.spell_service.usage_from_simulation(simulation)
            player_usage = usage.get(player_snapshot.user_pk, {})
            player_spell_usage = spell_usage.get(player_snapshot.user_pk, {})
            # Apply the same discount rate to skill / spell / attribute growth
            # so PvE is not a multiple of PvP growth efficiency across waves.
            player_usage = self._discount_usage(
                player_usage, dungeon.exp_discount_rate
            )
            player_spell_usage = self._discount_usage(
                player_spell_usage, dungeon.exp_discount_rate
            )
            all_skill_growths.extend(
                await self.skill_service.apply_growth_in_db(
                    db, user.id, player_usage, None
                )
            )
            all_spell_growths.extend(
                await self.spell_service.apply_growth_in_db(
                    db, user.id, player_spell_usage, None
                )
            )
            all_attribute_growths.extend(
                await self.attribute_service.apply_battle_growth_in_db(
                    db, user.id, player_usage, None
                )
            )

            if player_won:
                monsters_killed += 1
                player_state = simulation.attacker_final_state
                # Discounted level EXP for this kill.
                proposed_exp = self._pve_exp_gain(
                    user.level, wave.level, dungeon.exp_discount_rate, rng
                )
                total_requested_exp += proposed_exp
                allocation = await allocate_daily_growth_in_db(
                    db,
                    user_pk=user.id,
                    level=user.level,
                    requested_exp=proposed_exp,
                    at=now_ts,
                )
                exp_gain = allocation.granted
                if exp_gain > 0:
                    exp_result = await self.user_service.add_exp_in_db(
                        db, user, exp_gain
                    )
                    total_exp_gain += exp_gain
                    await db.execute(
                        """
                        UPDATE reward_ledger
                        SET exp_gain = exp_gain + ?
                        WHERE reward_key = ?
                        """,
                        (exp_gain, growth_reward_key),
                    )
                    all_level_ups.extend(exp_result.level_ups)
                    user = exp_result.user
            else:
                player_state = simulation.attacker_final_state
                player_defeated = True
                break

        cleared = monsters_killed >= len(dungeon.waves)

        # Grant equipment rewards.
        rewards: list[EquipmentItem] = []
        if cleared:
            rewards.extend(
                await self._grant_rewards_in_db(
                    db, user.id, dungeon.clear_rewards, rng
                )
            )
        elif monsters_killed > 0:
            rewards.extend(
                await self._grant_partial_rewards_in_db(
                    db, user.id, dungeon.partial_kill_rewards, rng
                )
            )

        # Update the run record with final results.
        rewards_json = json.dumps(
            [
                {
                    "id": item.id,
                    "name": item.name,
                    "item_level": item.item_level,
                    "quality": item.quality,
                    "item_type": item.item_type,
                }
                for item in rewards
            ],
            ensure_ascii=False,
        )
        await db.execute(
            """
            UPDATE reward_ledger SET reason = ? WHERE reward_key = ?
            """,
            (
                json.dumps(
                    {
                        "dungeon_id": dungeon.dungeon_id,
                        "run_id": run_id,
                        "monsters_killed": monsters_killed,
                        "requested_experience": total_requested_exp,
                        "granted_experience": total_exp_gain,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                growth_reward_key,
            ),
        )
        await db.execute(
            """
            UPDATE dungeon_runs SET
                cleared = ?, monsters_killed = ?, exp_gain = ?,
                rewards_json = ?
            WHERE id = ?
            """,
            (
                1 if cleared else 0,
                monsters_killed,
                total_exp_gain,
                rewards_json,
                run_id,
            ),
        )

        return DungeonRunResult(
            dungeon=dungeon,
            user=user,
            cleared=cleared,
            monsters_killed=monsters_killed,
            total_monsters=len(dungeon.waves),
            simulations=simulations,
            rewards=rewards,
            exp_gain=total_exp_gain,
            level_ups=all_level_ups,
            skill_growths=all_skill_growths,
            attribute_growths=all_attribute_growths,
            spell_growths=all_spell_growths,
            player_defeated=player_defeated,
        )

    async def _grant_rewards_in_db(
        self, db, user_pk, spec, rng
    ) -> list[EquipmentItem]:
        results: list[EquipmentItem] = []
        for _ in range(spec.equipment_count):
            item = self.equipment_service.generate_reward(
                user_pk,
                spec.catalog_id_min,
                spec.catalog_id_max,
                spec.equipment_level_min,
                spec.equipment_level_max,
                seed=rng.randrange(0, 2**63),
            )
            await self.equipment_service._insert_item_in_db(db, item)
            results.append(item)
        return results

    async def _grant_partial_rewards_in_db(
        self, db, user_pk, spec, rng
    ) -> list[EquipmentItem]:
        if rng.random() >= spec.chance:
            return []
        return await self._grant_rewards_in_db(db, user_pk, spec, rng)

    @staticmethod
    def _discount_usage(usage: dict[str, int], rate: float) -> dict[str, int]:
        """Scale raw usage by the dungeon exp discount rate (rounded down)."""
        if not usage or rate >= 1:
            return usage
        return {key: max(0, int(value * rate)) for key, value in usage.items()}

    async def _check_challenge_limit(self, db, user_pk: int, now_ts: int) -> None:
        """Enforce the shared 10-minute / 3-challenge limit across PvP and PvE."""
        window = now_ts - config.BATTLE_ACTIVE_CHALLENGE_WINDOW_SECONDS
        cursor = await db.execute(
            "SELECT created_at_ts FROM battles "
            "WHERE attacker_pk = ? AND created_at_ts >= ? "
            "AND COALESCE(is_counterattack, 0) = 0 "
            "ORDER BY created_at_ts ASC",
            (user_pk, window),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        active_count = len(rows)
        cursor = await db.execute(
            "SELECT created_at_ts FROM dungeon_runs "
            "WHERE user_pk = ? AND created_at_ts >= ? "
            "ORDER BY created_at_ts ASC",
            (user_pk, window),
        )
        rows2 = await cursor.fetchall()
        await cursor.close()
        active_count += len(rows2)
        if active_count >= config.BATTLE_ACTIVE_CHALLENGE_LIMIT:
            all_ts = sorted(
                [int(r["created_at_ts"]) for r in rows]
                + [int(r["created_at_ts"]) for r in rows2]
            )
            remain = (
                config.BATTLE_ACTIVE_CHALLENGE_WINDOW_SECONDS
                - (now_ts - all_ts[0])
            )
            raise ValueError(
                "10 分钟内最多挑战 3 次，"
                f"约 {max(1, (remain + 59) // 60)} 分钟后再试"
            )

    @staticmethod
    async def _check_legacy_daily_limit(db, user_pk: int, now_ts: int) -> None:
        """Keep the compatibility gauntlet from becoming an item printer."""
        _, start_ts, end_ts = daily_growth_day_window(now_ts)
        cursor = await db.execute(
            """
            SELECT 1 FROM dungeon_runs
            WHERE user_pk = ? AND created_at_ts >= ? AND created_at_ts < ?
            LIMIT 1
            """,
            (int(user_pk), int(start_ts), int(end_ts)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            raise ValueError(
                "旧固定波次副本每天仅可结算1次（04:00刷新）；"
                "请用 /奈菲亚 继续探索随机路线"
            )

    @staticmethod
    def _pve_exp_gain(
        player_level: int,
        monster_level: int,
        discount_rate: float,
        rng: random.Random,
    ) -> int:
        """Mirror PvP winner-EXP logic then apply the dungeon discount."""
        level_diff = monster_level - player_level
        level_diff_step = (
            config.BATTLE_EXP_TRANSFER_LOWER_LEVEL_RATE_STEP
            if level_diff < 0
            else config.BATTLE_EXP_TRANSFER_LEVEL_DIFF_RATE_STEP
        )
        rate = config.clamp(
            config.BATTLE_EXP_TRANSFER_BASE_RATE
            + level_diff * level_diff_step
            + rng.uniform(*config.BATTLE_EXP_TRANSFER_RANDOM_RATE_RANGE),
            *config.BATTLE_EXP_TRANSFER_RATE_RANGE,
        )
        loser_exp_loss = max(
            1,
            round(config.exp_required_for_next_level(monster_level) * rate),
        )
        level_cap = round(
            config.exp_required_for_next_level(player_level)
            * config.BATTLE_WIN_EXP_LEVEL_CAP_RATE
        )
        reward_floor = max(
            config.BATTLE_WIN_EXP_ABSOLUTE_FLOOR,
            round(
                config.exp_required_for_next_level(monster_level)
                * config.BATTLE_WIN_EXP_LOSER_LEVEL_FLOOR_RATE
            ),
        )
        pvp_equivalent = min(max(0, loser_exp_loss, reward_floor), level_cap)
        return max(1, round(pvp_equivalent * discount_rate))
