import math
from dataclasses import replace

try:
    from ..models.combat import AIProfile, ActionIntent, FighterState
    from .ability_runtime import AbilityRuntime
    from .balance_rules import (
        mana_overcast_backlash,
        mana_overcast_within_limit,
        ranged_preferred_range_fraction,
        spell_preferred_range_fraction,
    )
    from .tactic_rules import (
        BuildSignals,
        CombatPhase,
        TacticFamily,
        TacticPlan,
        family_for_legacy_strategy,
        phase_for_state,
        resolve_plan_phase,
    )
except ImportError:
    from models.combat import AIProfile, ActionIntent, FighterState
    from services.ability_runtime import AbilityRuntime
    from services.balance_rules import (
        mana_overcast_backlash,
        mana_overcast_within_limit,
        ranged_preferred_range_fraction,
        spell_preferred_range_fraction,
    )
    from services.tactic_rules import (
        BuildSignals,
        CombatPhase,
        TacticFamily,
        TacticPlan,
        family_for_legacy_strategy,
        phase_for_state,
        resolve_plan_phase,
    )


DEFAULT_PROFILE = AIProfile()
ABILITY_RUNTIME = AbilityRuntime()

# Six deliberately close behavior bands replace eighteen independently tuned
# probability bundles.  The Chinese strategy names remain compatible, while
# actual counterplay is resolved by the phased tactic rules below.
FAMILY_PROFILES = {
    TacticFamily.PRESSURE: AIProfile(0.84, 0.12, 0.92, 78, 0.06, 0.72),
    TacticFamily.COUNTER: AIProfile(0.66, 0.40, 0.76, 88, 0.18, 0.40),
    TacticFamily.SKIRMISH: AIProfile(0.72, 0.18, 0.82, 118, 0.36, 0.46),
    TacticFamily.CONTROL: AIProfile(0.70, 0.30, 0.76, 105, 0.28, 0.48),
    TacticFamily.SUSTAIN: AIProfile(0.64, 0.36, 0.74, 98, 0.22, 0.32),
    TacticFamily.GAMBIT: AIProfile(0.78, 0.16, 0.86, 86, 0.14, 0.82),
}


def _named_profile(strategy: str) -> AIProfile:
    family = family_for_legacy_strategy(strategy)
    base = FAMILY_PROFILES[family]
    plan = TacticPlan.from_legacy(strategy)
    return replace(
        base,
        strategy_name=strategy,
        tactic_plan=(
            plan.opening.value,
            plan.midgame.value,
            plan.endgame.value,
        ),
    )


# Values remain data, not subclasses: adding or migrating a preset does not
# require modifying the simulation loop.
STRATEGY_PROFILES = {
    strategy: _named_profile(strategy)
    for strategy in (
        "稳扎稳打", "全力猛攻", "防守反击", "游走消耗", "先手压制",
        "诱敌深入", "持久消耗", "奇袭爆发", "以守为攻", "速度拉扯",
        "幸运赌局", "破防强攻", "闪避拖延", "控制节奏", "血量压制",
        "精准打击", "扰乱节奏", "背水一战",
    )
}


def profile_for_strategy(strategy: str, custom_profile: dict | None = None) -> AIProfile:
    profile = STRATEGY_PROFILES.get(strategy)
    if profile is not None:
        return profile
    stats = tuple((custom_profile or {}).get("primary_stats", ()))
    if not stats:
        return DEFAULT_PROFILE
    aggression = 0.70 + (0.13 if "strength" in stats or "perception" in stats else 0.0)
    guard = 0.20 + (0.22 if "constitution" in stats else 0.0)
    chase = 0.80 + (0.13 if "dexterity" in stats else 0.0)
    preferred_range = 125 if "dexterity" in stats else 90
    retreat = 0.42 if "dexterity" in stats else 0.15
    low_hp_risk = 0.78 if "magic" in stats else (0.32 if "willpower" in stats else 0.50)
    if "dexterity" in stats:
        family = TacticFamily.SKIRMISH
    elif "constitution" in stats or "willpower" in stats:
        family = TacticFamily.SUSTAIN
    elif "magic" in stats:
        family = TacticFamily.CONTROL
    else:
        family = TacticFamily.PRESSURE
    return AIProfile(
        min(0.95, aggression),
        min(0.60, guard),
        min(1.0, chase),
        preferred_range,
        retreat,
        low_hp_risk,
        strategy,
        (family.value, family.value, family.value),
    )


