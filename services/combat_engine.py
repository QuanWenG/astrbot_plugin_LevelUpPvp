import random

try:
    from ..models.combat import (
        AIProfile,
        ActionIntent,
        BattleEvent,
        BattleState,
        FighterSnapshot,
        FighterState,
        SimulationResult,
    )
    from .combat_ai import choose_action
except ImportError:
    from models.combat import (
        AIProfile,
        ActionIntent,
        BattleEvent,
        BattleState,
        FighterSnapshot,
        FighterState,
        SimulationResult,
    )
    from services.combat_ai import choose_action


class SideviewCombatEngine:
    ENGINE_VERSION = "sideview-v2"
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

    def simulate(
        self,
        attacker: FighterSnapshot,
        defender: FighterSnapshot,
        attacker_profile: AIProfile,
        defender_profile: AIProfile,
        random_seed: int,
    ) -> SimulationResult:
        rng = random.Random(random_seed)
        state = BattleState(
            tick=0,
            attacker=FighterState(attacker, attacker.max_hp, self.ATTACKER_START),
            defender=FighterState(defender, defender.max_hp, self.DEFENDER_START),
            events=[],
            random_seed=random_seed,
        )

        for tick in range(1, self.MAX_TICKS + 1):
            state.tick = tick
            attacker_phase = self._prepare_tick(state.attacker)
            defender_phase = self._prepare_tick(state.defender)
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
            self._begin_attack(state, state.attacker, attacker_intent.action)
            self._begin_attack(state, state.defender, defender_intent.action)
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
            pre_attacker_hp = state.attacker.current_hp
            pre_defender_hp = state.defender.current_hp
            self._apply_damage(state, state.attacker, state.defender, attacker_damage)
            self._apply_damage(state, state.defender, state.attacker, defender_damage)
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
        state.finish_reason = "timeout_hp_ratio"
        state.events.append(
            BattleEvent(
                state.tick,
                "timeout",
                winner.snapshot.user_pk,
                loser.snapshot.user_pk,
            )
        )
        return self._result(state, winner, loser)

    def _prepare_tick(self, fighter: FighterState) -> str | None:
        fighter.attack_cooldown = max(0, fighter.attack_cooldown - 1)
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
    ) -> None:
        if action != "basic_attack":
            return
        fighter.attack_pending = True
        fighter.windup_ticks = self.ATTACK_WINDUP_TICKS
        state.events.append(
            BattleEvent(
                state.tick,
                "attack_windup",
                fighter.snapshot.user_pk,
                value=self.ATTACK_WINDUP_TICKS,
            )
        )

    def _resolve_guards(
        self,
        state: BattleState,
        attacker_action: str,
        defender_action: str,
    ) -> None:
        state.attacker.guarding = attacker_action == "guard"
        state.defender.guarding = defender_action == "guard"
        if state.attacker.guarding:
            state.events.append(
                BattleEvent(state.tick, "guard", state.attacker.snapshot.user_pk)
            )
        if state.defender.guarding:
            state.events.append(
                BattleEvent(state.tick, "guard", state.defender.snapshot.user_pk)
            )

    def _movement_step(self, fighter: FighterState) -> int:
        return max(25, min(80, 20 + fighter.snapshot.stat("speed") * 2))

    def _resolve_movement(
        self,
        state: BattleState,
        attacker_action: str,
        defender_action: str,
    ) -> None:
        attacker_position = state.attacker.position
        defender_position = state.defender.position
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
            and distance > self.ATTACK_RANGE
        ):
            attacker_position += min(
                self.ATTACK_LUNGE_DISTANCE,
                distance - self.ATTACK_RANGE,
            )
        distance = defender_position - attacker_position
        if (
            defender_action == "resolve_attack"
            and state.defender.attack_pending
            and distance > self.ATTACK_RANGE
        ):
            defender_position -= min(
                self.ATTACK_LUNGE_DISTANCE,
                distance - self.ATTACK_RANGE,
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
                )
            )

    def _attack_damage(
        self,
        state: BattleState,
        actor: FighterState,
        target: FighterState,
        action: str,
        rng: random.Random,
    ) -> tuple[int, bool, bool] | None:
        if action != "resolve_attack" or not actor.attack_pending:
            return None
        actor.attack_pending = False
        actor.windup_ticks = 0
        actor.recovery_ticks = self.ATTACK_RECOVERY_TICKS
        actor.attack_cooldown = self._attack_cooldown(actor)
        state.events.append(
            BattleEvent(
                state.tick,
                "attack",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
            )
        )
        state.events.append(
            BattleEvent(
                state.tick,
                "recovery",
                actor.snapshot.user_pk,
                value=self.ATTACK_RECOVERY_TICKS,
            )
        )
        if abs(actor.position - target.position) > self.ATTACK_RANGE:
            state.events.append(
                BattleEvent(
                    state.tick,
                    "whiff",
                    actor.snapshot.user_pk,
                    target.snapshot.user_pk,
                )
            )
            return None

        evade_chance = self._evade_chance(actor, target)
        if rng.random() < evade_chance:
            state.events.append(
                BattleEvent(
                    state.tick,
                    "evade",
                    target.snapshot.user_pk,
                    actor.snapshot.user_pk,
                )
            )
            return None

        base = max(
            1.0,
            actor.snapshot.stat("atk") * 4.0
            - target.snapshot.stat("defense") * 0.8,
        )
        damage = max(1, round(base * rng.uniform(0.90, 1.10)))
        critical_chance = self._critical_chance(actor, target)
        critical = rng.random() < critical_chance
        if critical:
            damage = max(1, round(damage * 1.5))
        guarded = target.guarding
        if guarded:
            damage = max(1, round(damage * self.GUARD_DAMAGE_MULTIPLIER))
        return damage, critical, guarded

    def _attack_cooldown(self, fighter: FighterState) -> int:
        return max(4, 6 - min(2, fighter.snapshot.stat("speed") // 25))

    def _evade_chance(self, actor: FighterState, target: FighterState) -> float:
        speed_diff = actor.snapshot.stat("speed") - target.snapshot.stat("speed")
        luck_diff = actor.snapshot.stat("luck") - target.snapshot.stat("luck")
        return max(0.0, min(0.20, -speed_diff * 0.005 - luck_diff * 0.002))

    def _critical_chance(self, actor: FighterState, target: FighterState) -> float:
        luck_diff = actor.snapshot.stat("luck") - target.snapshot.stat("luck")
        return max(0.05, min(0.25, 0.05 + luck_diff * 0.005))

    def _apply_damage(
        self,
        state: BattleState,
        actor: FighterState,
        target: FighterState,
        damage_result: tuple[int, bool, bool] | None,
    ) -> None:
        if damage_result is None:
            return
        damage, critical, guarded = damage_result
        target.current_hp = max(0, target.current_hp - damage)
        actor.damage_dealt += damage
        state.events.append(
            BattleEvent(
                state.tick,
                "damage",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                value=damage,
                remaining_hp=target.current_hp,
                critical=critical,
                guarded=guarded,
            )
        )

        if target.attack_pending and target.windup_ticks > 0:
            target.attack_pending = False
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
        damage_result: tuple[int, bool, bool] | None,
    ) -> int:
        if damage_result is None:
            return 0
        damage, critical, guarded = damage_result
        distance = self.KNOCKBACK_BASE + damage // 2
        if critical:
            distance += 10
        if guarded:
            distance = max(10, round(distance * 0.65))
        return min(self.KNOCKBACK_MAX, distance)

    def _apply_knockbacks(
        self,
        state: BattleState,
        attacker_position: int,
        defender_position: int,
        attacker_damage: tuple[int, bool, bool] | None,
        defender_damage: tuple[int, bool, bool] | None,
    ) -> None:
        attacker_knockback = self._knockback_distance(defender_damage)
        defender_knockback = self._knockback_distance(attacker_damage)
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
        attacker_ratio = pre_attacker_hp / state.attacker.snapshot.max_hp
        defender_ratio = pre_defender_hp / state.defender.snapshot.max_hp
        if attacker_ratio != defender_ratio:
            return state.attacker if attacker_ratio > defender_ratio else state.defender
        return self._resolve_equal_score(state, rng)

    def _resolve_timeout(
        self,
        state: BattleState,
        rng: random.Random,
    ) -> FighterState:
        if state.attacker.hp_ratio != state.defender.hp_ratio:
            return (
                state.attacker
                if state.attacker.hp_ratio > state.defender.hp_ratio
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
        attacker_speed = state.attacker.snapshot.stat("speed")
        defender_speed = state.defender.snapshot.stat("speed")
        if attacker_speed != defender_speed:
            return state.attacker if attacker_speed > defender_speed else state.defender
        luck_diff = (
            state.attacker.snapshot.stat("luck")
            - state.defender.snapshot.stat("luck")
        )
        attacker_chance = max(0.20, min(0.80, 0.50 + luck_diff * 0.01))
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
        )
