from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

try:
    from ..models.combat import FighterContinuationState, FighterSnapshot
    from .combat_engine import SideviewCombatEngine
    from .db import connect_db
except ImportError:
    from models.combat import FighterContinuationState, FighterSnapshot
    from services.combat_engine import SideviewCombatEngine
    from services.db import connect_db


RECOVERY_TURN_SECONDS = 30


@dataclass(frozen=True)
class RecoveryRates:
    hp: float
    mp: float
    stamina: float


@dataclass(frozen=True)
class CombatStateView:
    state: FighterContinuationState
    current_hp: int
    max_hp: int
    current_mp: int
    max_mp: int
    current_stamina: int
    max_stamina: int
    rates: RecoveryRates
    next_recovery_seconds: int


class CombatStateService:
    """Persistence and deterministic real-time advancement of combat state."""

    def __init__(
        self,
        db_path: str,
        combat_engine: SideviewCombatEngine | None = None,
        *,
        clock=time.time,
    ):
        self.db_path = db_path
        self.combat_engine = combat_engine or SideviewCombatEngine()
        self.clock = clock

    @staticmethod
    def pristine(now_ts: int = 0) -> FighterContinuationState:
        return FighterContinuationState(updated_at_ts=now_ts)

    async def preview(
        self,
        snapshot: FighterSnapshot,
        now_ts: int | None = None,
    ) -> CombatStateView:
        timestamp = int(self.clock() if now_ts is None else now_ts)
        async with await connect_db(self.db_path) as db:
            state = await self.load_in_db(
                db, snapshot, timestamp, consume_defeat=False
            )
        return self.view(snapshot, state, timestamp)

    async def load_in_db(
        self,
        db,
        snapshot: FighterSnapshot,
        now_ts: int,
        *,
        consume_defeat: bool = True,
    ) -> FighterContinuationState:
        cursor = await db.execute(
            "SELECT * FROM combat_states WHERE user_pk = ?",
            (snapshot.user_pk,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return self.pristine(now_ts)

        state = self._state_from_row(row)
        if state.defeated:
            return self.pristine(now_ts)
        return self.advance(snapshot, state, now_ts)

    async def save_in_db(
        self,
        db,
        user_pk: int,
        state: FighterContinuationState,
        now_ts: int,
    ) -> FighterContinuationState:
        stored = replace(
            self.pristine(now_ts) if state.defeated else state,
            updated_at_ts=int(now_ts),
            version=max(1, int(state.version) + 1),
        )
        await db.execute(
            """
            INSERT INTO combat_states (
                user_pk, hp_ratio, mana_ratio, stamina_ratio,
                hp_regen_buffer, mp_regen_buffer, sp_regen_buffer,
                recovery_turn_phase,
                statuses_json, skill_cooldowns_json,
                attack_cooldown, recovery_ticks, hitstun_ticks,
                counter_cooldown, hard_control_immunity_ticks,
                stance_id, frozen_mana_ratio,
                frozen_mana_capacity_ratio, lethal_survival_used,
                defeated, updated_at_ts, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_pk) DO UPDATE SET
                hp_ratio = excluded.hp_ratio,
                mana_ratio = excluded.mana_ratio,
                stamina_ratio = excluded.stamina_ratio,
                hp_regen_buffer = excluded.hp_regen_buffer,
                mp_regen_buffer = excluded.mp_regen_buffer,
                sp_regen_buffer = excluded.sp_regen_buffer,
                recovery_turn_phase = excluded.recovery_turn_phase,
                statuses_json = excluded.statuses_json,
                skill_cooldowns_json = excluded.skill_cooldowns_json,
                attack_cooldown = excluded.attack_cooldown,
                recovery_ticks = excluded.recovery_ticks,
                hitstun_ticks = excluded.hitstun_ticks,
                counter_cooldown = excluded.counter_cooldown,
                hard_control_immunity_ticks =
                    excluded.hard_control_immunity_ticks,
                stance_id = excluded.stance_id,
                frozen_mana_ratio = excluded.frozen_mana_ratio,
                frozen_mana_capacity_ratio = excluded.frozen_mana_capacity_ratio,
                lethal_survival_used = excluded.lethal_survival_used,
                defeated = excluded.defeated,
                updated_at_ts = excluded.updated_at_ts,
                version = combat_states.version + 1
            """,
            (
                user_pk,
                stored.hp_ratio,
                stored.mana_ratio,
                stored.stamina_ratio,
                stored.hp_regen_buffer,
                stored.mp_regen_buffer,
                stored.sp_regen_buffer,
                stored.recovery_turn_phase,
                json.dumps(stored.statuses, ensure_ascii=False),
                json.dumps(stored.skill_cooldowns, ensure_ascii=False),
                stored.attack_cooldown,
                stored.recovery_ticks,
                stored.hitstun_ticks,
                stored.counter_cooldown,
                stored.hard_control_immunity_ticks,
                stored.stance_id,
                stored.frozen_mana_ratio,
                stored.frozen_mana_capacity_ratio,
                1 if stored.lethal_survival_used else 0,
                1 if stored.defeated else 0,
                stored.updated_at_ts,
                stored.version,
            ),
        )
        return stored

    def advance(
        self,
        snapshot: FighterSnapshot,
        state: FighterContinuationState,
        now_ts: int,
    ) -> FighterContinuationState:
        elapsed = max(0, int(now_ts) - int(state.updated_at_ts))
        turns = elapsed // RECOVERY_TURN_SECONDS
        if turns <= 0:
            return state

        fighter = self.combat_engine._fighter_from_initial(snapshot, 0, state)
        dynamic_turns = min(turns, self._dynamic_turn_count(fighter))
        for turn in range(1, dynamic_turns + 1):
            phase = (fighter.recovery_turn_phase + 1) % 5
            fighter.recovery_turn_phase = phase
            self._advance_periodic_statuses(fighter, phase)
            if not fighter.alive:
                return replace(
                    self.pristine(now_ts),
                    version=state.version,
                )
            self._recover_one_turn(fighter)
            self._decrement_timers(fighter)
        remaining_turns = turns - dynamic_turns
        if remaining_turns > 0:
            self._recover_many(fighter, remaining_turns)
            fighter.recovery_turn_phase = (
                fighter.recovery_turn_phase + remaining_turns
            ) % 5

        result = self.combat_engine._continuation_state(fighter)
        return replace(
            result,
            updated_at_ts=(
                state.updated_at_ts + turns * RECOVERY_TURN_SECONDS
            ),
            version=state.version,
        )

    def view(
        self,
        snapshot: FighterSnapshot,
        state: FighterContinuationState,
        now_ts: int,
    ) -> CombatStateView:
        fighter = self.combat_engine._fighter_from_initial(snapshot, 0, state)
        rates = self.recovery_rates(fighter)
        remainder = max(0, int(now_ts) - int(state.updated_at_ts)) % 30
        return CombatStateView(
            state=state,
            current_hp=fighter.current_hp,
            max_hp=fighter.max_hp,
            current_mp=fighter.mana,
            max_mp=fighter.max_mp,
            current_stamina=fighter.stamina,
            max_stamina=fighter.max_sp,
            rates=rates,
            next_recovery_seconds=30 if remainder == 0 else 30 - remainder,
        )

    def recovery_rates(self, fighter) -> RecoveryRates:
        healing_level = fighter.skill_level("healing")
        meditation_level = fighter.skill_level("meditation")
        healing_die = max(1, healing_level // 3)
        meditation_die = max(1, meditation_level // 3)
        constitution = fighter.primary("constitution")
        willpower = fighter.primary("willpower")
        healing_power = (
            fighter.current_derived.healing_power
            if fighter.current_derived else 1.0
        )
        healing_efficiency = max(
            0.0,
            healing_power
            * (
                1
                + self.combat_engine.ability_runtime.modifier(
                    fighter, "healing"
                )
            ),
        )
        hp = (
            (((healing_die + 1) / 2 + 1) / 6)
            * (1 + min(2.0, constitution / 100))
            * healing_efficiency
        )
        mp_efficiency = (
            1.5
            if self.combat_engine.ability_runtime.has(fighter, "insight")
            else 1.0
        )
        mp = (
            (((meditation_die + 1) / 2 + 1) / 3)
            * (1 + min(2.0, willpower / 100))
            * mp_efficiency
        )
        equipment_regen = (
            fighter.snapshot.equipment.stamina_regen
            if fighter.snapshot.equipment else 8
        )
        if fighter.runtime_armor_style:
            equipment_regen = {"light": 10, "medium": 8, "heavy": 6}[
                fighter.runtime_armor_style
            ]
            if fighter.runtime_overloaded:
                equipment_regen = max(1, equipment_regen // 2)
        stamina = max(0.5, equipment_regen / 4) * (
            1 + min(2.0, (constitution + willpower) / 200)
        )
        return RecoveryRates(hp, mp, stamina)

    def _recover_one_turn(self, fighter) -> None:
        rates = self.recovery_rates(fighter)
        runtime = self.combat_engine.ability_runtime
        if (
            fighter.current_hp < fighter.max_hp
            and not runtime.has(fighter, "healing_block")
        ):
            fighter.hp_regen_buffer += rates.hp
            amount = min(
                fighter.max_hp - fighter.current_hp,
                int(fighter.hp_regen_buffer),
            )
            fighter.current_hp += amount
            fighter.hp_regen_buffer -= amount
        elif fighter.current_hp >= fighter.max_hp:
            fighter.hp_regen_buffer = 0.0

        available_mp = max(0, fighter.max_mp - fighter.frozen_mana_capacity)
        if (
            fighter.mana < available_mp
            and not runtime.has(fighter, "mp_regen_frozen")
        ):
            fighter.mp_regen_buffer += rates.mp
            amount = min(
                available_mp - fighter.mana,
                int(fighter.mp_regen_buffer),
            )
            fighter.mana += amount
            fighter.mp_regen_buffer -= amount
        elif fighter.mana >= available_mp:
            fighter.mp_regen_buffer = 0.0

        if fighter.stamina < fighter.max_sp:
            fighter.sp_regen_buffer += rates.stamina
            amount = min(
                fighter.max_sp - fighter.stamina,
                int(fighter.sp_regen_buffer),
            )
            fighter.stamina += amount
            fighter.sp_regen_buffer -= amount
        else:
            fighter.sp_regen_buffer = 0.0

    def _recover_many(self, fighter, turns: int) -> None:
        if turns <= 0:
            return
        rates = self.recovery_rates(fighter)
        if fighter.current_hp < fighter.max_hp:
            total = fighter.hp_regen_buffer + rates.hp * turns
            amount = min(fighter.max_hp - fighter.current_hp, int(total))
            fighter.current_hp += amount
            fighter.hp_regen_buffer = (
                0.0 if fighter.current_hp >= fighter.max_hp else total - amount
            )
        available_mp = max(0, fighter.max_mp - fighter.frozen_mana_capacity)
        if fighter.mana < available_mp:
            total = fighter.mp_regen_buffer + rates.mp * turns
            amount = min(available_mp - fighter.mana, int(total))
            fighter.mana += amount
            fighter.mp_regen_buffer = (
                0.0 if fighter.mana >= available_mp else total - amount
            )
        if fighter.stamina < fighter.max_sp:
            total = fighter.sp_regen_buffer + rates.stamina * turns
            amount = min(fighter.max_sp - fighter.stamina, int(total))
            fighter.stamina += amount
            fighter.sp_regen_buffer = (
                0.0 if fighter.stamina >= fighter.max_sp else total - amount
            )

    @staticmethod
    def _dynamic_turn_count(fighter) -> int:
        timers = [
            fighter.attack_cooldown,
            fighter.recovery_ticks,
            fighter.hitstun_ticks,
            fighter.counter_cooldown,
            fighter.hard_control_immunity_until,
            *fighter.skill_cooldowns.values(),
            *(status.remaining_ticks for status in fighter.statuses.values()),
        ]
        return max((max(0, int(value)) for value in timers), default=0)

    def _advance_periodic_statuses(self, fighter, phase: int) -> None:
        runtime = self.combat_engine.ability_runtime
        for status in list(fighter.statuses.values()):
            if (
                status.status_id in {"burn", "poison", "bleed"}
                and phase == 0
            ):
                damage = max(1, round(status.magnitude * status.stacks))
                fighter.current_hp = max(0, fighter.current_hp - damage)
            if (
                status.status_id in {"regeneration", "despair_regen"}
                and fighter.current_hp < fighter.max_hp
                and not runtime.has(fighter, "healing_block")
            ):
                multiplier = max(0.0, 1 + runtime.modifier(fighter, "healing"))
                fighter.current_hp = min(
                    fighter.max_hp,
                    fighter.current_hp
                    + max(0, round(status.magnitude * multiplier)),
                )

    def _decrement_timers(self, fighter) -> None:
        fighter.skill_cooldowns = {
            key: value - 1
            for key, value in fighter.skill_cooldowns.items()
            if value > 1
        }
        fighter.attack_cooldown = max(0, fighter.attack_cooldown - 1)
        fighter.recovery_ticks = max(0, fighter.recovery_ticks - 1)
        fighter.hitstun_ticks = max(0, fighter.hitstun_ticks - 1)
        fighter.counter_cooldown = max(0, fighter.counter_cooldown - 1)
        fighter.hard_control_immunity_until = max(
            0, fighter.hard_control_immunity_until - 1
        )
        expired = []
        for status in fighter.statuses.values():
            status.remaining_ticks -= 1
            if status.remaining_ticks <= 0:
                expired.append(status.status_id)
        for status_id in expired:
            fighter.statuses.pop(status_id, None)
            if fighter.stance_id == status_id:
                fighter.stance_id = None
                fighter.mana = min(
                    fighter.max_mp, fighter.mana + fighter.frozen_mana
                )
                fighter.frozen_mana = 0
                fighter.frozen_mana_capacity = 0
        if expired:
            self.combat_engine.ability_runtime.stat_resolver.refresh(fighter)

    @staticmethod
    def _state_from_row(row) -> FighterContinuationState:
        try:
            statuses = tuple(json.loads(row["statuses_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            statuses = ()
        try:
            cooldowns = dict(json.loads(row["skill_cooldowns_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            cooldowns = {}
        return FighterContinuationState(
            hp_ratio=float(row["hp_ratio"]),
            mana_ratio=float(row["mana_ratio"]),
            stamina_ratio=float(row["stamina_ratio"]),
            hp_regen_buffer=float(row["hp_regen_buffer"]),
            mp_regen_buffer=float(row["mp_regen_buffer"]),
            sp_regen_buffer=float(row["sp_regen_buffer"]),
            recovery_turn_phase=int(row["recovery_turn_phase"]) % 5,
            statuses=statuses,
            skill_cooldowns={
                str(key): max(0, int(value))
                for key, value in cooldowns.items()
            },
            attack_cooldown=int(row["attack_cooldown"]),
            recovery_ticks=int(row["recovery_ticks"]),
            hitstun_ticks=int(row["hitstun_ticks"]),
            counter_cooldown=int(row["counter_cooldown"]),
            hard_control_immunity_ticks=max(
                0, int(row["hard_control_immunity_ticks"])
            ),
            stance_id=row["stance_id"],
            frozen_mana_ratio=float(row["frozen_mana_ratio"]),
            frozen_mana_capacity_ratio=float(
                row["frozen_mana_capacity_ratio"]
            ),
            lethal_survival_used=bool(row["lethal_survival_used"]),
            defeated=bool(row["defeated"]),
            updated_at_ts=int(row["updated_at_ts"]),
            version=int(row["version"]),
        )