def _ability_score(
    definition,
    own,
    opponent,
    distance,
    slot_index,
    rng,
    *,
    tactic_family: TacticFamily = TacticFamily.SUSTAIN,
    utility_modifier: float = 0.0,
    ability_runtime: AbilityRuntime = ABILITY_RUNTIME,
    current_tick: int = 0,
):
    if not ability_runtime.compatible(definition, own):
        return None
    if own.skill_cooldowns.get(definition.ability_id, 0) > 0:
        return None
    cost = ability_runtime.effective_cost(definition, own)
    overcast_hp_cost = 0
    if definition.resource_type == "sp" and own.stamina < cost:
        return None
    if definition.resource_type == "mp" and own.mana < cost:
        projected_mana = own.mana - cost
        if not mana_overcast_within_limit(
            projected_mana,
            own.max_mp,
            ruleset=ability_runtime.ruleset,
        ):
            return None
        reduction = (
            own.current_derived.mana_overcast_reduction
            if own.current_derived else 0.0
        )
        overcast_hp_cost = mana_overcast_backlash(
            max_hp=own.max_hp,
            max_mp=own.max_mp,
            projected_mana=projected_mana,
            reduction=reduction,
            ruleset=ability_runtime.ruleset,
        )
        can_finish = "damage" in definition.ai_tags and opponent.current_hp <= max(10, own.primary("magic") * 2)
        emergency_heal = (
            "heal" in definition.ai_tags
            and own.hp_ratio < 0.25
            and own.current_hp > overcast_hp_cost
        )
        safe_tactical_overcast = (
            own.hp_ratio >= 0.55
            and overcast_hp_cost
            <= max(1, round(own.current_hp * 0.15))
        )
        if not (can_finish or emergency_heal or safe_tactical_overcast):
            return None
    self_target = definition.targeting in {"self", "ally", "ally_area"}
    if not self_target and distance > definition.cast_range:
        return None
    own_negative = any(not status.beneficial for status in own.statuses.values())
    if (
        "heal" in definition.ai_tags
        and own.current_hp >= own.max_hp
        and not ("cleanse" in definition.ai_tags and own_negative)
    ):
        return None
    if "cleanse" in definition.ai_tags and not any(not s.beneficial for s in own.statuses.values()):
        return None
    if "dispel" in definition.ai_tags and not any(s.beneficial for s in opponent.statuses.values()):
        return None
    for effect in definition.effects:
        if effect.params.get("requires_negative_status") and not any(not s.beneficial for s in own.statuses.values()):
            return None
        if effect.effect_type == "summon":
            entity_status = str(
                effect.params.get(
                    "aura_status",
                    effect.params.get("entity_id", definition.ability_id),
                )
            )
            if entity_status in own.statuses:
                return None
        if effect.effect_type in {"apply_status", "activate_stance"}:
            recipient = own if effect.target in {"self", "ally", "ally_area"} or effect.effect_type == "activate_stance" else opponent
            if effect.status_id and effect.status_id in recipient.statuses:
                return None
    # Ability choice is an action-economy decision, not a tag lottery.  A
    # control spell that merely refreshes an existing debuff should lose to a
    # real hit, while a defensive spell becomes attractive when its recipient
    # is actually under pressure.  The numbers are deliberately small and
    # bounded; they rank the four configured slots without multiplying damage.
    score = 18.0
    tags = set(definition.ai_tags)
    damage_effects = tuple(
        effect for effect in definition.effects
        if effect.effect_type in {"physical_damage", "magic_damage"}
    )
    has_damage_effect = bool(damage_effects)
    if "damage" in tags or has_damage_effect:
        damage_multiplier = sum(
            max(0.0, float(effect.value))
            for effect in damage_effects
        )
        score += 30.0 + 24.0 * min(2.2, damage_multiplier)
        if opponent.hp_ratio < 0.25:
            score += 28
        elif opponent.hp_ratio < 0.45:
            score += 10
    if "heal" in tags:
        score += 18 + (82 if own.hp_ratio < 0.30 else 34 * (1 - own.hp_ratio))
    if "cleanse" in tags and any(s.status_id in {"stun", "paralysis", "silence", "bind"} for s in own.statuses.values()):
        score += 85
    elif "cleanse" in tags and own_negative:
        score += 32
    if "control" in tags:
        hard_control = any(
            effect.status_id in {"stun", "paralysis", "bind", "silence"}
            for effect in definition.effects
            if effect.effect_type == "apply_status" and effect.target == "enemy"
        )
        target_hard_control = any(
            status.status_id in {"stun", "paralysis", "bind", "silence"}
            for status in opponent.statuses.values()
        )
        if target_hard_control:
            score -= 28
        elif hard_control and opponent.hard_control_immunity_until > current_tick:
            score -= 20
        else:
            score += 24 if hard_control else 16
    if "defense" in tags:
        score += 18 + (48 if own.hp_ratio < 0.40 else 16 * (1 - own.hp_ratio))
    if tags & {"buff", "stance", "summon", "zone"}:
        score += 18
        if any(
            effect.effect_type in {"activate_stance", "summon", "create_zone"}
            for effect in definition.effects
        ):
            score += 10
    if "mobility" in tags:
        score += 18
        if opponent.attack_pending:
            score += 35
        if own.snapshot.is_ranged and distance < max(120, definition.cast_range):
            score += 18
    if "resource" in tags:
        missing_mana = max(0, own.max_mp - own.mana)
        score += 12 + 24 * missing_mana / max(1, own.max_mp)
    # Family identity changes *which action looks useful*.  It never multiplies
    # the damage result, so a nominal counter still has to execute correctly.
    if tactic_family is TacticFamily.PRESSURE and "damage" in tags:
        score += 8
    elif tactic_family is TacticFamily.COUNTER and tags & {"defense", "buff"}:
        score += 8
    elif tactic_family is TacticFamily.SKIRMISH and tags & {"zone", "debuff"}:
        score += 7
    elif tactic_family is TacticFamily.CONTROL and "control" in tags:
        score += 10
    elif tactic_family is TacticFamily.SUSTAIN and tags & {"heal", "defense"}:
        score += 9
    elif tactic_family is TacticFamily.GAMBIT and "damage" in tags:
        score += 4 + (8 if opponent.hp_ratio < 0.40 else 0)
    if definition.ability_type == "stance" and own.stance_id:
        if all(
            effect.status_id != own.stance_id
            for effect in definition.effects
            if effect.effect_type == "activate_stance"
        ):
            score -= 9
    cost_penalty = 0.20 * cost + 1.25 * definition.windup_ticks
    cost_penalty += 0.75 * definition.recovery_ticks
    if overcast_hp_cost:
        cost_penalty += 45.0 * overcast_hp_cost / max(1, own.current_hp)
    score -= cost_penalty
    score *= 1.0 + max(-0.12, min(0.12, utility_modifier))
    score -= slot_index * 0.01
    score += rng.random() * 0.001
    return score


