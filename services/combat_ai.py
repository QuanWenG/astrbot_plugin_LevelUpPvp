import math

try:
    from ..models.combat import AIProfile, ActionIntent, FighterState
    from .ability_runtime import AbilityRuntime
except ImportError:
    from models.combat import AIProfile, ActionIntent, FighterState
    from services.ability_runtime import AbilityRuntime


DEFAULT_PROFILE = AIProfile()
ABILITY_RUNTIME = AbilityRuntime()

# Values are intentionally data, not subclasses: adding a behavior preset should
# not require modifying the simulation loop.
STRATEGY_PROFILES = {
    "稳扎稳打": AIProfile(0.68, 0.28, 0.82, 90, 0.12, 0.45),
    "全力猛攻": AIProfile(0.95, 0.05, 1.00, 70, 0.02, 0.90),
    "防守反击": AIProfile(0.62, 0.55, 0.72, 85, 0.16, 0.35),
    "游走消耗": AIProfile(0.62, 0.18, 0.72, 130, 0.58, 0.35),
    "先手压制": AIProfile(0.88, 0.10, 1.00, 75, 0.04, 0.75),
    "诱敌深入": AIProfile(0.58, 0.48, 0.58, 105, 0.40, 0.30),
    "持久消耗": AIProfile(0.56, 0.42, 0.68, 115, 0.34, 0.20),
    "奇袭爆发": AIProfile(0.92, 0.08, 0.94, 70, 0.10, 0.95),
    "以守为攻": AIProfile(0.64, 0.50, 0.76, 85, 0.15, 0.42),
    "速度拉扯": AIProfile(0.66, 0.14, 0.72, 145, 0.70, 0.45),
    "幸运赌局": AIProfile(0.78, 0.16, 0.82, 85, 0.20, 1.00),
    "破防强攻": AIProfile(0.90, 0.08, 0.98, 65, 0.03, 0.82),
    "闪避拖延": AIProfile(0.50, 0.16, 0.58, 150, 0.78, 0.20),
    "控制节奏": AIProfile(0.70, 0.34, 0.76, 105, 0.30, 0.42),
    "血量压制": AIProfile(0.82, 0.22, 0.90, 80, 0.08, 0.65),
    "精准打击": AIProfile(0.78, 0.24, 0.76, 95, 0.18, 0.48),
    "扰乱节奏": AIProfile(0.72, 0.30, 0.70, 120, 0.44, 0.68),
    "背水一战": AIProfile(0.76, 0.10, 0.88, 75, 0.06, 1.00),
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
    return AIProfile(
        min(0.95, aggression),
        min(0.60, guard),
        min(1.0, chase),
        preferred_range,
        retreat,
        low_hp_risk,
    )


def _ability_score(definition, own, opponent, distance, slot_index, rng):
    if not ABILITY_RUNTIME.compatible(definition, own):
        return None
    if own.skill_cooldowns.get(definition.ability_id, 0) > 0:
        return None
    cost = ABILITY_RUNTIME.effective_cost(definition, own)
    if definition.resource_type == "sp" and own.stamina < cost:
        return None
    if definition.resource_type == "mp" and own.mana < cost:
        projected_mana = own.mana - cost
        reduction = own.current_derived.mana_overcast_reduction if own.current_derived else 0.0
        backlash = max(
            1, math.ceil(abs(projected_mana) * 2 * (1 - reduction))
        )
        can_finish = "damage" in definition.ai_tags and opponent.current_hp <= max(10, own.primary("magic") * 2)
        emergency_heal = "heal" in definition.ai_tags and own.hp_ratio < 0.25 and own.current_hp > backlash
        if not (can_finish or emergency_heal):
            return None
    self_target = definition.targeting in {"self", "ally", "ally_area"}
    if not self_target and distance > definition.cast_range:
        return None
    if "heal" in definition.ai_tags and own.current_hp >= own.max_hp:
        return None
    if "cleanse" in definition.ai_tags and not any(not s.beneficial for s in own.statuses.values()):
        return None
    if "dispel" in definition.ai_tags and not any(s.beneficial for s in opponent.statuses.values()):
        return None
    for effect in definition.effects:
        if effect.params.get("requires_negative_status") and not any(not s.beneficial for s in own.statuses.values()):
            return None
        if effect.effect_type == "summon":
            entity_status = str(effect.params.get("entity_id", definition.ability_id))
            if entity_status in own.statuses:
                return None
        if effect.effect_type in {"apply_status", "activate_stance"}:
            recipient = own if effect.target in {"self", "ally", "ally_area"} or effect.effect_type == "activate_stance" else opponent
            if effect.status_id and effect.status_id in recipient.statuses:
                return None
    score = 20.0
    tags = set(definition.ai_tags)
    if "damage" in tags:
        score += 35 + (45 if opponent.hp_ratio < 0.25 else 0)
    if "heal" in tags:
        score += 30 + (70 if own.hp_ratio < 0.30 else 10 * (1 - own.hp_ratio))
    if "cleanse" in tags and any(s.status_id in {"stun", "paralysis", "silence", "bind"} for s in own.statuses.values()):
        score += 85
    if "control" in tags:
        score += 42
    if "defense" in tags:
        score += 25 + (35 if own.hp_ratio < 0.40 else 0)
    if tags & {"buff", "stance", "summon", "zone"}:
        score += 32
    score -= slot_index * 0.01
    score += rng.random() * 0.001
    return score


def choose_action(
    own: FighterState,
    opponent: FighterState,
    profile: AIProfile,
    rng,
    attack_range: int,
) -> ActionIntent:
    distance = abs(opponent.position - own.position)
    equipment = own.snapshot.equipment
    attack_range = equipment.attack_range if equipment else attack_range
    preferred_range = max(profile.preferred_range, round(attack_range * 0.70)) if own.snapshot.is_ranged else profile.preferred_range
    if ABILITY_RUNTIME.action_blocked(own):
        return ActionIntent("stunned")
    if own.stamina < 5 and not own.snapshot.skills:
        return ActionIntent("rest")
    low_hp = own.hp_ratio <= 0.30
    aggression = profile.aggression
    if low_hp:
        aggression += (1.0 - aggression) * profile.low_hp_risk

    if own.attack_cooldown == 0 and own.snapshot.skills:
        candidates = []
        for index, ability_id in enumerate(own.snapshot.skills.active_skill_ids):
            definition = own.snapshot.skills.active_definitions.get(ability_id)
            if definition:
                score = _ability_score(definition, own, opponent, distance, index, rng)
                if score is not None:
                    candidates.append((score, -index, ability_id))
        if candidates:
            _, _, ability_id = max(candidates)
            return ActionIntent("use_skill", ability_id)

    if ABILITY_RUNTIME.has(own, "bind") or ABILITY_RUNTIME.has(own, "never_retreat"):
        if distance <= attack_range and own.attack_cooldown == 0:
            return ActionIntent("basic_attack", "basic_attack")
        return ActionIntent("guard" if own.stamina >= 5 else "rest")
    if distance > attack_range:
        if distance < preferred_range and rng.random() < profile.retreat_tendency:
            return ActionIntent("retreat")
        if rng.random() < profile.chase_tendency or distance > preferred_range:
            return ActionIntent("advance")
        return ActionIntent("guard")
    if opponent.attack_pending:
        if rng.random() < profile.retreat_tendency * 0.55 + (1.0 - profile.guard_tendency) * 0.10:
            return ActionIntent("retreat")
        if rng.random() < profile.guard_tendency + 0.25:
            return ActionIntent("guard")
    if own.attack_cooldown > 0:
        if distance < preferred_range - 12 and rng.random() < 0.55 + profile.retreat_tendency * 0.40:
            return ActionIntent("retreat")
        if distance > preferred_range + 12 and rng.random() < profile.chase_tendency:
            return ActionIntent("advance")
        if rng.random() < profile.guard_tendency:
            return ActionIntent("guard")
        return ActionIntent("retreat" if distance <= preferred_range else "advance")
    if distance < preferred_range - 15 and rng.random() < profile.retreat_tendency:
        return ActionIntent("retreat")
    attack_cost = equipment.attack_stamina if equipment else 8
    if rng.random() < aggression and own.stamina >= attack_cost:
        return ActionIntent("basic_attack", "basic_attack")
    if rng.random() < profile.guard_tendency:
        return ActionIntent("guard")
    return ActionIntent("retreat" if distance < preferred_range else "advance")
