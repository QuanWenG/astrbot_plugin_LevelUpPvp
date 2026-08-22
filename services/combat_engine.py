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
        mana_overcast_backlash,
        mana_overcast_within_limit,
        physical_damage_amount,
        pvp_burst_cap,
        resistance_multiplier,
        spell_interrupt_damage_threshold,
        split_arrow_followup_multiplier,
        tempo_multiplier,
        triangular_variance,
    )
    from .ability_runtime import AbilityRuntime
    from .combat_ai import choose_action, tactic_resolution
    from .combat_random import KeyedEntropy, KeyedRandomStream
    from .combat_ruleset import DEFAULT_RULESET_REGISTRY, RuleSetRegistry
    from .equipment_proc_service import EquipmentProcResolver
    from .tactic_rules import phase_for_state
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
        mana_overcast_backlash,
        mana_overcast_within_limit,
        physical_damage_amount,
        pvp_burst_cap,
        resistance_multiplier,
        spell_interrupt_damage_threshold,
        split_arrow_followup_multiplier,
        tempo_multiplier,
        triangular_variance,
    )
    from services.ability_runtime import AbilityRuntime
    from services.combat_ai import choose_action, tactic_resolution
    from services.combat_random import KeyedEntropy, KeyedRandomStream
    from services.combat_ruleset import DEFAULT_RULESET_REGISTRY, RuleSetRegistry
    from services.equipment_proc_service import EquipmentProcResolver
    from services.tactic_rules import phase_for_state