def _profile_plan(profile: AIProfile) -> TacticPlan:
    try:
        return TacticPlan(*profile.tactic_plan)
    except (TypeError, ValueError):
        return TacticPlan.from_legacy(profile.strategy_name)


def _build_signals(fighter: FighterState) -> BuildSignals:
    derived = fighter.current_derived
    attack = float(
        derived.attack_power if derived else fighter.snapshot.stat("atk") * 4
    )
    defense = float(
        derived.defense if derived else fighter.snapshot.stat("defense")
    )
    speed = float(
        derived.action_speed if derived else fighter.snapshot.stat("speed")
    )
    critical = float(derived.critical_rate if derived else 0.05)
    block = float(
        fighter.snapshot.equipment.block_rate
        if fighter.snapshot.equipment else 0.0
    )
    definitions = (
        fighter.snapshot.skills.active_definitions.values()
        if fighter.snapshot.skills else ()
    )
    definitions = tuple(definitions)
    control_count = sum(
        "control" in definition.ai_tags for definition in definitions
    )
    disruption = control_count / max(1, len(definitions))
    advanced_luck = (
        fighter.snapshot.advanced_attributes.luck
        if fighter.snapshot.advanced_attributes else fighter.snapshot.stat("luck")
    )
    return BuildSignals(
        burst=min(1.0, attack / max(1.0, attack + fighter.max_hp / 8.0)),
        retaliation=min(1.0, block * 1.5 + defense / (defense + 120.0)),
        mobility=min(1.0, max(0.0, (speed - 50.0) / 130.0)),
        disruption=min(1.0, disruption),
        endurance=min(
            1.0,
            0.55 * defense / (defense + 100.0)
            + 0.45 * fighter.max_hp / (fighter.max_hp + 500.0),
        ),
        variance=min(
            1.0,
            critical * 1.5 + max(0.0, advanced_luck - 60) / 240.0,
        ),
    )


