import math
import random

try:
    from ..models.ability import CombatStatus
    from ..models.combat import (
        AIProfile,
        ActionIntent,
        BattleEvent,
        BattleState,
        FighterContinuationState,
        FighterSnapshot,
        FighterState,
        SimulationResult,
    )
    from .balance_rules import (
        hit_chance,
        physical_damage_amount,
        resistance_multiplier,
    )
    from .ability_runtime import AbilityRuntime
    from .combat_ai import choose_action
    from .equipment_proc_service import EquipmentProcResolver
except ImportError:
    from models.ability import CombatStatus
    from models.combat import (
        AIProfile,
        ActionIntent,
        BattleEvent,
        BattleState,
        FighterContinuationState,
        FighterSnapshot,
        FighterState,
        SimulationResult,
    )
    from services.balance_rules import (
        hit_chance,
        physical_damage_amount,
        resistance_multiplier,
    )
    from services.ability_runtime import AbilityRuntime
    from services.combat_ai import choose_action
    from services.equipment_proc_service import EquipmentProcResolver


class SideviewCombatEngine:
    ENGINE_VERSION = "sideview-v10"
    FIELD_MIN = 0
    FIELD_MAX = 1000
    ATTACKER_START = 200
    DEFENDER_START = 800
    MIN_DISTANCE = 30
    ATTACK_RANGE = 100
    ATTACK_LUNGE_DISTANCE = 30
    MAX_TICKS = 120
    GUARD_DAMAGE_MULTIPLIER = 0.55
    ATTACK_WINDUP_TICKS = 1
    ATTACK_RECOVERY_TICKS = 2
    HITSTUN_TICKS = 1
    CRITICAL_HITSTUN_TICKS = 2
    KNOCKBACK_BASE = 20
    KNOCKBACK_MAX = 60

    def __init__(self):
        self.ability_runtime = AbilityRuntime()
        self.equipment_proc_resolver = EquipmentProcResolver(
            self.ability_runtime
        )

    def simulate(
        self,
        attacker: FighterSnapshot,
        defender: FighterSnapshot,
        attacker_profile: AIProfile,
        defender_profile: AIProfile,
        random_seed: int,
        attacker_initial_state: FighterContinuationState | None = None,
        defender_initial_state: FighterContinuationState | None = None,
    ) -> SimulationResult:
        rng = random.Random(random_seed)
        state = BattleState(
            tick=0,
            attacker=self._fighter_from_initial(
                attacker, self.ATTACKER_START, attacker_initial_state
            ),
            defender=self._fighter_from_initial(
                defender, self.DEFENDER_START, defender_initial_state
            ),
            events=[],
            random_seed=random_seed,
        )
        for tick in range(1, self.MAX_TICKS + 1):
            state.tick = tick
            pre_status_attacker_hp = state.attacker.current_hp
            pre_status_defender_hp = state.defender.current_hp
            self.ability_runtime.tick(state, rng)
            if not state.attacker.alive or not state.defender.alive:
                if not state.attacker.alive and not state.defender.alive:
                    winner = self._resolve_double_ko(
                        state, pre_status_attacker_hp,
                        pre_status_defender_hp, rng,
                    )
                    state.finish_reason = "status_double_ko_tiebreak"
                else:
                    winner = state.attacker if state.attacker.alive else state.defender
                    state.finish_reason = "status_knockout"
                loser = state.defender if winner is state.attacker else state.attacker
                state.events.append(BattleEvent(
                    tick, "knockout", winner.snapshot.user_pk,
                    loser.snapshot.user_pk,
                ))
                return self._result(state, winner, loser)
            attacker_phase = self._prepare_tick(state, state.attacker)
            defender_phase = self._prepare_tick(state, state.defender)
            attacker_intent = self._intent_for_phase(
                state.attacker,
                state.defender,
                attacker_profile,
                attacker_phase,
                rng,
            )
            defender_intent = self._intent_for_phase(
                state.defender,
                state.attacker,
                defender_profile,
                defender_phase,
                rng,
            )
            if attacker_intent.action == "rest":
                state.events.append(BattleEvent(tick, "rest", state.attacker.snapshot.user_pk, stamina=state.attacker.stamina))
            if defender_intent.action == "rest":
                state.events.append(BattleEvent(tick, "rest", state.defender.snapshot.user_pk, stamina=state.defender.stamina))
            self._begin_attack(state, state.attacker, attacker_intent.action, attacker_intent.skill_id)
            self._begin_attack(state, state.defender, defender_intent.action, defender_intent.skill_id)
            self._resolve_movement(state, attacker_intent.action, defender_intent.action)
            self._resolve_attack_lunges(
                state,
                attacker_intent.action,
                defender_intent.action,
            )
            self._resolve_guards(state, attacker_intent.action, defender_intent.action)

            pre_attacker_position = state.attacker.position
            pre_defender_position = state.defender.position
            attacker_damage = self._attack_damage(
                state, state.attacker, state.defender, attacker_intent.action, rng
            )
            defender_damage = self._attack_damage(
                state, state.defender, state.attacker, defender_intent.action, rng
            )
            attacker_followup = self._followup_damage(state, state.attacker, state.defender, attacker_damage, rng)
            defender_followup = self._followup_damage(state, state.defender, state.attacker, defender_damage, rng)
            pre_attacker_hp = state.attacker.current_hp
            pre_defender_hp = state.defender.current_hp
            self._apply_damage(state, state.attacker, state.defender, attacker_damage)
            self._apply_damage(state, state.defender, state.attacker, defender_damage)
            self._apply_ability_secondary(state, state.attacker, state.defender, attacker_damage, rng)
            self._apply_ability_secondary(state, state.defender, state.attacker, defender_damage, rng)
            self._apply_equipment_procs(
                state, state.attacker, state.defender, attacker_damage, rng
            )
            self._apply_equipment_procs(
                state, state.defender, state.attacker, defender_damage, rng
            )
            normalized = self._normalize_positions(state.attacker.position, state.defender.position)
            self._record_positions(state, normalized[0], normalized[1], "teleport_adjust")
            self._apply_damage(state, state.attacker, state.defender, attacker_followup, "followup")
            self._apply_damage(state, state.defender, state.attacker, defender_followup, "followup")
            self._try_counterattack(state, state.defender, state.attacker, attacker_damage, rng)
            self._try_counterattack(state, state.attacker, state.defender, defender_damage, rng)
            self._apply_knockbacks(
                state,
                pre_attacker_position,
                pre_defender_position,
                attacker_damage,
                defender_damage,
            )

            if not state.attacker.alive or not state.defender.alive:
                if not state.attacker.alive and not state.defender.alive:
                    winner = self._resolve_double_ko(
                        state, pre_attacker_hp, pre_defender_hp, rng
                    )
                    state.finish_reason = "double_ko_tiebreak"
                else:
                    winner = state.attacker if state.attacker.alive else state.defender
                    state.finish_reason = "knockout"
                loser = state.defender if winner is state.attacker else state.attacker
                state.events.append(
                    BattleEvent(
                        tick,
                        "knockout",
                        winner.snapshot.user_pk,
                        loser.snapshot.user_pk,
                    )
                )
                return self._result(state, winner, loser)

        winner = self._resolve_timeout(state, rng)
        loser = state.defender if winner is state.attacker else state.attacker
        state.finish_reason = "timeout_remaining_hp"
        state.events.append(
            BattleEvent(
                state.tick,
                "timeout",
                winner.snapshot.user_pk,
                loser.snapshot.user_pk,
            )
        )
        return self._result(state, winner, loser)

    def _fighter_from_initial(
        self,
        snapshot: FighterSnapshot,
        position: int,
        initial: FighterContinuationState | None,
    ) -> FighterState:
        fighter = FighterState(
            snapshot,
            snapshot.max_hp,
            position,
            stamina=snapshot.max_sp,
            mana=snapshot.max_mp,
        )
        self.ability_runtime.stat_resolver.initialize(fighter)
        if initial is None:
            return fighter

        fighter.statuses = {
            str(data["status_id"]): CombatStatus(**data)
            for data in initial.statuses
            if data.get("status_id") and int(data.get("remaining_ticks", 0)) > 0
        }
        fighter.skill_cooldowns = {
            str(key): max(0, int(value))
            for key, value in initial.skill_cooldowns.items()
            if int(value) > 0
        }
        fighter.attack_cooldown = max(0, int(initial.attack_cooldown))
        fighter.recovery_ticks = max(0, int(initial.recovery_ticks))
        fighter.hitstun_ticks = max(0, int(initial.hitstun_ticks))
        fighter.counter_cooldown = max(0, int(initial.counter_cooldown))
        fighter.stance_id = (
            initial.stance_id
            if initial.stance_id in fighter.statuses else None
        )
        fighter.lethal_survival_used = bool(initial.lethal_survival_used)
        fighter.hp_regen_buffer = max(0.0, float(initial.hp_regen_buffer))
        fighter.mp_regen_buffer = max(0.0, float(initial.mp_regen_buffer))
        fighter.sp_regen_buffer = max(0.0, float(initial.sp_regen_buffer))
        fighter.recovery_turn_phase = (
            max(0, int(initial.recovery_turn_phase)) % 5
        )
        if fighter.statuses:
            self.ability_runtime.stat_resolver.refresh(fighter)

        fighter.current_hp = max(
            0, min(fighter.max_hp, round(fighter.max_hp * initial.hp_ratio))
        )
        fighter.mana = min(
            fighter.max_mp, round(fighter.max_mp * initial.mana_ratio)
        )
        fighter.stamina = max(
            0, min(fighter.max_sp, round(fighter.max_sp * initial.stamina_ratio))
        )
        if fighter.stance_id:
            fighter.frozen_mana_capacity = max(
                0,
                min(
                    fighter.max_mp,
                    round(
                        fighter.max_mp
                        * initial.frozen_mana_capacity_ratio
                    ),
                ),
            )
            fighter.frozen_mana = max(
                0,
                min(
                    fighter.frozen_mana_capacity,
                    round(fighter.max_mp * initial.frozen_mana_ratio),
                ),
            )
            fighter.mana = min(
                fighter.mana,
                max(0, fighter.max_mp - fighter.frozen_mana_capacity),
            )
        return fighter

    @staticmethod
    def _continuation_state(
        fighter: FighterState,
    ) -> FighterContinuationState:
        max_hp = max(1, fighter.max_hp)
        max_mp = max(1, fighter.max_mp)
        max_sp = max(1, fighter.max_sp)
        return FighterContinuationState(
            hp_ratio=fighter.current_hp / max_hp,
            mana_ratio=fighter.mana / max_mp,
            stamina_ratio=fighter.stamina / max_sp,
            hp_regen_buffer=fighter.hp_regen_buffer,
            mp_regen_buffer=fighter.mp_regen_buffer,
            sp_regen_buffer=fighter.sp_regen_buffer,
            recovery_turn_phase=fighter.recovery_turn_phase,
            statuses=tuple(
                status.to_dict() for status in fighter.statuses.values()
            ),
            skill_cooldowns=dict(fighter.skill_cooldowns),
            attack_cooldown=fighter.attack_cooldown,
            recovery_ticks=fighter.recovery_ticks,
            hitstun_ticks=fighter.hitstun_ticks,
            counter_cooldown=fighter.counter_cooldown,
            stance_id=fighter.stance_id,
            frozen_mana_ratio=fighter.frozen_mana / max_mp,
            frozen_mana_capacity_ratio=(
                fighter.frozen_mana_capacity / max_mp
            ),
            lethal_survival_used=fighter.lethal_survival_used,
            defeated=not fighter.alive,
        )

    def _prepare_tick(
        self, state: BattleState, fighter: FighterState
    ) -> str | None:
        self._apply_passive_regen(state, fighter)
        if fighter.runtime_armor_style:
            regen = {"light": 10, "medium": 8, "heavy": 6}[fighter.runtime_armor_style]
            if fighter.runtime_overloaded:
                regen = max(1, regen // 2)
        else:
            regen = fighter.snapshot.equipment.stamina_regen if fighter.snapshot.equipment else 8
        maximum = fighter.max_sp
        fighter.stamina = min(maximum, fighter.stamina + regen)
        fighter.skill_cooldowns = {key: max(0, value - 1) for key, value in fighter.skill_cooldowns.items()}
        fighter.attack_cooldown = max(0, fighter.attack_cooldown - 1)
        fighter.counter_cooldown = max(0, fighter.counter_cooldown - 1)
        fighter.guarding = False
        if fighter.hitstun_ticks > 0:
            fighter.hitstun_ticks -= 1
            return "hitstun"
        if fighter.recovery_ticks > 0:
            fighter.recovery_ticks -= 1
            return "recovery"
        if fighter.attack_pending:
            fighter.windup_ticks = max(0, fighter.windup_ticks - 1)
            if fighter.windup_ticks == 0:
                return "attack_ready"
            return "windup"
        return None

    def _apply_passive_regen(
        self, state: BattleState, fighter: FighterState
    ) -> None:
        derived = fighter.current_derived
        if not derived:
            return
        if fighter.current_hp < fighter.max_hp and not self.ability_runtime.has(fighter, "healing_block"):
            fighter.hp_regen_buffer += derived.hp_regen_per_tick * max(0.0, 1 + self.ability_runtime.modifier(fighter, "healing"))
            amount = min(
                fighter.max_hp - fighter.current_hp,
                int(fighter.hp_regen_buffer),
            )
            if amount > 0:
                fighter.hp_regen_buffer -= amount
                fighter.current_hp += amount
                state.events.append(
                    BattleEvent(
                        state.tick,
                        "recover_hp",
                        fighter.snapshot.user_pk,
                        value=amount,
                        remaining_hp=fighter.current_hp,
                    )
                )
        else:
            fighter.hp_regen_buffer = 0.0
        available_max_mp = max(0, fighter.max_mp - fighter.frozen_mana_capacity)
        if fighter.mana < available_max_mp and not self.ability_runtime.has(fighter, "mp_regen_frozen"):
            mp_regen_multiplier = 1.50 if self.ability_runtime.has(fighter, "insight") else 1.0
            fighter.mp_regen_buffer += derived.mp_regen_per_tick * mp_regen_multiplier
            amount = min(
                available_max_mp - fighter.mana,
                int(fighter.mp_regen_buffer),
            )
            if amount > 0:
                fighter.mp_regen_buffer -= amount
                fighter.mana += amount
                state.events.append(
                    BattleEvent(
                        state.tick,
                        "recover_mp",
                        fighter.snapshot.user_pk,
                        value=amount,
                        mana=fighter.mana,
                    )
                )
        else:
            fighter.mp_regen_buffer = 0.0
    def _intent_for_phase(
        self,
        own: FighterState,
        opponent: FighterState,
        profile: AIProfile,
        phase: str | None,
        rng: random.Random,
    ):
        if phase == "attack_ready":
            return self._intent("resolve_attack")
        if phase is not None:
            return self._intent(phase)
        return choose_action(own, opponent, profile, rng, self.ATTACK_RANGE)

    def _intent(self, action: str) -> ActionIntent:
        return ActionIntent(action)

    def _begin_attack(
        self,
        state: BattleState,
        fighter: FighterState,
        action: str,
        skill_id: str | None = None,
    ) -> None:
        if action not in {"basic_attack", "use_skill"}:
            return
        equipment = fighter.snapshot.equipment
        windup = equipment.attack_windup if equipment else self.ATTACK_WINDUP_TICKS
        resource_type = "sp"
        resource_cost = equipment.attack_stamina if equipment else 8
        definition = None
        mana_breakdown = None
        if action == "use_skill" and fighter.snapshot.skills and skill_id:
            definition = fighter.snapshot.skills.active_definitions.get(skill_id)
            if (
                not definition
                or fighter.skill_cooldowns.get(skill_id, 0) > 0
                or not self.ability_runtime.compatible(definition, fighter)
            ):
                return
            windup = definition.windup_ticks
            resource_type = definition.resource_type
            if definition.ability_type == "spell":
                mana_breakdown = self.ability_runtime.mana_cost_breakdown(
                    definition, fighter
                )
                resource_cost = mana_breakdown.final_cost
            else:
                resource_cost = self.ability_runtime.effective_cost(
                    definition, fighter
                )
        windup = self._scaled_ticks(fighter, windup)
        fighter.pending_resource_details = {}
        if resource_type == "sp":
            if fighter.stamina < resource_cost:
                return
            fighter.stamina -= resource_cost
        else:
            mana_before = fighter.mana
            fighter.mana -= resource_cost
            if mana_breakdown:
                fighter.pending_resource_details = {
                    "base_mana_cost": mana_breakdown.base_cost,
                    "level_mana_cost": mana_breakdown.level_cost,
                    "mana_cost_ratio": mana_breakdown.reduction_ratio,
                    "mana_cost": mana_breakdown.final_cost,
                    "mana_before": mana_before,
                    "mana_after": fighter.mana,
                    "spell_power": mana_breakdown.spell_power,
                }
            if fighter.mana < 0:
                reduction = (
                    fighter.current_derived.mana_overcast_reduction
                    if fighter.current_derived else 0.0
                )
                backlash = max(
                    1,
                    math.ceil(abs(fighter.mana) * 2 * (1 - reduction)),
                )
                fighter.current_hp = max(0, fighter.current_hp - backlash)
                state.events.append(BattleEvent(
                    state.tick, "mana_backlash", fighter.snapshot.user_pk,
                    fighter.snapshot.user_pk, value=backlash,
                    remaining_hp=fighter.current_hp, skill_id=skill_id,
                    mana=fighter.mana, damage_type="magic",
                    **fighter.pending_resource_details,
                ))
        fighter.attack_pending = True
        fighter.pending_skill_id = skill_id if action == "use_skill" else None
        fighter.attack_bonus_knockback = 0
        fighter.windup_ticks = windup
        if definition:
            event_kind = (
                "spell_cast_start"
                if definition.ability_type == "spell" else "skill_use"
            )
            state.events.append(BattleEvent(
                state.tick, event_kind, fighter.snapshot.user_pk,
                skill_id=skill_id, stamina=fighter.stamina, mana=fighter.mana,
                **fighter.pending_resource_details,
            ))
        state.events.append(BattleEvent(
            state.tick, "attack_windup", fighter.snapshot.user_pk,
            value=windup, skill_id=fighter.pending_skill_id,
            stamina=fighter.stamina, mana=fighter.mana,
        ))
    def _resolve_guards(
        self,
        state: BattleState,
        attacker_action: str,
        defender_action: str,
    ) -> None:
        state.attacker.guarding = attacker_action == "guard" and state.attacker.stamina >= 5
        state.defender.guarding = defender_action == "guard" and state.defender.stamina >= 5
        if state.attacker.guarding: state.attacker.stamina -= 5
        if state.defender.guarding: state.defender.stamina -= 5
        if state.attacker.guarding:
            state.events.append(
                BattleEvent(state.tick, "guard", state.attacker.snapshot.user_pk, stamina=state.attacker.stamina)
            )
        if state.defender.guarding:
            state.events.append(
                BattleEvent(state.tick, "guard", state.defender.snapshot.user_pk, stamina=state.defender.stamina)
            )

    def _movement_step(self, fighter: FighterState) -> int:
        if not fighter.current_derived:
            base = max(
                25, min(80, 20 + fighter.snapshot.stat("speed") * 2)
            )
            multiplier = (
                fighter.snapshot.equipment.movement_multiplier
                if fighter.snapshot.equipment else 1.0
            )
            for key in ("slow", "gravity"):
                if key in fighter.statuses:
                    multiplier *= max(0.30, 1 - fighter.statuses[key].magnitude)
            for speed_status in ("haste", "lulwy_possession"):
                if speed_status in fighter.statuses:
                    multiplier *= 1 + fighter.statuses[speed_status].magnitude
            multiplier *= max(
                0.30, 1 - self.ability_runtime.modifier(fighter, "slow")
            )
            return max(10, round(base * multiplier))
        multiplier = 1.0
        for key in ("slow", "gravity"):
            if key in fighter.statuses:
                multiplier *= max(0.30, 1 - fighter.statuses[key].magnitude)
        for speed_status in ("haste", "lulwy_possession"):
            if speed_status in fighter.statuses:
                multiplier *= 1 + fighter.statuses[speed_status].magnitude
        multiplier *= max(0.30, 1 - self.ability_runtime.modifier(fighter, "slow"))
        return max(10, min(80, round(35 * fighter.current_derived.action_speed / 100 * multiplier)))

    def _resolve_movement(
        self,
        state: BattleState,
        attacker_action: str,
        defender_action: str,
    ) -> None:
        attacker_position = state.attacker.position
        defender_position = state.defender.position
        if attacker_action in {"advance", "retreat"}: state.attacker.stamina = max(0, state.attacker.stamina - 2)
        if defender_action in {"advance", "retreat"}: state.defender.stamina = max(0, state.defender.stamina - 2)
        if attacker_action == "advance":
            attacker_position += self._movement_step(state.attacker)
        elif attacker_action == "retreat":
            attacker_position -= self._movement_step(state.attacker)
        if defender_action == "advance":
            defender_position -= self._movement_step(state.defender)
        elif defender_action == "retreat":
            defender_position += self._movement_step(state.defender)

        attacker_position, defender_position = self._normalize_positions(
            attacker_position,
            defender_position,
        )
        self._record_positions(state, attacker_position, defender_position, "move")

    def _resolve_attack_lunges(
        self,
        state: BattleState,
        attacker_action: str,
        defender_action: str,
    ) -> None:
        attacker_position = state.attacker.position
        defender_position = state.defender.position
        distance = defender_position - attacker_position
        if (
            attacker_action == "resolve_attack"
            and state.attacker.attack_pending
            and distance > self._attack_range(state.attacker)
        ):
            attacker_position += min(
                self.ATTACK_LUNGE_DISTANCE,
                distance - self._attack_range(state.attacker),
            )
        distance = defender_position - attacker_position
        if (
            defender_action == "resolve_attack"
            and state.defender.attack_pending
            and distance > self._attack_range(state.defender)
        ):
            defender_position -= min(
                self.ATTACK_LUNGE_DISTANCE,
                distance - self._attack_range(state.defender),
            )
        attacker_position, defender_position = self._normalize_positions(
            attacker_position,
            defender_position,
        )
        self._record_positions(
            state,
            attacker_position,
            defender_position,
            "attack_lunge",
        )
    def _normalize_positions(
        self,
        attacker_position: int,
        defender_position: int,
    ) -> tuple[int, int]:
        attacker_position = max(self.FIELD_MIN, min(self.FIELD_MAX, attacker_position))
        defender_position = max(self.FIELD_MIN, min(self.FIELD_MAX, defender_position))
        if defender_position - attacker_position < self.MIN_DISTANCE:
            midpoint = (attacker_position + defender_position) // 2
            attacker_position = max(
                self.FIELD_MIN,
                min(
                    self.FIELD_MAX - self.MIN_DISTANCE,
                    midpoint - self.MIN_DISTANCE // 2,
                ),
            )
            defender_position = attacker_position + self.MIN_DISTANCE
        return attacker_position, defender_position

    def _record_positions(
        self,
        state: BattleState,
        attacker_position: int,
        defender_position: int,
        kind: str,
    ) -> None:
        if attacker_position != state.attacker.position:
            previous = state.attacker.position
            state.attacker.position = attacker_position
            state.events.append(
                BattleEvent(
                    state.tick,
                    kind,
                    state.attacker.snapshot.user_pk,
                    value=abs(attacker_position - previous),
                    position=attacker_position,
                    stamina=state.attacker.stamina,
                )
            )
        if defender_position != state.defender.position:
            previous = state.defender.position
            state.defender.position = defender_position
            state.events.append(
                BattleEvent(
                    state.tick,
                    kind,
                    state.defender.snapshot.user_pk,
                    value=abs(defender_position - previous),
                    position=defender_position,
                    stamina=state.defender.stamina,
                )
            )

    def _attack_damage(
        self,
        state: BattleState,
        actor: FighterState,
        target: FighterState,
        action: str,
        rng: random.Random,
    ):
        if action != "resolve_attack" or not actor.attack_pending:
            return None
        skill_id = actor.pending_skill_id
        resource_details = dict(actor.pending_resource_details)
        definition = (
            actor.snapshot.skills.active_definitions.get(skill_id)
            if skill_id and actor.snapshot.skills else None
        )
        actor.attack_pending = False
        actor.pending_skill_id = None
        actor.pending_resource_details = {}
        actor.windup_ticks = 0
        recovery_ticks = (
            definition.recovery_ticks if definition
            else actor.snapshot.equipment.attack_recovery if actor.snapshot.equipment
            else self.ATTACK_RECOVERY_TICKS
        )
        actor.recovery_ticks = self._scaled_ticks(actor, recovery_ticks)
        actor.attack_cooldown = self._attack_cooldown(actor)
        if definition:
            actor.skill_cooldowns[skill_id] = definition.cooldown_ticks
        state.events.append(BattleEvent(
            state.tick, "attack", actor.snapshot.user_pk,
            target.snapshot.user_pk, skill_id=skill_id,
        ))
        state.events.append(BattleEvent(
            state.tick, "recovery", actor.snapshot.user_pk,
            value=actor.recovery_ticks, skill_id=skill_id,
        ))
        if definition and definition.ability_type == "spell":
            state.events.append(BattleEvent(
                state.tick, "spell_cast", actor.snapshot.user_pk,
                target.snapshot.user_pk, skill_id=skill_id,
                mana=actor.mana,
                **resource_details,
            ))
        attack_range = definition.cast_range if definition else self._attack_range(actor)
        is_self = bool(definition and definition.targeting in {"self", "ally", "ally_area"})
        if not is_self and abs(actor.position - target.position) > attack_range:
            state.events.append(BattleEvent(
                state.tick, "whiff", actor.snapshot.user_pk,
                target.snapshot.user_pk, skill_id=skill_id,
            ))
            return None
        has_damage = bool(definition and any(
            effect.effect_type in {"physical_damage", "magic_damage"}
            for effect in definition.effects
        ))
        is_spell = bool(definition and definition.ability_type == "spell")
        if not is_self:
            evade_chance = self._evade_chance(actor, target, is_spell=is_spell)
            if is_spell:
                high_accuracy = any(effect.params.get("high_accuracy") for effect in definition.effects)
                evade_chance *= 0.10 if high_accuracy else 0.35
                if self.ability_runtime.has(actor, "confusion"):
                    evade_chance = min(0.60, evade_chance + 0.25)
            if self.ability_runtime.has(actor, "blind"):
                evade_chance = min(0.70, evade_chance + 0.25)
            if self.ability_runtime.has(actor, "haze"):
                evade_chance = min(0.75, evade_chance + 0.10 * actor.statuses["haze"].stacks)
            if rng.random() < evade_chance:
                state.events.append(BattleEvent(
                    state.tick, "evade", target.snapshot.user_pk,
                    actor.snapshot.user_pk, skill_id=skill_id,
                ))
                return None
        if definition and not has_damage:
            return (0, False, False, 0, skill_id, {})
        if definition:
            return self.ability_runtime.damage_result(actor, target, definition, rng)

        if actor.current_derived and target.current_derived:
            target_defense = target.current_derived.defense * max(0.1, 1 + self.ability_runtime.modifier(target, "defense"))
            attack_power = actor.current_derived.attack_power
            offense_multiplier = actor.current_derived.physical_damage_multiplier
        else:
            target_defense = target.snapshot.stat("defense")
            attack_power = actor.snapshot.stat("atk") * 4.0
            offense_multiplier = 1.0
        equipment_effects = (
            actor.snapshot.equipment.combat_effects
            if actor.snapshot.equipment else {}
        )
        penetration = min(
            0.75,
            max(0.0, float(equipment_effects.get("armor_penetration", 0))),
        )
        target_defense *= 1.0 - penetration
        effect_multiplier = 1 + self.ability_runtime.modifier(actor, "physical_damage") - self.ability_runtime.modifier(actor, "damage_penalty")
        variance = rng.uniform(0.90, 1.10)
        physical_reduction = (target.current_derived.physical_reduction if target.current_derived else 0.0) + self.ability_runtime.modifier(target, "physical_reduction")
        breakdown = {
            "physical": physical_damage_amount(
                attack_power=attack_power,
                offense_multiplier=offense_multiplier,
                effect_multiplier=effect_multiplier,
                variance=variance,
                defense=target_defense,
                attacker_level=actor.snapshot.level,
                physical_reduction=physical_reduction,
            )
        }
        if actor.current_derived and target.current_derived:
            for damage_type, bonus in actor.current_derived.elemental_damage.items():
                if bonus <= 0:
                    continue
                resistance = target.current_derived.resistances.get(damage_type, 0.0)
                elemental = round(
                    bonus * variance
                    * resistance_multiplier(resistance, actor.snapshot.level)
                    * (1 - target.current_derived.magical_reduction)
                )
                if elemental > 0:
                    breakdown[damage_type] = elemental
        critical = rng.random() < self._critical_chance(actor, target)
        if critical:
            critical_damage = actor.current_derived.critical_damage if actor.current_derived else 1.5
            breakdown = {kind: max(1, round(value * critical_damage)) for kind, value in breakdown.items()}
        passive_block = (target.snapshot.equipment.block_rate if target.snapshot.equipment else 0.0) + self.ability_runtime.modifier(target, "block")
        guarded = target.guarding or rng.random() < min(0.75, passive_block)
        if guarded:
            breakdown = {kind: max(1, round(value * self.GUARD_DAMAGE_MULTIPLIER)) for kind, value in breakdown.items()}
        return sum(breakdown.values()), critical, guarded, 0, None, breakdown
    def _apply_ability_secondary(self, state, actor, target, damage_result, rng):
        if not damage_result or len(damage_result) < 5 or not damage_result[4]:
            return
        definition = (
            actor.snapshot.skills.active_definitions.get(damage_result[4])
            if actor.snapshot.skills else None
        )
        if definition:
            self.ability_runtime.apply_secondary(
                state, actor, target, definition, damage_result, rng
            )

    def _apply_equipment_procs(
        self, state, actor, target, damage_result, rng
    ) -> None:
        if not self._is_direct_weapon_hit(actor, damage_result):
            return
        self.equipment_proc_resolver.resolve(
            state,
            actor,
            target,
            damage_result,
            rng,
            self._apply_damage,
        )

    @staticmethod
    def _is_direct_weapon_hit(actor, damage_result) -> bool:
        if not damage_result or damage_result[0] <= 0:
            return False
        ability_id = damage_result[4] if len(damage_result) > 4 else None
        if not ability_id:
            return True
        definition = (
            actor.snapshot.skills.active_definitions.get(ability_id)
            if actor.snapshot.skills else None
        )
        return bool(
            definition
            and definition.ability_type != "spell"
            and any(
                effect.effect_type == "physical_damage"
                for effect in definition.effects
            )
        )
    def _followup_damage(self, state, actor, target, first_result, rng):
        if first_result is None or not actor.snapshot.equipment:
            return None
        equipment = actor.snapshot.equipment
        ability_id = first_result[4] if len(first_result) > 4 else None
        definition = (
            actor.snapshot.skills.active_definitions.get(ability_id)
            if ability_id and actor.snapshot.skills else None
        )
        if definition and definition.ability_type == "spell":
            return None
        forced = actor.snapshot.weapon_mode == "dual_wield"
        chance = equipment.ranged_followup if actor.snapshot.is_ranged else equipment.melee_followup
        chance += self.ability_runtime.modifier(actor, "ranged_followup" if actor.snapshot.is_ranged else "followup")
        if not forced and rng.random() >= chance:
            return None
        if rng.random() < self._evade_chance(actor, target):
            state.events.append(BattleEvent(state.tick, "evade", target.snapshot.user_pk, actor.snapshot.user_pk))
            return None
        scale = 1.0 if forced else 0.5
        source_breakdown = (
            first_result[5]
            if len(first_result) > 5 else {"physical": first_result[0]}
        )
        breakdown = {
            damage_type: max(1, round(value * scale))
            for damage_type, value in source_breakdown.items()
        }
        critical = rng.random() < self._critical_chance(actor, target)
        if critical:
            critical_damage = (
                actor.current_derived.critical_damage
                if actor.current_derived else 1.5
            )
            breakdown = {
                damage_type: max(1, round(value * critical_damage))
                for damage_type, value in breakdown.items()
            }
        guarded = target.guarding
        if guarded:
            breakdown = {
                damage_type: max(
                    1, round(value * self.GUARD_DAMAGE_MULTIPLIER)
                )
                for damage_type, value in breakdown.items()
            }
        damage = sum(breakdown.values())
        skill_id = first_result[4] if len(first_result) > 4 else None
        state.events.append(
            BattleEvent(
                state.tick,
                "followup_trigger",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                skill_id=skill_id,
            )
        )
        return damage, critical, guarded, 0, skill_id, breakdown
    def _attack_range(self, fighter: FighterState) -> int:
        return (
            fighter.snapshot.equipment.attack_range
            if fighter.snapshot.equipment else self.ATTACK_RANGE
        )

    def _scaled_ticks(self, fighter: FighterState, ticks: int) -> int:
        action_speed = (
            fighter.current_derived.action_speed
            if fighter.current_derived
            else 100
        )
        if "slow" in fighter.statuses:
            action_speed *= max(0.30, 1 - fighter.statuses["slow"].magnitude)
        if "gravity" in fighter.statuses:
            action_speed *= max(0.30, 1 - fighter.statuses["gravity"].magnitude)
        for speed_status in ("haste", "lulwy_possession"):
            if speed_status in fighter.statuses:
                action_speed *= 1 + fighter.statuses[speed_status].magnitude
        action_speed *= max(
            0.30, 1 - self.ability_runtime.modifier(fighter, "slow")
        )
        if fighter.snapshot.is_ranged:
            action_speed *= 1 + self.ability_runtime.modifier(fighter, "ranged_speed")
        return max(1, round(ticks * 100 / max(50, action_speed)))

    def _attack_cooldown(self, fighter: FighterState) -> int:
        base = (
            fighter.snapshot.equipment.attack_cooldown
            if fighter.snapshot.equipment else 6
        )
        return self._scaled_ticks(fighter, base)

    def _accuracy_multiplier(self, fighter: FighterState, is_spell: bool) -> float:
        if fighter.current_derived:
            physical = fighter.current_derived.physical_accuracy_multiplier
            spell = fighter.current_derived.spell_accuracy_multiplier
        elif fighter.snapshot.equipment:
            physical = fighter.snapshot.equipment.physical_accuracy_multiplier
            spell = fighter.snapshot.equipment.spell_accuracy_multiplier
        else:
            physical = spell = 1.0
        return spell if is_spell else physical

    def _evade_chance(
        self, actor: FighterState, target: FighterState, *, is_spell: bool = False
    ) -> float:
        if actor.current_derived and target.current_derived:
            accuracy = (
                actor.current_derived.accuracy
                * self._accuracy_multiplier(actor, is_spell)
                * max(0.1, 1 + self.ability_runtime.modifier(actor, "accuracy"))
            )
            evasion = target.current_derived.evasion * max(0.1, 1 + self.ability_runtime.modifier(target, "evasion"))
            evade = 1.0 - hit_chance(
                accuracy, evasion, is_spell=is_spell
            )
            if self.ability_runtime.has(target, "martial_awakening"):
                level = target.skill_level("unarmed")
                if level >= 80: evade = max(evade, 0.10)
            return evade
        speed_diff = actor.snapshot.stat("speed") - target.snapshot.stat("speed")
        luck_diff = actor.snapshot.stat("luck") - target.snapshot.stat("luck")
        dodge = (
            target.snapshot.skills.effective_levels.get("dodge", 0)
            if target.snapshot.skills else 0
        )
        return max(
            0.0,
            min(
                0.20,
                -speed_diff * 0.005
                - luck_diff * 0.002
                + dodge * 0.0005,
            ),
        )

    def _critical_chance(self, actor: FighterState, target: FighterState) -> float:
        if actor.current_derived:
            return actor.current_derived.critical_rate
        luck_diff = actor.snapshot.stat("luck") - target.snapshot.stat("luck")
        return max(0.05, min(0.25, 0.05 + luck_diff * 0.005))

    def _apply_damage(
        self,
        state: BattleState,
        actor: FighterState,
        target: FighterState,
        damage_result: tuple[int, bool, bool, int, str | None] | None,
        event_kind: str = "damage",
        allow_on_hit_effects: bool = True,
    ) -> None:
        if damage_result is None:
            return
        damage, critical, guarded = damage_result[:3]
        if damage <= 0:
            return
        if (
            damage >= target.current_hp
            and self.ability_runtime.has(target, "shield_wall")
            and not target.lethal_survival_used
            and (state.random_seed + state.tick + target.snapshot.user_pk) % 4 == 0
        ):
            damage = max(0, target.current_hp - 1)
            target.lethal_survival_used = True
        target.current_hp = max(0, target.current_hp - damage)
        actor.damage_dealt += damage
        state.events.append(
            BattleEvent(
                state.tick,
                event_kind,
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                value=damage,
                remaining_hp=target.current_hp,
                critical=critical,
                guarded=guarded,
                skill_id=damage_result[4] if len(damage_result) > 4 else None,
                damage_type=(
                    next(iter(damage_result[5]))
                    if len(damage_result) > 5 and len(damage_result[5]) == 1
                    else "mixed"
                ),
                damage_breakdown=(
                    dict(damage_result[5]) if len(damage_result) > 5 else {}
                ),
                armor_style=(
                    target.runtime_armor_style
                    or target.snapshot.armor_style
                ),
            )
        )
        equipment_effects = (
            actor.snapshot.equipment.combat_effects
            if actor.snapshot.equipment else {}
        )
        life_steal = min(
            0.50,
            max(0.0, float(equipment_effects.get("life_steal", 0))),
        ) if allow_on_hit_effects else 0.0
        if life_steal and actor.current_hp > 0 and actor.current_hp < actor.max_hp:
            recovered = min(
                actor.max_hp - actor.current_hp,
                max(1, round(damage * life_steal)),
            )
            actor.current_hp += recovered
            state.events.append(
                BattleEvent(
                    state.tick,
                    "life_steal",
                    actor.snapshot.user_pk,
                    actor.snapshot.user_pk,
                    value=recovered,
                    remaining_hp=actor.current_hp,
                )
            )

        if target.attack_pending and target.windup_ticks > 0:
            target.attack_pending = False
            target.pending_skill_id = None
            target.pending_resource_details = {}
            target.attack_bonus_knockback = 0
            target.windup_ticks = 0
            target.recovery_ticks = max(target.recovery_ticks, 1)
            state.events.append(
                BattleEvent(
                    state.tick,
                    "attack_interrupted",
                    actor.snapshot.user_pk,
                    target.snapshot.user_pk,
                )
            )

        hitstun = (
            self.CRITICAL_HITSTUN_TICKS if critical else self.HITSTUN_TICKS
        )
        target.hitstun_ticks = max(target.hitstun_ticks, hitstun)
        state.events.append(
            BattleEvent(
                state.tick,
                "hitstun",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                value=hitstun,
            )
        )

    def _knockback_distance(
        self,
        damage_result: tuple[int, bool, bool, int, str | None] | None,
    ) -> int:
        if damage_result is None:
            return 0
        damage, critical, guarded = damage_result[:3]
        distance = self.KNOCKBACK_BASE + damage // 2
        if critical:
            distance += 10
        distance += damage_result[3] if len(damage_result) > 3 else 0
        if guarded:
            distance = max(10, round(distance * 0.65))
        return min(self.KNOCKBACK_MAX, distance)

    def _try_counterattack(self, state, reactor, target, incoming_result, rng):
        if not incoming_result or incoming_result[0] <= 0 or not reactor.alive:
            return
        hold_line = self.ability_runtime.has(reactor, "hold_the_line")
        never_retreat = self.ability_runtime.has(reactor, "never_retreat")
        if not (hold_line or never_retreat) or reactor.counter_cooldown > 0:
            return
        if abs(reactor.position - target.position) > self._attack_range(reactor):
            return
        if not hold_line and rng.random() >= 0.30:
            return
        derived = reactor.current_derived
        target_derived = target.current_derived
        base = max(
            1,
            round(
                (derived.attack_power * 1.3 if derived else reactor.snapshot.stat("atk") * 3)
                - (target_derived.defense * 0.5 if target_derived else target.snapshot.stat("defense") * 0.5)
            ),
        )
        critical = rng.random() < self._critical_chance(reactor, target)
        if critical:
            base = round(base * (derived.critical_damage if derived else 1.5))
        reactor.counter_cooldown = 6 if hold_line else 3
        state.events.append(BattleEvent(state.tick, "counter_trigger", reactor.snapshot.user_pk, target.snapshot.user_pk, value=reactor.counter_cooldown, status_id="hold_the_line" if hold_line else "never_retreat"))
        self._apply_damage(
            state, reactor, target,
            (base, critical, False, 0, None, {"physical": base}),
            "counter_damage",
        )
    def _apply_knockbacks(
        self,
        state: BattleState,
        attacker_position: int,
        defender_position: int,
        attacker_damage: tuple[int, bool, bool, int, str | None] | None,
        defender_damage: tuple[int, bool, bool, int, str | None] | None,
    ) -> None:
        attacker_knockback = self._knockback_distance(defender_damage)
        defender_knockback = self._knockback_distance(attacker_damage)
        if state.attacker.snapshot.equipment:
            attacker_knockback = round(attacker_knockback * (1 - state.attacker.snapshot.equipment.knockback_resistance))
        if state.defender.snapshot.equipment:
            defender_knockback = round(defender_knockback * (1 - state.defender.snapshot.equipment.knockback_resistance))
        new_attacker_position = attacker_position - attacker_knockback
        new_defender_position = defender_position + defender_knockback
        new_attacker_position, new_defender_position = self._normalize_positions(
            new_attacker_position,
            new_defender_position,
        )

        if new_attacker_position != state.attacker.position:
            moved = abs(new_attacker_position - state.attacker.position)
            state.attacker.position = new_attacker_position
            state.events.append(
                BattleEvent(
                    state.tick,
                    "knockback",
                    state.defender.snapshot.user_pk,
                    state.attacker.snapshot.user_pk,
                    value=moved,
                    position=new_attacker_position,
                )
            )
        if new_defender_position != state.defender.position:
            moved = abs(new_defender_position - state.defender.position)
            state.defender.position = new_defender_position
            state.events.append(
                BattleEvent(
                    state.tick,
                    "knockback",
                    state.attacker.snapshot.user_pk,
                    state.defender.snapshot.user_pk,
                    value=moved,
                    position=new_defender_position,
                )
            )

    def _resolve_double_ko(
        self,
        state: BattleState,
        pre_attacker_hp: int,
        pre_defender_hp: int,
        rng: random.Random,
    ) -> FighterState:
        attacker_ratio = pre_attacker_hp / state.attacker.max_hp
        defender_ratio = pre_defender_hp / state.defender.max_hp
        if attacker_ratio != defender_ratio:
            return state.attacker if attacker_ratio > defender_ratio else state.defender
        return self._resolve_equal_score(state, rng)

    def _resolve_timeout(
        self,
        state: BattleState,
        rng: random.Random,
    ) -> FighterState:
        if state.attacker.current_hp != state.defender.current_hp:
            return (
                state.attacker
                if state.attacker.current_hp > state.defender.current_hp
                else state.defender
            )
        return self._resolve_equal_score(state, rng)

    def _resolve_equal_score(
        self,
        state: BattleState,
        rng: random.Random,
    ) -> FighterState:
        if state.attacker.damage_dealt != state.defender.damage_dealt:
            return (
                state.attacker
                if state.attacker.damage_dealt > state.defender.damage_dealt
                else state.defender
            )
        attacker_speed = (
            state.attacker.current_derived.action_speed
            if state.attacker.current_derived
            else state.attacker.snapshot.stat("speed")
        )
        defender_speed = (
            state.defender.current_derived.action_speed
            if state.defender.current_derived
            else state.defender.snapshot.stat("speed")
        )
        if attacker_speed != defender_speed:
            return state.attacker if attacker_speed > defender_speed else state.defender
        attacker_chance = 0.50
        if not state.attacker.current_derived and not state.defender.current_derived:
            luck_diff = (
                state.attacker.snapshot.stat("luck")
                - state.defender.snapshot.stat("luck")
            )
            attacker_chance = max(
                0.20, min(0.80, 0.50 + luck_diff * 0.01)
            )
        return (
            state.attacker
            if rng.random() < attacker_chance
            else state.defender
        )

    def _result(
        self,
        state: BattleState,
        winner: FighterState,
        loser: FighterState,
    ) -> SimulationResult:
        return SimulationResult(
            attacker=state.attacker.snapshot,
            defender=state.defender.snapshot,
            winner_pk=winner.snapshot.user_pk,
            loser_pk=loser.snapshot.user_pk,
            duration_ticks=state.tick,
            finish_reason=state.finish_reason,
            attacker_remaining_hp=state.attacker.current_hp,
            defender_remaining_hp=state.defender.current_hp,
            attacker_damage_dealt=state.attacker.damage_dealt,
            defender_damage_dealt=state.defender.damage_dealt,
            events=tuple(state.events),
            random_seed=state.random_seed,
            engine_version=self.ENGINE_VERSION,
            attacker_remaining_stamina=state.attacker.stamina,
            defender_remaining_stamina=state.defender.stamina,
            attacker_remaining_mana=state.attacker.mana,
            defender_remaining_mana=state.defender.mana,
            attacker_frozen_mana=state.attacker.frozen_mana,
            defender_frozen_mana=state.defender.frozen_mana,
            attacker_final_statuses=tuple(s.to_dict() for s in state.attacker.statuses.values()),
            defender_final_statuses=tuple(s.to_dict() for s in state.defender.statuses.values()),
            final_entities=tuple(e.to_dict() for e in state.entities),
            final_zones=tuple(z.to_dict() for z in state.zones),
            attacker_final_state=self._continuation_state(state.attacker),
            defender_final_state=self._continuation_state(state.defender),
        )