class SideviewCombatEngine:
    ENGINE_VERSION = "sideview-v11"
    FIELD_MIN = 0
    FIELD_MAX = 1000
    ATTACKER_START = 200
    DEFENDER_START = 800
    MIN_DISTANCE = 30
    ATTACK_RANGE = 100
    ATTACK_LUNGE_DISTANCE = 30
    MAX_TICKS = 160
    GUARD_DAMAGE_MULTIPLIER = 0.55
    ATTACK_WINDUP_TICKS = 1
    ATTACK_RECOVERY_TICKS = 2
    HITSTUN_TICKS = 1
    CRITICAL_HITSTUN_TICKS = 2
    KNOCKBACK_BASE = 20
    KNOCKBACK_MAX = 60
    RAIN_FIRE_DAMAGE_MULTIPLIER = 0.90
    RAIN_LIGHTNING_DAMAGE_MULTIPLIER = 1.15
    ETHER_CAST_WINDOW_TICKS = 18
    ETHER_BACKLASH_BASE_HP_RATIO = 0.02
    ETHER_BACKLASH_STRESS_HP_RATIO = 0.015
    ETHER_BACKLASH_HP_RATIO_CAP = 0.10
    DIRECT_EVADE_TARGETING = frozenset({"single", "line", "projectile"})

    # Every environment understood by this ruleset.  Callers may force any of
    # these IDs for PvE, operations, and exact replay reconstruction.
    SUPPORTED_ENVIRONMENTS = (
        ("calm", 45),
        ("rain", 12),
        ("fog", 12),
        ("strong_wind", 10),
        ("close_quarters", 10),
        ("mana_tide", 6),
        ("ether_disturbance", 5),
    )
    # Rated PvP avoids environments which can dominate a build matchup before
    # either player makes a tactical choice.  The ordinary weather pool still
    # creates variety, with calm fights remaining the clear majority.
    DEFAULT_RATED_ENVIRONMENTS = (
        ("calm", 60),
        ("rain", 15),
        ("fog", 15),
        ("strong_wind", 10),
    )
    # Transitional public name retained for scripts and integrations written
    # before supported environments and the rated random pool were separated.
    ENVIRONMENTS = SUPPORTED_ENVIRONMENTS

    def __init__(
        self,
        ruleset_id: str = "sideview-v11",
        registry: RuleSetRegistry | None = None,
    ):
        self.ruleset_registry = registry or DEFAULT_RULESET_REGISTRY
        self.ruleset = self.ruleset_registry.require(ruleset_id)
        self.ability_runtime = AbilityRuntime(ruleset=self.ruleset)
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
        *,
        environment_id: str | None = None,
        random_environment_pool: tuple[tuple[str, float], ...] | None = None,
    ) -> SimulationResult:
        entropy = KeyedEntropy(self.ruleset.ruleset_id, random_seed)

        def rng_for(
            stream: str,
            tick: int = 0,
            actor: int | str | None = None,
            action_seq: int = 0,
        ) -> KeyedRandomStream:
            return KeyedRandomStream(
                entropy,
                stream=stream,
                tick=tick,
                actor=actor,
                action_seq=action_seq,
            )

        available_environments = tuple(
            item[0] for item in self.SUPPORTED_ENVIRONMENTS
        )
        if environment_id is None:
            environment_pool = tuple(
                self.DEFAULT_RATED_ENVIRONMENTS
                if random_environment_pool is None
                else random_environment_pool
            )
            if not environment_pool:
                raise ValueError("随机战斗环境池不能为空")
            pool_environments = tuple(item[0] for item in environment_pool)
            if (
                len(set(pool_environments)) != len(pool_environments)
                or any(
                    item not in available_environments
                    for item in pool_environments
                )
            ):
                raise ValueError("随机战斗环境池包含未知或重复环境")
            environment_id = entropy.weighted_choice(
                pool_environments,
                tuple(item[1] for item in environment_pool),
                stream="combat.environment",
            )
        else:
            environment_id = str(environment_id).strip()
            if environment_id not in available_environments:
                raise ValueError(f"未知战斗环境：{environment_id}")
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
            ruleset_id=self.ruleset.ruleset_id,
            environment_id=environment_id,
        )
        if environment_id == "close_quarters":
            state.attacker.position = 300
            state.defender.position = 700
        state.events.append(
            BattleEvent(0, "battle_context", status_id=environment_id)
        )

        for tick in range(1, self.ruleset.timeout.hard_tick_limit + 1):
            state.tick = tick
            pre_status_attacker_hp = state.attacker.current_hp
            pre_status_defender_hp = state.defender.current_hp
            self.ability_runtime.tick(
                state,
                rng_for("combat.status_tick", tick),
                apply_damage=self._apply_damage,
            )
            if not state.attacker.alive or not state.defender.alive:
                if not state.attacker.alive and not state.defender.alive:
                    winner = self._resolve_double_ko(
                        state, pre_status_attacker_hp,
                        pre_status_defender_hp,
                        rng_for("combat.tiebreak", tick),
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
            self._record_tactic_phase(
                state,
                attacker_profile,
                defender_profile,
            )
            attacker_phase = self._prepare_tick(state, state.attacker)
            defender_phase = self._prepare_tick(state, state.defender)
            attacker_intent = self._intent_for_phase(
                state,
                state.attacker,
                state.defender,
                attacker_profile,
                defender_profile,
                attacker_phase,
                rng_for(
                    "combat.ai",
                    tick,
                    state.attacker.snapshot.user_pk,
                ),
            )
            defender_intent = self._intent_for_phase(
                state,
                state.defender,
                state.attacker,
                defender_profile,
                attacker_profile,
                defender_phase,
                rng_for(
                    "combat.ai",
                    tick,
                    state.defender.snapshot.user_pk,
                ),
            )
            if attacker_intent.action == "rest":
                self._rest(state, state.attacker)
            if defender_intent.action == "rest":
                self._rest(state, state.defender)
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
                state,
                state.attacker,
                state.defender,
                attacker_intent.action,
                rng_for(
                    "combat.strike",
                    tick,
                    state.attacker.snapshot.user_pk,
                ),
                rng_for(
                    "combat.fortune",
                    tick,
                    state.attacker.snapshot.user_pk,
                    0,
                ),
                rng_for(
                    "combat.fortune",
                    tick,
                    state.defender.snapshot.user_pk,
                    1,
                ),
            )
            defender_damage = self._attack_damage(
                state,
                state.defender,
                state.attacker,
                defender_intent.action,
                rng_for(
                    "combat.strike",
                    tick,
                    state.defender.snapshot.user_pk,
                ),
                rng_for(
                    "combat.fortune",
                    tick,
                    state.defender.snapshot.user_pk,
                    0,
                ),
                rng_for(
                    "combat.fortune",
                    tick,
                    state.attacker.snapshot.user_pk,
                    1,
                ),
            )
            pre_attacker_hp = state.attacker.current_hp
            pre_defender_hp = state.defender.current_hp
            attacker_damage = self._apply_damage(
                state, state.attacker, state.defender, attacker_damage
            )
            defender_damage = self._apply_damage(
                state, state.defender, state.attacker, defender_damage
            )
            attacker_followup = self._followup_damage(
                state, state.attacker, state.defender, attacker_damage,
                rng_for("combat.followup", tick, state.attacker.snapshot.user_pk),
            )
            defender_followup = self._followup_damage(
                state, state.defender, state.attacker, defender_damage,
                rng_for("combat.followup", tick, state.defender.snapshot.user_pk),
            )
            self._apply_ability_secondary(
                state, state.attacker, state.defender, attacker_damage,
                rng_for("combat.secondary", tick, state.attacker.snapshot.user_pk),
            )
            self._apply_ability_secondary(
                state, state.defender, state.attacker, defender_damage,
                rng_for("combat.secondary", tick, state.defender.snapshot.user_pk),
            )
            self._apply_equipment_procs(
                state, state.attacker, state.defender, attacker_damage,
                rng_for("combat.equipment_proc", tick, state.attacker.snapshot.user_pk),
            )
            self._apply_equipment_procs(
                state, state.defender, state.attacker, defender_damage,
                rng_for("combat.equipment_proc", tick, state.defender.snapshot.user_pk),
            )
            normalized = self._normalize_positions(state.attacker.position, state.defender.position)
            self._record_positions(state, normalized[0], normalized[1], "teleport_adjust")
            self._apply_damage(state, state.attacker, state.defender, attacker_followup, "followup")
            self._apply_damage(state, state.defender, state.attacker, defender_followup, "followup")
            self._try_counterattack(
                state, state.defender, state.attacker, attacker_damage,
                rng_for("combat.counter", tick, state.defender.snapshot.user_pk),
            )
            self._try_counterattack(
                state, state.attacker, state.defender, defender_damage,
                rng_for("combat.counter", tick, state.attacker.snapshot.user_pk),
            )
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
                        state, pre_attacker_hp, pre_defender_hp,
                        rng_for("combat.tiebreak", tick),
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

        winner = self._resolve_timeout(
            state,
            rng_for("combat.timeout", state.tick),
        )
        loser = state.defender if winner is state.attacker else state.attacker
        state.finish_reason = "timeout_score"
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
        luck = (
            snapshot.advanced_attributes.luck
            if snapshot.advanced_attributes else snapshot.stat("luck")
        )
        fortune = self.ruleset.fortune
        if fortune.charge_cap > 0:
            fighter.fortune_charges = max(
                0,
                min(
                    fortune.charge_cap,
                    fortune.charges_at_baseline
                    + math.floor(
                        (luck - fortune.luck_baseline)
                        / max(1, fortune.luck_per_extra_charge)
                    ),
                ),
            )
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
        fighter.hard_control_immunity_until = max(
            0, int(initial.hard_control_immunity_ticks)
        )
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

    def _continuation_state(
        self,
        fighter: FighterState,
        current_tick: int = 0,
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
            hard_control_immunity_ticks=max(
                0, fighter.hard_control_immunity_until - current_tick
            ),
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
        maximum = fighter.max_sp
        armor_factor = {
            "light": 1.0,
            "medium": 0.90,
            "heavy": 0.80,
        }.get(fighter.runtime_armor_style or fighter.snapshot.armor_style, 1.0)
        if fighter.runtime_overloaded:
            armor_factor *= 0.60
        willpower = max(0.0, float(fighter.primary("willpower")))
        will_factor = willpower / (willpower + 30.0)
        fighter.sp_regen_buffer += maximum * (
            0.006 + 0.006 * will_factor
        ) * armor_factor
        restored = min(
            maximum - fighter.stamina,
            int(fighter.sp_regen_buffer),
        )
        if restored > 0:
            fighter.sp_regen_buffer -= restored
            fighter.stamina += restored
        fighter.skill_cooldowns = {key: max(0, value - 1) for key, value in fighter.skill_cooldowns.items()}
        fighter.attack_cooldown = max(0, fighter.attack_cooldown - 1)
        fighter.counter_cooldown = max(0, fighter.counter_cooldown - 1)
        fighter.fortune_cooldown = max(0, fighter.fortune_cooldown - 1)
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
            sudden_healing = (
                self.ruleset.timeout.sudden_death_healing_multiplier
                if state.tick > self.ruleset.timeout.sudden_death_start_tick
                else 1.0
            )
            fighter.hp_regen_buffer += (
                derived.hp_regen_per_tick
                * max(0.0, 1 + self.ability_runtime.modifier(fighter, "healing"))
                * sudden_healing
            )
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
        state: BattleState,
        own: FighterState,
        opponent: FighterState,
        profile: AIProfile,
        opponent_profile: AIProfile,
        phase: str | None,
        rng: random.Random,
    ):
        if phase == "attack_ready":
            return self._intent("resolve_attack")
        if phase is not None:
            return self._intent(phase)
        return choose_action(
            own,
            opponent,
            profile,
            rng,
            self.ATTACK_RANGE,
            opponent_profile,
            state.tick,
            self.ability_runtime,
        )

    def _record_tactic_phase(
        self,
        state: BattleState,
        attacker_profile: AIProfile,
        defender_profile: AIProfile,
    ) -> None:
        phase = phase_for_state(
            state.tick,
            state.attacker.hp_ratio,
            state.defender.hp_ratio,
        )
        if state.tactic_phase == phase.value:
            return
        state.tactic_phase = phase.value
        attacker_resolution = tactic_resolution(
            state.attacker,
            state.defender,
            attacker_profile,
            defender_profile,
            state.tick,
            self.ruleset.strategy,
        )
        defender_resolution = tactic_resolution(
            state.defender,
            state.attacker,
            defender_profile,
            attacker_profile,
            state.tick,
            self.ruleset.strategy,
        )
        state.attacker.tactic_initiative = (
            attacker_resolution.gain.initiative
        )
        state.attacker.tactic_counter_sp_cost = (
            attacker_resolution.gain.counter_sp_cost
        )
        state.defender.tactic_initiative = (
            defender_resolution.gain.initiative
        )
        state.defender.tactic_counter_sp_cost = (
            defender_resolution.gain.counter_sp_cost
        )
        state.events.extend(
            (
                BattleEvent(
                    state.tick,
                    "strategy_trigger",
                    state.attacker.snapshot.user_pk,
                    state.defender.snapshot.user_pk,
                    value=attacker_resolution.matchup,
                    skill_id=phase.value,
                    status_id=attacker_resolution.own_family.value,
                ),
                BattleEvent(
                    state.tick,
                    "strategy_trigger",
                    state.defender.snapshot.user_pk,
                    state.attacker.snapshot.user_pk,
                    value=defender_resolution.matchup,
                    skill_id=phase.value,
                    status_id=defender_resolution.own_family.value,
                ),
            )
        )

    def _intent(self, action: str) -> ActionIntent:
        return ActionIntent(action)

    def _rest(self, state: BattleState, fighter: FighterState) -> None:
        equipment_regen = (
            fighter.snapshot.equipment.stamina_regen
            if fighter.snapshot.equipment else 8
        )
        equipment = fighter.snapshot.equipment
        runtime_equipment_changed = bool(
            fighter.runtime_armor_style
            and (
                equipment is None
                or fighter.runtime_armor_style != equipment.armor_style
                or fighter.runtime_overloaded != equipment.overloaded
            )
        )
        if runtime_equipment_changed:
            equipment_regen = {
                "light": 10,
                "medium": 8,
                "heavy": 6,
            }.get(fighter.runtime_armor_style, equipment_regen)
            if fighter.runtime_overloaded:
                equipment_regen = max(1, equipment_regen // 2)
        resource_rules = self.ruleset.resource
        rest_amount = max(
            resource_rules.stamina_restoration_floor,
            min(
                resource_rules.stamina_restoration_ceiling,
                int(equipment_regen),
            ),
        )
        recovered = min(
            fighter.max_sp - fighter.stamina,
            rest_amount,
        )
        fighter.stamina += max(0, recovered)
        state.events.append(
            BattleEvent(
                state.tick,
                "rest",
                fighter.snapshot.user_pk,
                value=max(0, recovered),
                stamina=fighter.stamina,
            )
        )

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
        weapon_weight = equipment.weapon_weight if equipment else 2.0
        mode_surcharge = 2.0 if fighter.snapshot.weapon_mode in {
            "dual_wield", "two_hand_heavy",
        } else 0.0
        resource_cost = round(
            max(6.0, min(16.0, 6.0 + 0.8 * weapon_weight + mode_surcharge))
        )
        if equipment:
            resource_cost = equipment.attack_stamina
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
        # Catalog windups describe the technique itself.  A very heavy weapon
        # still needs an extra, interruptible commitment window around it.
        # This supplies the missing downside of high-HP/high-power axe builds
        # without silently lowering their successful hit damage.
        attack_power = (
            fighter.current_derived.attack_power
            if fighter.current_derived else fighter.snapshot.stat("atk") * 4
        )
        handling_threshold = 20.0 + 0.25 * fighter.snapshot.level
        heavy_windup = 0
        if attack_power > handling_threshold:
            heavy_windup = min(
                2,
                math.ceil(max(0.0, weapon_weight - 4.0) / 4.0),
            )
        windup += heavy_windup
        if (
            state.environment_id == "strong_wind"
            and fighter.snapshot.is_ranged
            and not self._immune_to_adverse_weather(state, fighter)
        ):
            windup = max(1, math.ceil(windup * 1.08))
        if resource_type == "mp" and state.environment_id == "mana_tide":
            resource_cost = max(1, round(resource_cost * 0.92))
        windup = self._scaled_ticks(fighter, windup)
        fighter.pending_resource_details = {}
        if resource_type == "sp":
            if fighter.stamina < resource_cost:
                return
            fighter.stamina -= resource_cost
        else:
            if not mana_overcast_within_limit(
                fighter.mana - resource_cost,
                fighter.max_mp,
                ruleset=self.ruleset,
            ):
                return
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
                backlash = mana_overcast_backlash(
                    max_hp=fighter.max_hp,
                    max_mp=fighter.max_mp,
                    projected_mana=fighter.mana,
                    reduction=reduction,
                    ruleset=self.ruleset,
                )
                fighter.current_hp = max(0, fighter.current_hp - backlash)
                state.events.append(BattleEvent(
                    state.tick, "mana_backlash", fighter.snapshot.user_pk,
                    fighter.snapshot.user_pk, value=backlash,
                    remaining_hp=fighter.current_hp, skill_id=skill_id,
                    mana=fighter.mana, damage_type="magic",
                    **fighter.pending_resource_details,
                ))
            if definition and definition.ability_type == "spell":
                self._apply_ether_disturbance_backlash(
                    state,
                    fighter,
                    skill_id,
                )
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

    def _apply_ether_disturbance_backlash(
        self,
        state: BattleState,
        fighter: FighterState,
        skill_id: str | None,
    ) -> None:
        if (
            state.environment_id != "ether_disturbance"
        ):
            return

        recent_casts = sum(
            event.kind == "spell_cast_start"
            and event.actor_pk == fighter.snapshot.user_pk
            and 0 <= state.tick - event.tick <= self.ETHER_CAST_WINDOW_TICKS
            for event in state.events
        )
        overcast = fighter.mana < 0
        overloaded = fighter.runtime_overloaded
        if recent_casts == 0 and not overcast and not overloaded:
            return

        # The rift punishes repeated commitments, but it never decides a fight
        # with an opaque lethal roll. Mana Limit mitigates this like ordinary
        # overcast backlash, and the canonical event keeps it visible in reports.
        stress = recent_casts + int(overcast) + int(overloaded)
        ratio_cap = min(
            self.ETHER_BACKLASH_HP_RATIO_CAP,
            self.ruleset.environment.environmental_damage_hp_ratio_cap,
        )
        backlash_ratio = min(
            ratio_cap,
            self.ETHER_BACKLASH_BASE_HP_RATIO
            + self.ETHER_BACKLASH_STRESS_HP_RATIO * stress,
        )
        if backlash_ratio <= 0 or fighter.current_hp <= 1:
            return
        reduction = (
            fighter.current_derived.mana_overcast_reduction
            if fighter.current_derived else 0.0
        )
        reduction = max(0.0, min(0.90, reduction))
        raw_backlash = max(
            1,
            math.ceil(fighter.max_hp * backlash_ratio * (1 - reduction)),
        )
        backlash = min(raw_backlash, fighter.current_hp - 1)
        fighter.current_hp -= backlash
        state.events.append(BattleEvent(
            state.tick,
            "mana_backlash",
            fighter.snapshot.user_pk,
            fighter.snapshot.user_pk,
            value=backlash,
            remaining_hp=fighter.current_hp,
            skill_id=skill_id,
            mana=fighter.mana,
            damage_type="magic",
            status_id="ether_disturbance",
            **fighter.pending_resource_details,
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

    def _movement_step(
        self, fighter: FighterState, *, retreating: bool = False
    ) -> int:
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
            if retreating:
                multiplier *= 0.65
            return max(10, round(base * multiplier))
        multiplier = 1.0
        for key in ("slow", "gravity"):
            if key in fighter.statuses:
                multiplier *= max(0.30, 1 - fighter.statuses[key].magnitude)
        for speed_status in ("haste", "lulwy_possession"):
            if speed_status in fighter.statuses:
                multiplier *= 1 + fighter.statuses[speed_status].magnitude
        multiplier *= max(0.30, 1 - self.ability_runtime.modifier(fighter, "slow"))
        if retreating:
            multiplier *= 0.65
        return max(10, min(80, round(35 * fighter.current_derived.action_speed / 100 * multiplier)))

    def _resolve_movement(
        self,
        state: BattleState,
        attacker_action: str,
        defender_action: str,
    ) -> None:
        attacker_position = state.attacker.position
        defender_position = state.defender.position
        if attacker_action in {"advance", "retreat"}:
            movement_cost = 3 if attacker_action == "retreat" else 2
            state.attacker.stamina = max(
                0, state.attacker.stamina - movement_cost
            )
        if defender_action in {"advance", "retreat"}:
            movement_cost = 3 if defender_action == "retreat" else 2
            state.defender.stamina = max(
                0, state.defender.stamina - movement_cost
            )
        if attacker_action == "advance":
            attacker_position += self._movement_step(state.attacker)
        elif attacker_action == "retreat":
            attacker_position -= self._movement_step(
                state.attacker, retreating=True
            )
        if defender_action == "advance":
            defender_position -= self._movement_step(state.defender)
        elif defender_action == "retreat":
            defender_position += self._movement_step(
                state.defender, retreating=True
            )

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
        actor_fortune_rng: random.Random | None = None,
        target_fortune_rng: random.Random | None = None,
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
        # A firearm technique still cycles through the weapon's deliberate
        # reload rhythm.  Its short windup is the reward for spending SP, not
        # a way to bypass the four-tick gun recovery on every turn.
        if (
            definition
            and actor.snapshot.weapon_type == "firearm"
            and definition.ability_type != "spell"
        ):
            recovery_ticks = max(2, recovery_ticks)
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
        if not is_self and self._uses_direct_evade(definition, has_damage):
            evade_chance = self._evade_chance(
                actor,
                target,
                is_spell=is_spell,
                state=state,
            )
            if is_spell:
                high_accuracy = any(effect.params.get("high_accuracy") for effect in definition.effects)
                if high_accuracy:
                    evade_chance *= 0.55
                if self.ability_runtime.has(actor, "confusion"):
                    evade_chance = min(0.60, evade_chance + 0.25)
            if self.ability_runtime.has(actor, "blind"):
                evade_chance = min(0.70, evade_chance + 0.25)
            if self.ability_runtime.has(actor, "haze"):
                evade_chance = min(0.75, evade_chance + 0.10 * actor.statuses["haze"].stacks)
            missed = rng.random() < evade_chance
            if (
                missed
                and 1.0 - evade_chance
                >= self.ruleset.fortune.severe_miss_hit_chance_threshold
                and actor_fortune_rng is not None
                and self._trigger_fortune(
                    state,
                    actor,
                    target,
                    actor_fortune_rng,
                    "severe_miss",
                )
            ):
                missed = actor_fortune_rng.random() < evade_chance
            if missed:
                state.events.append(BattleEvent(
                    state.tick, "evade", target.snapshot.user_pk,
                    actor.snapshot.user_pk, skill_id=skill_id,
                ))
                return None
        if definition and not has_damage:
            return (0, False, False, 0, skill_id, {})
        if definition:
            result = self.ability_runtime.damage_result(
                actor, target, definition, rng
            )
            result = self._apply_ranged_mastery(actor, result)
            return self._apply_resource_pressure(actor, result)

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
        variance = triangular_variance(
            rng.random(), rng.random(), ruleset=self.ruleset
        )
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
                ruleset=self.ruleset,
            )
        }
        if actor.current_derived and target.current_derived:
            for damage_type, bonus in actor.current_derived.elemental_damage.items():
                if bonus <= 0:
                    continue
                resistance = target.current_derived.resistances.get(damage_type, 0.0)
                elemental = round(
                    bonus * variance
                    * resistance_multiplier(
                        resistance,
                        actor.snapshot.level,
                        ruleset=self.ruleset,
                    )
                    * (1 - target.current_derived.magical_reduction)
                )
                if elemental > 0:
                    breakdown[damage_type] = elemental
        critical_chance = self._critical_chance(actor, target)
        critical_roll = rng.random()
        critical = critical_roll < critical_chance
        if (
            critical
            and critical_chance - critical_roll <= 0.04
            and target_fortune_rng is not None
            and self._trigger_fortune(
                state,
                target,
                actor,
                target_fortune_rng,
                "critical_downgrade",
            )
        ):
            critical = False
        if critical:
            critical_damage = actor.current_derived.critical_damage if actor.current_derived else 1.5
            breakdown = {kind: max(1, round(value * critical_damage)) for kind, value in breakdown.items()}
        passive_block = (target.snapshot.equipment.block_rate if target.snapshot.equipment else 0.0) + self.ability_runtime.modifier(target, "block")
        guarded = target.guarding or rng.random() < min(0.75, passive_block)
        if guarded:
            breakdown = {
                kind: max(
                    1,
                    round(
                        value
                        * self.ruleset.damage.physical_guard_multiplier
                    ),
                )
                for kind, value in breakdown.items()
            }
        result = (
            sum(breakdown.values()), critical, guarded, 0, None, breakdown
        )
        result = self._apply_ranged_mastery(actor, result)
        return self._apply_resource_pressure(actor, result)

    @classmethod
    def _uses_direct_evade(cls, definition, has_damage: bool) -> bool:
        """Whether an action needs the single-target hit/evasion contest.

        Area and ground effects resolve at a location, so their own damage or
        status stage decides the outcome.  A pure hostile status ability also
        uses ``status_chance`` as its sole hit contest; rolling evasion first
        would make the advertised status chance misleading.  Direct damage
        and other point-selected utility keep the ordinary evasion contest.
        """

        if definition is None:
            return True
        if definition.targeting not in cls.DIRECT_EVADE_TARGETING:
            return False
        pure_hostile_status = (
            not has_damage
            and any(
                effect.effect_type == "apply_status"
                and effect.target not in {"self", "ally", "ally_area"}
                for effect in definition.effects
            )
            and all(
                effect.effect_type == "apply_status"
                for effect in definition.effects
            )
        )
        return not pure_hostile_status

    def _apply_ability_secondary(self, state, actor, target, damage_result, rng):
        if not damage_result or len(damage_result) < 5 or not damage_result[4]:
            return
        definition = (
            actor.snapshot.skills.active_definitions.get(damage_result[4])
            if actor.snapshot.skills else None
        )
        if definition:
            self.ability_runtime.apply_secondary(
                state,
                actor,
                target,
                definition,
                damage_result,
                rng,
                apply_damage=self._apply_damage,
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
        if (
            first_result is None
            or first_result[0] <= 0
            or not actor.snapshot.equipment
        ):
            return None
        equipment = actor.snapshot.equipment
        ability_id = first_result[4] if len(first_result) > 4 else None
        definition = (
            actor.snapshot.skills.active_definitions.get(ability_id)
            if ability_id and actor.snapshot.skills else None
        )
        if definition and definition.ability_type == "spell":
            return None
        dual_wield = actor.snapshot.weapon_mode == "dual_wield"
        split_arrow = (
            actor.snapshot.is_ranged
            and self.ability_runtime.has(actor, "split_arrow")
        )
        forced = dual_wield or split_arrow
        chance = equipment.ranged_followup if actor.snapshot.is_ranged else equipment.melee_followup
        chance += self.ability_runtime.modifier(actor, "ranged_followup" if actor.snapshot.is_ranged else "followup")
        if not forced and rng.random() >= chance:
            return None
        if rng.random() < self._evade_chance(actor, target, state=state):
            state.events.append(BattleEvent(state.tick, "evade", target.snapshot.user_pk, actor.snapshot.user_pk))
            return None
        # Dual wield keeps a reliable off-hand segment.  In a single-target
        # duel, Split Arrow's otherwise dead splash shard curls back for a
        # smaller segment; the stance now has a reason to be equipped without
        # pretending it is another full main-hand shot.
        if dual_wield:
            scale = equipment.dual_wield_followup_scale or (
                0.55 if self.ruleset.ruleset_id == "sideview-v10" else 0.25
            )
        elif split_arrow:
            scale = split_arrow_followup_multiplier(
                actor.skill_level("marksmanship"),
                ruleset=self.ruleset,
            )
        else:
            scale = 0.5
        source_breakdown = (
            first_result[5]
            if len(first_result) > 5 else {"physical": first_result[0]}
        )
        breakdown = {
            damage_type: max(1, round(value * scale))
            for damage_type, value in source_breakdown.items()
        }
        # The follow-up is another segment of the already resolved weapon
        # action.  Reusing that action's critical/guard result avoids a hidden
        # second multiplier roll and keeps replay variance bounded.
        critical = bool(first_result[1])
        guarded = bool(first_result[2])
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
        tempo = tempo_multiplier(action_speed, ruleset=self.ruleset)
        initiative_cap = max(0.0, self.ruleset.strategy.initiative_cap)
        tempo *= 1 + max(
            -initiative_cap,
            min(initiative_cap, fighter.tactic_initiative),
        )
        tempo = max(
            self.ruleset.tempo.speed_multiplier_floor,
            min(self.ruleset.tempo.speed_multiplier_ceiling, tempo),
        )
        stamina_ratio = fighter.stamina / max(1, fighter.max_sp)
        if stamina_ratio < 0.10:
            tempo *= 0.80
        elif stamina_ratio < 0.25:
            tempo *= 0.90
        return max(1, math.ceil(max(1, ticks) / max(0.25, tempo)))

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

    @staticmethod
    def _immune_to_adverse_weather(
        state: BattleState,
        fighter: FighterState,
    ) -> bool:
        if state.environment_id not in {"rain", "fog", "strong_wind"}:
            return False
        opponent = (
            state.defender
            if fighter is state.attacker
            else state.attacker
        )
        if opponent.snapshot.combatant_kind == "player":
            return False
        equipment = fighter.snapshot.equipment
        return bool(
            equipment
            and getattr(equipment, "adverse_weather_immunity", False)
        )

    def _evade_chance(
        self,
        actor: FighterState,
        target: FighterState,
        *,
        is_spell: bool = False,
        state: BattleState | None = None,
    ) -> float:
        if actor.current_derived and target.current_derived:
            accuracy = (
                actor.current_derived.accuracy
                * self._accuracy_multiplier(actor, is_spell)
                * max(0.1, 1 + self.ability_runtime.modifier(actor, "accuracy"))
            )
            evasion = target.current_derived.evasion * max(0.1, 1 + self.ability_runtime.modifier(target, "evasion"))
            logit_modifier = 0.0
            if (
                state is not None
                and state.environment_id == "fog"
                and actor.snapshot.is_ranged
                and not self._immune_to_adverse_weather(state, actor)
            ):
                logit_modifier -= 0.15
            evade = 1.0 - hit_chance(
                accuracy,
                evasion,
                is_spell=is_spell,
                combat_level=round(
                    (actor.snapshot.level + target.snapshot.level) / 2
                ),
                logit_modifier=logit_modifier,
                ruleset=self.ruleset,
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
            base = actor.current_derived.critical_rate
        else:
            base = 0.05
        luck = self._combat_luck(actor)
        bonus = max(
            -0.02,
            min(
                self.ruleset.fortune.critical_bonus_cap,
                (luck - self.ruleset.fortune.luck_baseline)
                * self.ruleset.fortune.critical_chance_per_luck,
            ),
        )
        return max(
            0.02,
            min(self.ruleset.damage.critical_chance_cap, base + bonus),
        )

    @staticmethod
    def _combat_luck(fighter: FighterState) -> int:
        if fighter.snapshot.advanced_attributes:
            return fighter.snapshot.advanced_attributes.luck
        return fighter.snapshot.stat("luck")

    def _trigger_fortune(
        self,
        state: BattleState,
        owner: FighterState,
        opponent: FighterState,
        rng,
        reason: str,
    ) -> bool:
        if owner.fortune_charges <= 0 or owner.fortune_cooldown > 0:
            return False
        luck = self._combat_luck(owner)
        opponent_luck = self._combat_luck(opponent)
        gate = max(
            0.10,
            min(
                0.65,
                0.32
                + 0.25 * math.tanh((luck - 100) / 70.0)
                + 0.08 * math.tanh((luck - opponent_luck) / 80.0),
            ),
        )
        if rng.random() >= gate:
            return False
        owner.fortune_charges -= 1
        owner.fortune_cooldown = 12
        state.events.append(
            BattleEvent(
                state.tick,
                "fortune_swing",
                owner.snapshot.user_pk,
                opponent.snapshot.user_pk,
                value=owner.fortune_charges,
                status_id=reason,
            )
        )
        return True

    @staticmethod
    def _apply_ranged_mastery(actor: FighterState, damage_result):
        """Give marksmanship a smooth payoff without a novice double tax."""
        if (
            not damage_result
            or damage_result[0] <= 0
            or not actor.snapshot.is_ranged
        ):
            return damage_result
        mastery = max(0, actor.skill_level("marksmanship"))
        if mastery <= 15:
            multiplier = 0.90 + mastery / 150.0
        else:
            multiplier = 1.00 + 0.012 * (mastery - 15)
        multiplier = max(0.90, min(1.35, multiplier))
        source = (
            dict(damage_result[5])
            if len(damage_result) > 5 else {"physical": damage_result[0]}
        )
        if "physical" not in source:
            return damage_result
        source["physical"] = max(
            1, round(source["physical"] * multiplier)
        )
        values = list(damage_result)
        values[0] = sum(source.values())
        if len(values) > 5:
            values[5] = source
        return tuple(values)

    @staticmethod
    def _apply_resource_pressure(actor: FighterState, damage_result):
        if not damage_result or damage_result[0] <= 0:
            return damage_result
        stamina_ratio = actor.stamina / max(1, actor.max_sp)
        if stamina_ratio <= 0:
            multiplier = 0.75
        elif stamina_ratio < 0.10:
            multiplier = 0.85
        elif stamina_ratio < 0.25:
            multiplier = 0.92
        else:
            multiplier = 1.0
        if actor.mana < 0 and actor.max_mp > 0:
            debt = min(0.50, abs(actor.mana) / actor.max_mp)
            multiplier *= max(0.65, 1.0 - 0.35 * debt)
        if multiplier >= 0.999:
            return damage_result
        source = (
            dict(damage_result[5])
            if len(damage_result) > 5 else {"physical": damage_result[0]}
        )
        breakdown = {
            damage_type: max(0, round(amount * multiplier))
            for damage_type, amount in source.items()
        }
        breakdown = {
            damage_type: amount
            for damage_type, amount in breakdown.items()
            if amount > 0
        }
        total = sum(breakdown.values())
        values = list(damage_result)
        values[0] = total
        if len(values) > 5:
            values[5] = breakdown
        return tuple(values)

    def _apply_damage(
        self,
        state: BattleState,
        actor: FighterState,
        target: FighterState,
        damage_result: tuple[int, bool, bool, int, str | None] | None,
        event_kind: str = "damage",
        allow_on_hit_effects: bool = True,
        *,
        causes_hit_reaction: bool = True,
        credit_damage: bool = True,
        status_id: str | None = None,
        zone_id: str | None = None,
        entity_id: str | None = None,
    ) -> tuple | None:
        if damage_result is None:
            return None
        damage, critical, guarded = damage_result[:3]
        if damage <= 0:
            # A resolved non-damaging ability still carries its ability id in
            # this tuple.  Keep that metadata alive so the shared secondary
            # stage can apply buffs, control, movement and resource effects.
            # Returning ``None`` here made the battle log claim the spell was
            # cast while silently discarding the actual effect.
            return damage_result
        breakdown = (
            dict(damage_result[5])
            if len(damage_result) > 5 and damage_result[5]
            else {"physical": damage}
        )
        if state.environment_id == "rain":
            # The ring shields its wearer from rain's penalty, but rain remains
            # a conductive battlefield and still amplifies lightning attacks.
            if (
                breakdown.get("fire", 0) > 0
                and not self._immune_to_adverse_weather(state, actor)
            ):
                breakdown["fire"] = max(
                    0,
                    round(
                        breakdown["fire"]
                        * self.RAIN_FIRE_DAMAGE_MULTIPLIER
                    ),
                )
            if breakdown.get("lightning", 0) > 0:
                breakdown["lightning"] = max(
                    0,
                    round(
                        breakdown["lightning"]
                        * self.RAIN_LIGHTNING_DAMAGE_MULTIPLIER
                    ),
                )
        damage = sum(breakdown.values())
        sudden_start = self.ruleset.timeout.sudden_death_start_tick
        if state.tick > sudden_start:
            sudden_bonus = min(
                self.ruleset.timeout.sudden_death_damage_growth_cap,
                (state.tick - sudden_start)
                * self.ruleset.timeout.sudden_death_damage_growth_per_tick,
            )
            if sudden_bonus > 0:
                breakdown = self._scale_breakdown(
                    breakdown,
                    1.0 + sudden_bonus,
                )
                damage = sum(breakdown.values())
            if event_kind == "damage":
                elapsed = state.tick - sudden_start
                erosion_ratio = min(
                    self.ruleset.timeout.sudden_death_minimum_hit_ratio_cap,
                    self.ruleset.timeout.sudden_death_minimum_hit_ratio
                    + elapsed
                    * self.ruleset.timeout.sudden_death_minimum_hit_ratio_growth,
                )
                erosion_floor = max(1, round(target.max_hp * erosion_ratio))
                if damage < erosion_floor:
                    breakdown = self._scale_breakdown(
                        breakdown,
                        erosion_floor / max(1, damage),
                        exact_total=erosion_floor,
                    )
                    damage = erosion_floor
        if (
            actor.snapshot.combatant_kind == "player"
            and target.snapshot.combatant_kind == "player"
        ):
            capped = pvp_burst_cap(
                damage,
                target.max_hp,
                ruleset=self.ruleset,
            )
            if capped < damage:
                breakdown = self._scale_breakdown(
                    breakdown,
                    capped / max(1, damage),
                    exact_total=capped,
                )
                damage = capped
        mana_shields = [
            status
            for status in target.statuses.values()
            if float(status.params.get("mana_shield_ratio", 0.0)) > 0
        ]
        mana_shield_ratio = min(
            0.35,
            sum(
                float(status.params.get("mana_shield_ratio", 0.0))
                for status in mana_shields
            ),
        )
        if damage > 0 and target.mana > 0 and mana_shield_ratio > 0:
            absorbed = min(
                target.mana,
                max(0, round(damage * mana_shield_ratio)),
            )
            if absorbed > 0:
                damage -= absorbed
                breakdown = self._scale_breakdown(
                    breakdown,
                    damage / max(1, damage + absorbed),
                    exact_total=damage,
                )
                target.mana -= absorbed
                source_status = mana_shields[0]
                state.events.append(
                    BattleEvent(
                        state.tick,
                        "mana_barrier",
                        target.snapshot.user_pk,
                        actor.snapshot.user_pk,
                        value=absorbed,
                        mana=target.mana,
                        skill_id=str(
                            source_status.params.get("source_ability_id", "")
                        ) or None,
                        status_id=source_status.status_id,
                    )
                )
        if (
            damage >= target.current_hp
            and self.ability_runtime.has(target, "shield_wall")
            and not target.lethal_survival_used
            and (state.random_seed + state.tick + target.snapshot.user_pk) % 4 == 0
        ):
            survived_damage = max(0, target.current_hp - 1)
            breakdown = self._scale_breakdown(
                breakdown,
                survived_damage / max(1, damage),
                exact_total=survived_damage,
            )
            damage = survived_damage
            target.lethal_survival_used = True
        result_values = list(damage_result)
        result_values[0] = damage
        if len(result_values) > 5:
            result_values[5] = breakdown
        damage_result = tuple(result_values)
        target.current_hp = max(0, target.current_hp - damage)
        if credit_damage:
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
                    dict(breakdown)
                ),
                armor_style=(
                    target.runtime_armor_style
                    or target.snapshot.armor_style
                ),
                status_id=status_id,
                zone_id=zone_id,
                entity_id=entity_id,
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
            healing_multiplier = (
                self.ruleset.timeout.sudden_death_healing_multiplier
                if state.tick > self.ruleset.timeout.sudden_death_start_tick
                else 1.0
            )
            recovered = min(
                actor.max_hp - actor.current_hp,
                max(1, round(damage * life_steal * healing_multiplier)),
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

        if causes_hit_reaction and target.attack_pending and target.windup_ticks > 0:
            pending_skill_id = target.pending_skill_id
            pending_definition = (
                target.snapshot.skills.active_definitions.get(pending_skill_id)
                if pending_skill_id and target.snapshot.skills
                else None
            )
            concentration_holds = False
            if pending_definition and pending_definition.ability_type == "spell":
                focus = (
                    target.primary("magic")
                    + target.primary("willpower")
                    + target.skill_level(pending_definition.unlock_skill_id)
                )
                threshold = spell_interrupt_damage_threshold(
                    max_hp=target.max_hp,
                    focus=focus,
                    guarded=guarded,
                    ruleset=self.ruleset,
                )
                concentration_holds = damage <= threshold
            if concentration_holds:
                state.events.append(
                    BattleEvent(
                        state.tick,
                        "spell_concentration",
                        target.snapshot.user_pk,
                        actor.snapshot.user_pk,
                        value=damage,
                        remaining_hp=target.current_hp,
                        skill_id=pending_skill_id,
                    )
                )
            else:
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

        if causes_hit_reaction:
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
        return damage_result

    @staticmethod
    def _scale_breakdown(
        breakdown: dict[str, int],
        multiplier: float,
        *,
        exact_total: int | None = None,
    ) -> dict[str, int]:
        if not breakdown:
            return {}
        scaled = {
            damage_type: max(0, round(amount * multiplier))
            for damage_type, amount in breakdown.items()
        }
        if exact_total is not None:
            difference = exact_total - sum(scaled.values())
            anchor = max(breakdown, key=breakdown.get)
            scaled[anchor] = max(0, scaled.get(anchor, 0) + difference)
        return {
            damage_type: amount
            for damage_type, amount in scaled.items()
            if amount > 0
        }

    def _knockback_distance(
        self,
        damage_result: tuple[int, bool, bool, int, str | None] | None,
    ) -> int:
        if damage_result is None or damage_result[0] <= 0:
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
        base_counter_cost = max(
            0,
            round(
                reactor.max_sp
                * max(0.0, self.ruleset.strategy.counter_stamina_ratio)
            ),
        )
        counter_cap = max(
            0.0, self.ruleset.strategy.counter_sp_cost_cap
        )
        counter_cost = round(
            base_counter_cost
            * (
                1
                + max(
                    -counter_cap,
                    min(counter_cap, reactor.tactic_counter_sp_cost),
                )
            )
        )
        if reactor.stamina < counter_cost:
            return
        reactor.stamina -= counter_cost
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
        state.events.append(BattleEvent(
            state.tick,
            "counter_trigger",
            reactor.snapshot.user_pk,
            target.snapshot.user_pk,
            value=reactor.counter_cooldown,
            stamina=reactor.stamina,
            status_id="hold_the_line" if hold_line else "never_retreat",
        ))
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
        # Secondary movement (blink, teleport, charge effects) has already
        # resolved by this stage.  Apply any real knockback from the fighter's
        # current position so a zero-damage utility cast cannot rewind that
        # movement to the pre-cast coordinates.
        new_attacker_position = state.attacker.position - attacker_knockback
        new_defender_position = state.defender.position + defender_knockback
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
        attacker_score = self._timeout_score(
            state,
            state.attacker,
            state.defender,
        )
        defender_score = self._timeout_score(
            state,
            state.defender,
            state.attacker,
        )
        if abs(attacker_score - defender_score) >= 0.01:
            return state.attacker if attacker_score > defender_score else state.defender
        luck_delta = (
            self._combat_luck(state.attacker)
            - self._combat_luck(state.defender)
        )
        attacker_chance = 0.50 + 0.05 * math.tanh(luck_delta / 80.0)
        return (
            state.attacker
            if rng.random() < attacker_chance else state.defender
        )

    @staticmethod
    def _timeout_score(
        state: BattleState,
        fighter: FighterState,
        opponent: FighterState,
    ) -> float:
        hp_ratio = fighter.current_hp / max(1, fighter.max_hp)
        pressure = min(1.0, fighter.damage_dealt / max(1, opponent.max_hp))
        stamina_ratio = fighter.stamina / max(1, fighter.max_sp)
        mana_ratio = (
            max(0.0, fighter.mana) / max(1, fighter.max_mp)
            if fighter.max_mp > 0 else stamina_ratio
        )
        control_events = sum(
            event.kind == "status_apply"
            and event.actor_pk == fighter.snapshot.user_pk
            for event in state.events
        )
        opponent_control_events = sum(
            event.kind == "status_apply"
            and event.actor_pk == opponent.snapshot.user_pk
            for event in state.events
        )
        control_score = control_events / max(
            1,
            control_events + opponent_control_events,
        )
        return (
            0.60 * hp_ratio
            + 0.25 * pressure
            + 0.10 * ((stamina_ratio + mana_ratio) / 2.0)
            + 0.05 * control_score
        )

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
            engine_version=self.ruleset.ruleset_id,
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
            attacker_final_state=self._continuation_state(
                state.attacker, state.tick
            ),
            defender_final_state=self._continuation_state(
                state.defender, state.tick
            ),
            ruleset_id=state.ruleset_id,
            environment_id=state.environment_id,
        )
