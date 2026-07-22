try:
    from ..models.combat import AIProfile, ActionIntent, FighterState
except ImportError:
    from models.combat import AIProfile, ActionIntent, FighterState


DEFAULT_PROFILE = AIProfile()

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
    aggression = 0.70 + (0.13 if "atk" in stats else 0.0)
    guard = 0.20 + (0.22 if "defense" in stats else 0.0)
    chase = 0.80 + (0.13 if "speed" in stats else 0.0)
    preferred_range = 125 if "speed" in stats else 90
    retreat = 0.42 if "speed" in stats else 0.15
    low_hp_risk = 0.78 if "luck" in stats else (0.32 if "hp" in stats else 0.50)
    return AIProfile(
        min(0.95, aggression),
        min(0.60, guard),
        min(1.0, chase),
        preferred_range,
        retreat,
        low_hp_risk,
    )


def choose_action(
    own: FighterState,
    opponent: FighterState,
    profile: AIProfile,
    rng,
    attack_range: int,
) -> ActionIntent:
    distance = abs(opponent.position - own.position)
    low_hp = own.hp_ratio <= 0.30
    aggression = profile.aggression
    if low_hp:
        aggression = aggression + (1.0 - aggression) * profile.low_hp_risk

    if distance > attack_range:
        if distance < profile.preferred_range and rng.random() < profile.retreat_tendency:
            return ActionIntent("retreat")
        if rng.random() < profile.chase_tendency or distance > profile.preferred_range:
            return ActionIntent("advance")
        return ActionIntent("guard")

    # A visible windup can be answered by guarding or moving out of range.
    if opponent.attack_pending:
        evade_windup = (
            profile.retreat_tendency * 0.55
            + (1.0 - profile.guard_tendency) * 0.10
        )
        if rng.random() < evade_windup:
            return ActionIntent("retreat")
        if rng.random() < profile.guard_tendency + 0.25:
            return ActionIntent("guard")

    # During cooldown, actively restore the strategy's preferred spacing instead
    # of standing still until the next attack becomes available.
    if own.attack_cooldown > 0:
        spacing_margin = 12
        if distance < profile.preferred_range - spacing_margin:
            if rng.random() < 0.55 + profile.retreat_tendency * 0.40:
                return ActionIntent("retreat")
        elif distance > profile.preferred_range + spacing_margin:
            if rng.random() < profile.chase_tendency:
                return ActionIntent("advance")
        if rng.random() < profile.guard_tendency:
            return ActionIntent("guard")
        return ActionIntent(
            "retreat" if distance <= profile.preferred_range else "advance"
        )

    if (
        distance < profile.preferred_range - 15
        and rng.random() < profile.retreat_tendency
    ):
        return ActionIntent("retreat")
    if rng.random() < aggression:
        return ActionIntent("basic_attack", "basic_attack")
    if rng.random() < profile.guard_tendency:
        return ActionIntent("guard")
    return ActionIntent("retreat" if distance < profile.preferred_range else "advance")
