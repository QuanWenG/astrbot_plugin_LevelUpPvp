from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

try:
    from ..models.operation import stable_operation_seed
    from ..models.user import UserIdentity
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS, ability_is_unlocked
    from .daily_growth_budget import daily_growth_day_window
    from .db import connect_db
    from .effect_whitelist import EffectWhitelist
    from .attribute_service import skill_level_cap
    from .skill_catalog import SKILL_DEFINITIONS
except ImportError:
    from models.operation import stable_operation_seed
    from models.user import UserIdentity
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS, ability_is_unlocked
    from services.daily_growth_budget import daily_growth_day_window
    from services.db import connect_db
    from services.effect_whitelist import EffectWhitelist
    from services.attribute_service import skill_level_cap
    from services.skill_catalog import SKILL_DEFINITIONS


@dataclass(frozen=True)
class AutoPilotState:
    user_pk: int
    enabled: bool
    origin_umo: str
    origin_group_id: str
    started_at_ts: int
    last_tick_ts: int
    next_tick_ts: int
    cursor: dict[str, Any] = field(default_factory=dict)
    consecutive_errors: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class AutoPilotPassResult:
    user_pk: int
    enabled: bool
    actions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class AutoPilotService:
    """Persistent, silent per-user automation for the LevelUpPvp loop."""

    TICK_INTERVAL_SECONDS = 45
    STARTUP_DELAY_SECONDS = 5
    MAX_USERS_PER_TICK = 8
    MAX_BOOKS_PER_PASS = 1
    ERROR_BACKOFF_SECONDS = 180

    _TRAINING_PRIORITY = (
        "tactics",
        "shield",
        "light_armor",
        "medium_armor",
        "heavy_armor",
        "dodge",
        "weightlifting",
        "reading",
        "concealment",
    )
    _WEAPON_SKILL_IDS = frozenset(
        {
            "longsword", "shortsword", "axe", "spear", "unarmed", "scythe",
            "blunt", "staff", "bow", "crossbow", "firearm", "throwing",
        }
    )
    _ACTIVE_TAG_ORDER = {
        "heal": 0,
        "defense": 1,
        "cleanse": 2,
        "control": 3,
        "damage": 4,
        "buff": 5,
        "stance": 6,
        "summon": 7,
    }

    def __init__(
        self,
        *,
        db_path: str,
        effect_whitelist: EffectWhitelist,
        user_service,
        stat_service,
        attribute_service,
        skill_service,
        spell_service,
        equipment_service,
        auto_equip_service,
        dungeon_service,
        operation_service=None,
        operation_settlement_service=None,
    ) -> None:
        self.db_path = db_path
        self.effect_whitelist = effect_whitelist
        self.user_service = user_service
        self.stat_service = stat_service
        self.attribute_service = attribute_service
        self.skill_service = skill_service
        self.spell_service = spell_service
        self.equipment_service = equipment_service
        self.auto_equip_service = auto_equip_service
        self.dungeon_service = dungeon_service
        self.operation_service = operation_service
        self.operation_settlement_service = operation_settlement_service
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._user_locks: dict[int, asyncio.Lock] = {}

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("auto pilot is closed")
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(),
                name="level-up-pvp-auto-pilot",
            )

    async def shutdown(self) -> None:
        self._closed = True
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._worker = None

    async def enable(
        self,
        identity: UserIdentity,
        *,
        origin_umo: str = "",
    ) -> AutoPilotState:
        user = await self.user_service.get_or_create_user(identity)
        now_ts = int(time.time())
        origin_group_id = str(identity.group_id or "")
        origin_umo = str(origin_umo or "").strip()
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO auto_pilot_state (
                        user_pk, enabled, origin_umo, origin_group_id,
                        started_at_ts, last_tick_ts, next_tick_ts,
                        cursor_json, consecutive_errors, last_error,
                        updated_at_ts
                    ) VALUES (?, 1, ?, ?, ?, 0, ?, '{}', 0, '', ?)
                    ON CONFLICT(user_pk) DO UPDATE SET
                        enabled = 1,
                        origin_umo = excluded.origin_umo,
                        origin_group_id = excluded.origin_group_id,
                        next_tick_ts = excluded.next_tick_ts,
                        consecutive_errors = 0,
                        last_error = '',
                        updated_at_ts = excluded.updated_at_ts
                    """,
                    (
                        int(user.id),
                        origin_umo,
                        origin_group_id,
                        now_ts,
                        now_ts + self.STARTUP_DELAY_SECONDS,
                        now_ts,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        state = await self.get_state(int(user.id))
        if state is None:
            raise RuntimeError("托管状态写入后无法读取")
        return state

    async def disable(self, user_pk: int) -> bool:
        now_ts = int(time.time())
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT enabled FROM auto_pilot_state WHERE user_pk = ?",
                    (int(user_pk),),
                )
                row = await cursor.fetchone()
                await cursor.close()
                await db.execute(
                    """
                    UPDATE auto_pilot_state
                    SET enabled = 0, next_tick_ts = 0, updated_at_ts = ?
                    WHERE user_pk = ?
                    """,
                    (now_ts, int(user_pk)),
                )
                await db.commit()
                return bool(row is not None and row["enabled"])
            except Exception:
                await db.rollback()
                raise

    async def get_state(self, user_pk: int) -> AutoPilotState | None:
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT * FROM auto_pilot_state WHERE user_pk = ?",
                (int(user_pk),),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._state_from_row(row) if row is not None else None

    async def list_enabled_due(self, now_ts: int | None = None) -> list[AutoPilotState]:
        timestamp = int(time.time() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT * FROM auto_pilot_state
                WHERE enabled = 1 AND (next_tick_ts = 0 OR next_tick_ts <= ?)
                ORDER BY next_tick_ts, user_pk
                LIMIT ?
                """,
                (timestamp, self.MAX_USERS_PER_TICK),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._state_from_row(row) for row in rows]

    async def run_pass(
        self,
        user_pk: int,
        *,
        now_ts: int | None = None,
    ) -> AutoPilotPassResult:
        user_pk = int(user_pk)
        now_ts = int(time.time() if now_ts is None else now_ts)
        lock = self._user_locks.setdefault(user_pk, asyncio.Lock())
        async with lock:
            state = await self.get_state(user_pk)
            if state is None or not state.enabled:
                return AutoPilotPassResult(user_pk, False)
            if not self.effect_whitelist.allows(
                unified_msg_origin=state.origin_umo,
                group_id=state.origin_group_id,
            ):
                await self._schedule_state(
                    user_pk,
                    now_ts=now_ts,
                    cursor=state.cursor,
                    errors=(),
                    delay=self.TICK_INTERVAL_SECONDS,
                )
                return AutoPilotPassResult(user_pk, True)

            actions: list[str] = []
            errors: list[str] = []
            operations_due = False
            day_key = daily_growth_day_window(now_ts)[0]
            try:
                user = await self.user_service.get_user_by_pk(user_pk)
                if user is None:
                    raise RuntimeError("托管用户不存在")
                identity = UserIdentity(
                    platform=user.platform,
                    group_id=user.group_id,
                    user_id=user.user_id,
                    nickname=user.nickname,
                )
                await self._run_step(
                    "attributes",
                    lambda: self._allocate_attributes(user, identity),
                    actions,
                    errors,
                )
                user = await self.user_service.get_user_by_pk(user_pk) or user
                await self._run_step(
                    "skills",
                    lambda: self._learn_or_train(user),
                    actions,
                    errors,
                )
                user = await self.user_service.get_user_by_pk(user_pk) or user
                await self._run_step(
                    "books",
                    lambda: self._read_books(user),
                    actions,
                    errors,
                )
                user = await self.user_service.get_user_by_pk(user_pk) or user
                await self._run_step(
                    "active_slots",
                    lambda: self._fill_active_slots(user),
                    actions,
                    errors,
                )
                user = await self.user_service.get_user_by_pk(user_pk) or user
                await self._run_step(
                    "equipment",
                    lambda: self._auto_equip(user),
                    actions,
                    errors,
                )
                user = await self.user_service.get_user_by_pk(user_pk) or user
                await self._run_step(
                    "nefia",
                    lambda: self._run_nefia_step(user, identity, now_ts=now_ts),
                    actions,
                    errors,
                )
                user = await self.user_service.get_user_by_pk(user_pk) or user
                last_operation_day_key = str(
                    state.cursor.get("last_operation_day_key", "")
                )
                last_operation_attempt_ts = int(
                    state.cursor.get("last_operation_attempt_ts", 0) or 0
                )
                operations_due = (
                    last_operation_day_key != day_key
                    or now_ts - last_operation_attempt_ts >= 300
                )
                if operations_due:
                    await self._run_step(
                        "operations",
                        lambda: self._claim_operations(user),
                        actions,
                        errors,
                    )
                if "operations" in actions:
                    user = await self.user_service.get_user_by_pk(user_pk) or user
                    await self._run_step(
                        "equipment_after_rewards",
                        lambda: self._auto_equip(user),
                        actions,
                        errors,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(f"pass:{exc}")
                logger.exception("LevelUpPvp auto pilot pass failed for %s", user_pk)

            cursor = dict(state.cursor)
            cursor["last_day_key"] = day_key
            if operations_due:
                cursor["last_operation_day_key"] = day_key
                cursor["last_operation_attempt_ts"] = now_ts
            if actions:
                cursor["last_action"] = actions[-1]
            await self._schedule_state(
                user_pk,
                now_ts=now_ts,
                cursor=cursor,
                errors=tuple(errors),
                delay=(self.ERROR_BACKOFF_SECONDS if errors else self.TICK_INTERVAL_SECONDS),
            )
            return AutoPilotPassResult(
                user_pk,
                True,
                tuple(actions),
                tuple(errors),
            )

    async def _run_step(self, name, callback, actions, errors) -> None:
        try:
            result = await callback()
            if result:
                actions.append(name)
        except asyncio.CancelledError:
            raise
        except ValueError:
            # Resource exhaustion, already-read books and already-learned skills
            # are normal convergence outcomes for a repeated silent pass.
            return
        except Exception as exc:
            errors.append(f"{name}:{exc}")
            logger.exception("LevelUpPvp auto pilot %s step failed", name)

    async def _allocate_attributes(self, user, identity) -> bool:
        points = int(getattr(user, "stat_points", 0))
        if points <= 0:
            return False
        attributes = self.attribute_service.attributes_for_user(user)
        dominant = self.auto_equip_service.dominant_attribute(attributes)
        await self.stat_service.allocate(identity, dominant, points)
        return True

    async def _learn_or_train(self, user) -> bool:
        skills, _ = await self.skill_service.get_skills(user)
        if int(user.skill_points) <= 0:
            return False
        library = await self.spell_service.get_book_library(user)
        priorities = self._skill_priorities(library)
        target = self._next_skill_to_learn(priorities, skills)
        if target:
            await self.skill_service.learn_many(user, (target,))
            return True

        training_id = await self._next_skill_to_train(user, skills)
        if not training_id:
            return False
        await self.skill_service.train_many(
            user,
            ((training_id, min(5, int(user.skill_points))),),
        )
        return True

    def _skill_priorities(self, library) -> tuple[str, ...]:
        values: list[str] = []
        for entry in sorted(
            getattr(library, "entries", ()),
            key=lambda item: (
                int(item.reading_difficulty),
                str(item.spell_id),
            ),
        ):
            if int(entry.quantity) > 0 and entry.spell_id:
                values.append(str(entry.school_id))
        values.extend(("reading", "concealment"))
        values.extend(self._TRAINING_PRIORITY)
        result = []
        for value in values:
            if value in SKILL_DEFINITIONS and value not in result:
                result.append(value)
        return tuple(result)

    def _next_skill_to_learn(self, priorities, skills) -> str | None:
        for skill_id in priorities:
            candidate = self._missing_dependency_leaf(skill_id, skills, set())
            if candidate and candidate not in skills:
                return candidate
        return None

    def _missing_dependency_leaf(self, skill_id: str, skills, visiting: set[str]) -> str | None:
        if skill_id in skills:
            return None
        definition = SKILL_DEFINITIONS.get(skill_id)
        if definition is None or skill_id in visiting:
            return None
        visiting.add(skill_id)
        for required_id, required_level in definition.prerequisites:
            required = skills.get(required_id)
            if required is None:
                leaf = self._missing_dependency_leaf(required_id, skills, visiting)
                return leaf or required_id
            if int(required.level) < int(required_level):
                return None
        return skill_id

    async def _next_skill_to_train(self, user, skills) -> str | None:
        attributes = self.attribute_service.attributes_for_user(user)
        priority = list(await self._equipped_weapon_skill_ids(user))
        priority.extend(
            skill_id for skill_id in self._TRAINING_PRIORITY
            if skill_id not in priority
        )
        priority.extend(
            skill_id for skill_id in skills
            if skill_id not in priority
        )
        for skill_id in priority:
            skill = skills.get(skill_id)
            definition = SKILL_DEFINITIONS.get(skill_id)
            if skill is None or definition is None or int(skill.potential) >= 200:
                continue
            level_cap = skill_level_cap(
                attributes,
                definition.governing_attributes,
                skill_id,
            )
            if int(skill.level) < min(100, int(level_cap)):
                return skill_id
        return None

    async def _read_books(self, user) -> bool:
        library = await self.spell_service.get_book_library(user)
        candidates = [
            entry
            for entry in library.entries
            if int(entry.quantity) > 0
            and not bool(entry.studied_today)
            and int(entry.school_level) >= 1
        ]
        candidates.sort(
            key=lambda entry: (
                int(entry.reading_difficulty),
                str(entry.spell_id),
                int(entry.oldest_book_id),
            )
        )
        if not candidates:
            return False
        for entry in candidates[: self.MAX_BOOKS_PER_PASS]:
            await self.spell_service.read_book(
                user,
                entry.oldest_book_id,
            )
        return True

    async def _fill_active_slots(self, user) -> bool:
        skills, slots = await self.skill_service.get_skills(user)
        spells = await self.spell_service.get_spells(user.id)
        existing = {value for value in slots if value}
        empty_slots = [index + 1 for index, value in enumerate(slots) if not value]
        if not empty_slots:
            return False
        candidates = []
        for ability_id, definition in ACTIVE_ABILITY_DEFINITIONS.items():
            if ability_id in existing or not ability_is_unlocked(definition, skills, spells):
                continue
            tags = set(definition.ai_tags)
            rank = min(
                (self._ACTIVE_TAG_ORDER.get(tag, 20) for tag in tags),
                default=20,
            )
            candidates.append((rank, str(ability_id), ability_id))
        candidates.sort()
        assignments = [
            (slot, ability_id)
            for slot, (_, _, ability_id) in zip(empty_slots, candidates)
        ]
        if not assignments:
            return False
        await self.skill_service.set_active_slots(user, assignments)
        return True

    async def _auto_equip(self, user) -> bool:
        results = await self.auto_equip_service.auto_equip_user(
            user,
            respect_locked=True,
        )
        return bool(results)

    async def _run_nefia_step(
        self,
        user,
        identity,
        *,
        now_ts: int | None = None,
    ) -> bool:
        dungeons = tuple(self.dungeon_service.list_dungeons())
        if not dungeons:
            return False
        day_key = daily_growth_day_window(now_ts)[0]
        dungeon = max(
            dungeons,
            key=lambda item: (
                stable_operation_seed(
                    "nefia-theme-v12",
                    identity.group_id or "global",
                    day_key,
                    item.dungeon_id,
                ),
                str(item.dungeon_id),
            ),
        )
        result = None
        terminal_result = None
        for candidate in dungeons:
            try:
                candidate_result = await self.dungeon_service.view_nefia(
                    identity,
                    dungeon_id=candidate.dungeon_id,
                )
            except KeyError:
                continue
            if candidate_result.view.terminal:
                terminal_result = candidate_result
                continue
            result = candidate_result
            dungeon = candidate
            break
        if result is None:
            if terminal_result is not None:
                return False
            await self.dungeon_service.start_nefia(
                identity,
                dungeon.dungeon_id,
                difficulty=1,
                strategy="稳扎稳打",
            )
            return True

        view = result.view
        if view.phase == "route_choice":
            route = self._choose_route(view, user.level)
            await self.dungeon_service.choose_nefia_route(
                identity,
                view.adventure_id,
                route.option_id,
            )
            return True
        if view.phase == "risk_choice":
            route = next(
                (
                    item for item in view.routes
                    if item.option_id == view.selected_route_id
                ),
                None,
            )
            if route is None:
                raise ValueError("奈菲亚存档缺少已选路线")
            risk = self._choose_risk(route, view.hp_ratio)
            await self.dungeon_service.choose_nefia_risk(
                identity,
                view.adventure_id,
                risk.risk_id,
            )
            await self._record_nefia_risk_progress(
                user,
                view.adventure_id,
                view.floor_number,
                risk.risk_id,
            )
            return True
        if view.phase == "combat_ready":
            floor_number = view.floor_number
            route = next(
                (
                    item for item in view.routes
                    if item.option_id == view.selected_route_id
                ),
                None,
            )
            if route is None:
                raise ValueError("奈菲亚存档缺少已选路线")
            risk = next(
                (
                    item for item in route.risk_choices
                    if item.risk_id == view.selected_risk_id
                ),
                None,
            )
            if risk is None:
                raise ValueError("奈菲亚存档缺少已选风险")
            result = await self.dungeon_service.fight_nefia(
                identity,
                view.adventure_id,
                strategy="稳扎稳打",
            )
            await self._record_nefia_fight_progress(
                user,
                result,
                route,
                risk,
                floor_number,
            )
            return True
        return False

    def _choose_route(self, view, user_level: int):
        routes = [
            route for route in view.routes
            if getattr(route, "risk_choices", ())
        ]
        if not routes:
            raise ValueError("奈菲亚当前没有可处理的路线")
        hp_ratio = float(view.hp_ratio)
        if hp_ratio < 0.55:
            recovery = [
                route for route in routes
                if route.node_kind in {"camp", "remains"}
            ]
            if recovery:
                return min(recovery, key=lambda route: route.option_id)

        def key(route):
            low_risk = self._choose_risk(route, hp_ratio)
            threat = int(low_risk.monster_level)
            unsafe = int(hp_ratio < 0.70 and threat > int(user_level) + 4)
            rank = {
                "boss": 0,
                "elite": 1,
                "camp": 2,
                "remains": 2,
                "hidden_room": 2,
                "treasure": 2,
                "gathering": 2,
                "normal": 3,
            }.get(route.node_kind, 4)
            return (
                unsafe,
                rank,
                threat,
                0 if route.discovery_accessible else 1,
                str(route.option_id),
            )

        return min(routes, key=key)

    @staticmethod
    def _choose_risk(route, hp_ratio: float):
        risks = list(route.risk_choices)
        if not risks:
            raise ValueError("奈菲亚当前路线没有风险选项")
        affordable = [
            risk for risk in risks
            if float(hp_ratio) >= 0.50 or float(risk.entry_hp_cost_ratio) <= 0.0
        ]
        if not affordable:
            affordable = risks
        if route.node_kind in {"boss", "elite"} and float(hp_ratio) >= 0.70:
            free = [
                risk for risk in affordable
                if float(risk.entry_hp_cost_ratio) <= 0.0
                and float(risk.entry_mp_cost_ratio) <= 0.0
            ]
            if free:
                return max(
                    free,
                    key=lambda risk: (
                        float(risk.reward_multiplier),
                        -int(risk.monster_level),
                        str(risk.risk_id),
                    ),
                )
        return min(
            affordable,
            key=lambda risk: (
                int(risk.monster_level),
                float(risk.entry_hp_cost_ratio),
                float(risk.entry_mp_cost_ratio),
                -float(risk.reward_multiplier),
                str(risk.risk_id),
            ),
        )

    async def _record_nefia_risk_progress(
        self,
        user,
        adventure_id: str,
        floor_number: int,
        risk_id: str,
    ) -> None:
        if self.operation_service is None:
            return
        common = {
            "user_pk": user.id,
            "group_id": user.group_id or "global",
        }
        for event_type, event_key in (
            (
                "risk_choice",
                f"nefia:{adventure_id}:floor:{floor_number}:risk",
            ),
            ("risk_choice_unique", f"risk:{risk_id}"),
        ):
            try:
                await self.operation_service.record_event(
                    **common,
                    event_type=event_type,
                    event_key=event_key,
                )
            except Exception:
                logger.exception("托管奈菲亚风险事件记录失败：%s", event_type)

    async def _record_nefia_fight_progress(
        self,
        user,
        result,
        route,
        risk,
        floor_number: int,
    ) -> None:
        if self.operation_service is None:
            return
        simulation = result.simulation
        prefix = (
            f"nefia:{result.view.adventure_id}:floor:{max(1, int(floor_number))}"
        )
        common = {
            "user_pk": user.id,
            "group_id": user.group_id or "global",
        }
        event_suffix = "fight" if simulation is not None else "event"
        events = [("nefia_node", f"{prefix}:{event_suffix}", 1)]
        if simulation is None:
            events.append(("nefia_discovery", f"{prefix}:discovery", 1))
        else:
            if route.node_kind in {"elite", "boss"}:
                events.append(("boss_attempt", f"{prefix}:boss-attempt", 1))
            if route.node_kind == "boss" and simulation.winner_pk == user.id:
                events.append(("nefia_boss_clear", f"{prefix}:boss-clear", 1))
            active_uses = sum(
                event.actor_pk == user.id
                and event.kind in {"skill_use", "spell_cast_start"}
                for event in simulation.events
            )
            if active_uses:
                events.append(("active_skill", f"{prefix}:active-skill", active_uses))
            for event_type, event_kind in (
                ("spell_cast", "spell_cast"),
                ("guard_action", "guard"),
                ("fortune_trigger", "fortune_swing"),
            ):
                count = sum(
                    event.actor_pk == user.id and event.kind == event_kind
                    for event in simulation.events
                )
                if count:
                    events.append((event_type, f"{prefix}:{event_type}", count))
            tactic_events = [
                event for event in simulation.events
                if event.actor_pk == user.id and event.kind == "strategy_trigger"
            ]
            if any(event.skill_id == "endgame" for event in tactic_events):
                events.append(("combat_endgame", f"{prefix}:endgame", 1))
            for family in {
                event.status_id for event in tactic_events if event.status_id
            }:
                events.append(("stance_unique", f"stance:{family}", 1))
            events.append(
                (
                    "environment_unique",
                    f"environment:{simulation.environment_id}",
                    1,
                )
            )
            if simulation.winner_pk == user.id:
                events.append(("battle_win", f"{prefix}:win", 1))
        for event_type, event_key, amount in events:
            try:
                await self.operation_service.record_event(
                    **common,
                    event_type=event_type,
                    event_key=event_key,
                    amount=amount,
                )
            except Exception:
                logger.exception("托管奈菲亚战斗事件记录失败：%s", event_type)

    async def _claim_operations(self, user) -> bool:
        if self.operation_service is None or self.operation_settlement_service is None:
            return False
        group_id = user.group_id or "global"
        applied_any = False
        daily = await self.operation_service.claim_daily_reward(
            user_pk=user.id,
            group_id=group_id,
        )
        if daily.eligible and daily.reward_intent is not None:
            settled = await self.operation_settlement_service.settle(
                user_pk=user.id,
                intent=daily.reward_intent,
            )
            applied_any = applied_any or bool(settled.applied)
            try:
                await self.operation_service.record_event(
                    user_pk=user.id,
                    group_id=group_id,
                    event_type="daily_reward",
                    event_key=f"settled:{daily.reward_intent.reward_key}",
                )
            except Exception:
                logger.exception("托管每日运营奖励进度记录失败")

        weekly = await self.operation_service.claim_weekly_reward(
            user_pk=user.id,
            group_id=group_id,
        )
        if weekly.eligible and weekly.reward_intent is not None:
            settled = await self.operation_settlement_service.settle(
                user_pk=user.id,
                intent=weekly.reward_intent,
            )
            applied_any = applied_any or bool(settled.applied)
        return applied_any

    async def _equipped_weapon_skill_ids(self, user) -> tuple[str, ...]:
        slots, items = await self.equipment_service.get_loadout(user.id)
        equipped_ids = set(slots.values())
        values = []
        for item in items:
            if item.id not in equipped_ids or item.weapon_type not in self._WEAPON_SKILL_IDS:
                continue
            if item.weapon_type not in values:
                values.append(item.weapon_type)
        return tuple(values)

    async def _run(self) -> None:
        await asyncio.sleep(self.STARTUP_DELAY_SECONDS)
        while True:
            try:
                now_ts = int(time.time())
                states = await self.list_enabled_due(now_ts)
                for state in states:
                    await self.run_pass(state.user_pk, now_ts=now_ts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LevelUpPvp auto pilot worker iteration failed")
                await asyncio.sleep(self.ERROR_BACKOFF_SECONDS)
                continue
            await asyncio.sleep(self.TICK_INTERVAL_SECONDS)

    async def _schedule_state(
        self,
        user_pk: int,
        *,
        now_ts: int | None,
        cursor: dict[str, Any],
        errors: tuple[str, ...],
        delay: int,
    ) -> None:
        timestamp = int(time.time() if now_ts is None else now_ts)
        state = await self.get_state(user_pk)
        if state is None:
            return
        error_count = state.consecutive_errors + 1 if errors else 0
        last_error = errors[-1][:500] if errors else ""
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE auto_pilot_state
                    SET last_tick_ts = ?, next_tick_ts = ?, cursor_json = ?,
                        consecutive_errors = ?, last_error = ?, updated_at_ts = ?
                    WHERE user_pk = ? AND enabled = 1
                    """,
                    (
                        timestamp,
                        timestamp + max(1, int(delay)),
                        json.dumps(cursor, ensure_ascii=False, sort_keys=True),
                        error_count,
                        last_error,
                        timestamp,
                        int(user_pk),
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def _state_from_row(row) -> AutoPilotState:
        try:
            cursor = json.loads(str(row["cursor_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = {}
        if not isinstance(cursor, dict):
            cursor = {}
        return AutoPilotState(
            user_pk=int(row["user_pk"]),
            enabled=bool(row["enabled"]),
            origin_umo=str(row["origin_umo"] or ""),
            origin_group_id=str(row["origin_group_id"] or ""),
            started_at_ts=int(row["started_at_ts"]),
            last_tick_ts=int(row["last_tick_ts"]),
            next_tick_ts=int(row["next_tick_ts"]),
            cursor=cursor,
            consecutive_errors=int(row["consecutive_errors"]),
            last_error=str(row["last_error"] or ""),
        )


__all__ = ["AutoPilotPassResult", "AutoPilotService", "AutoPilotState"]