def tactic_resolution(
    own: FighterState,
    opponent: FighterState,
    profile: AIProfile,
    opponent_profile: AIProfile,
    tick: int,
    strategy_rules=None,
):
    phase = phase_for_state(tick, own.hp_ratio, opponent.hp_ratio)
    limits = strategy_rules
    return resolve_plan_phase(
        _profile_plan(profile),
        _profile_plan(opponent_profile),
        phase,
        _build_signals(own),
        _build_signals(opponent),
        utility_cap=(
            limits.utility_cap if limits is not None else 0.12
        ),
        guard_logit_cap=(
            limits.guard_logit_cap if limits is not None else 0.32
        ),
        initiative_cap=(
            limits.initiative_cap if limits is not None else 0.10
        ),
        counter_sp_cost_cap=(
            limits.counter_sp_cost_cap if limits is not None else 0.18
        ),
    )


def _adjust_probability(probability: float, logit_delta: float) -> float:
    probability = max(0.001, min(0.999, probability))
    odds_log = math.log(probability / (1.0 - probability))
    adjusted = 1.0 / (1.0 + math.exp(-(odds_log + logit_delta)))
    return max(0.01, min(0.95, adjusted))


def choose_action(
    own: FighterState,
    opponent: FighterState,
    profile: AIProfile,
    rng,
    attack_range: int,
    opponent_profile: AIProfile | None = None,
    tick: int = 0,
    ability_runtime: AbilityRuntime = ABILITY_RUNTIME,
) -> ActionIntent:
    distance = abs(opponent.position - own.position)
    equipment = own.snapshot.equipment
    weapon_attack_range = equipment.attack_range if equipment else attack_range
    engagement_range = weapon_attack_range
    preferred_range = profile.preferred_range
    if own.snapshot.is_ranged:
        range_fraction = ranged_preferred_range_fraction(
            own.skill_level("marksmanship"),
            ruleset=ability_runtime.ruleset,
        )
        preferred_range = max(
            preferred_range,
            round(weapon_attack_range * range_fraction),
        )
    spell_ranges: list[tuple[int, int]] = []
    if own.snapshot.skills:
        for ability_id in own.snapshot.skills.active_skill_ids:
            definition = own.snapshot.skills.active_definitions.get(ability_id)
            if (
                definition
                and definition.ability_type == "spell"
                and definition.cast_range > 0
                and definition.targeting not in {"self", "ally", "ally_area"}
            ):
                spell_ranges.append(
                    (
                        definition.cast_range,
                        own.skill_level(definition.unlock_skill_id),
                    )
                )
    if spell_ranges:
        spell_attack_range, school_mastery = max(spell_ranges)
        engagement_range = max(engagement_range, spell_attack_range)
        spell_fraction = spell_preferred_range_fraction(
            school_mastery,
            ruleset=ability_runtime.ruleset,
        )
        preferred_range = max(
            preferred_range,
            round(spell_attack_range * spell_fraction),
        )
    # A skirmish plan should not make a sword user backpedal as often as a bow
    # user.  The plan supplies intent; the build decides whether it can execute
    # that intent as sustained kiting.
    uses_distance = own.snapshot.is_ranged or bool(spell_ranges)
    retreat_tendency = profile.retreat_tendency * (
        1.0 if uses_distance else 0.55
    )
    if ability_runtime.action_blocked(own):
        return ActionIntent("stunned")
    if own.stamina < 5 and not own.snapshot.skills:
        return ActionIntent("rest")
    opponent_profile = opponent_profile or DEFAULT_PROFILE
    resolution = tactic_resolution(
        own,
        opponent,
        profile,
        opponent_profile,
        tick,
        ability_runtime.ruleset.strategy,
    )
    family = resolution.own_family
    gain = resolution.gain
    low_hp = own.hp_ratio <= 0.30
    aggression = max(
        0.05,
        min(0.97, profile.aggression + gain.utility),
    )
    guard_tendency = _adjust_probability(
        profile.guard_tendency,
        gain.guard_logit,
    )
    if low_hp:
        aggression += (1.0 - aggression) * profile.low_hp_risk

    if own.attack_cooldown == 0 and own.snapshot.skills:
        candidates = []
        for index, ability_id in enumerate(own.snapshot.skills.active_skill_ids):
            definition = own.snapshot.skills.active_definitions.get(ability_id)
            if definition:
                score = _ability_score(
                    definition,
                    own,
                    opponent,
                    distance,
                    index,
                    rng,
                    tactic_family=family,
                    utility_modifier=gain.utility,
                    ability_runtime=ability_runtime,
                    current_tick=tick,
                )
                if score is not None:
                    candidates.append((score, -index, ability_id))
        if candidates:
            _, _, ability_id = max(candidates)
            return ActionIntent("use_skill", ability_id)

    weapon_weight = equipment.weapon_weight if equipment else 2.0
    mode_surcharge = 2.0 if own.snapshot.weapon_mode in {
        "dual_wield", "two_hand_heavy",
    } else 0.0
    attack_cost = (
        equipment.attack_stamina
        if equipment
        else round(
            max(
                6.0,
                min(16.0, 6.0 + 0.8 * weapon_weight + mode_surcharge),
            )
        )
    )
    if own.stamina < max(5, attack_cost):
        return ActionIntent("rest")

    if ability_runtime.has(own, "bind") or ability_runtime.has(own, "never_retreat"):
        if distance <= weapon_attack_range and own.attack_cooldown == 0:
            return ActionIntent("basic_attack", "basic_attack")
        return ActionIntent("guard" if own.stamina >= 5 else "rest")
    if distance > engagement_range:
        if distance < preferred_range and rng.random() < retreat_tendency:
            return ActionIntent("retreat")
        if rng.random() < profile.chase_tendency or distance > preferred_range:
            return ActionIntent("advance")
        return ActionIntent("guard")
    if opponent.attack_pending:
        if rng.random() < retreat_tendency * 0.55 + (1.0 - guard_tendency) * 0.10:
            return ActionIntent("retreat")
        if rng.random() < guard_tendency + 0.25:
            return ActionIntent("guard")
    if own.attack_cooldown > 0:
        if distance < preferred_range - 12 and rng.random() < 0.55 + retreat_tendency * 0.40:
            return ActionIntent("retreat")
        if distance > preferred_range + 12 and rng.random() < profile.chase_tendency:
            return ActionIntent("advance")
        if rng.random() < guard_tendency:
            return ActionIntent("guard")
        return ActionIntent("retreat" if distance <= preferred_range else "advance")
    if distance > weapon_attack_range:
        if distance < preferred_range - 15 and rng.random() < retreat_tendency:
            return ActionIntent("retreat")
        if distance > preferred_range + 15 and rng.random() < profile.chase_tendency:
            return ActionIntent("advance")
        return ActionIntent("guard")
    if distance < preferred_range - 15 and rng.random() < retreat_tendency:
        return ActionIntent("retreat")
    if rng.random() < aggression and own.stamina >= attack_cost:
        return ActionIntent("basic_attack", "basic_attack")
    if rng.random() < guard_tendency:
        return ActionIntent("guard")
    return ActionIntent("retreat" if distance < preferred_range else "advance")
