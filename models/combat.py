from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class FighterSnapshot:
    user_pk: int
    name: str
    level: int
    hp: int
    atk: int
    defense: int
    speed: int
    luck: int
    strategy: str
    equipment_modifiers: dict[str, int] = field(default_factory=dict)
    skill_ids: tuple[str, ...] = ("basic_attack",)

    def stat(self, name: str) -> int:
        return max(0, int(getattr(self, name)) + int(self.equipment_modifiers.get(name, 0)))

    @property
    def max_hp(self) -> int:
        return 50 + self.stat("hp") * 10

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["skill_ids"] = list(self.skill_ids)
        payload["max_hp"] = self.max_hp
        return payload


@dataclass(frozen=True)
class AIProfile:
    aggression: float = 0.7
    guard_tendency: float = 0.2
    chase_tendency: float = 0.8
    preferred_range: int = 90
    retreat_tendency: float = 0.15
    low_hp_risk: float = 0.5


@dataclass(frozen=True)
class ActionIntent:
    action: str
    skill_id: str | None = None


@dataclass
class FighterState:
    snapshot: FighterSnapshot
    current_hp: int
    position: int
    attack_cooldown: int = 0
    windup_ticks: int = 0
    recovery_ticks: int = 0
    hitstun_ticks: int = 0
    attack_pending: bool = False
    guarding: bool = False
    damage_dealt: int = 0

    @property
    def alive(self) -> bool:
        return self.current_hp > 0

    @property
    def hp_ratio(self) -> float:
        return max(0.0, self.current_hp / self.snapshot.max_hp)


@dataclass(frozen=True)
class BattleEvent:
    tick: int
    kind: str
    actor_pk: int | None = None
    target_pk: int | None = None
    value: int = 0
    remaining_hp: int | None = None
    position: int | None = None
    critical: bool = False
    guarded: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BattleState:
    tick: int
    attacker: FighterState
    defender: FighterState
    events: list[BattleEvent]
    random_seed: int
    finish_reason: str = ""


@dataclass(frozen=True)
class SimulationResult:
    attacker: FighterSnapshot
    defender: FighterSnapshot
    winner_pk: int
    loser_pk: int
    duration_ticks: int
    finish_reason: str
    attacker_remaining_hp: int
    defender_remaining_hp: int
    attacker_damage_dealt: int
    defender_damage_dealt: int
    events: tuple[BattleEvent, ...]
    random_seed: int
    engine_version: str = "sideview-v2"

    def to_dict(self) -> dict:
        return {
            "engine_version": self.engine_version,
            "random_seed": self.random_seed,
            "duration_ticks": self.duration_ticks,
            "finish_reason": self.finish_reason,
            "winner_pk": self.winner_pk,
            "loser_pk": self.loser_pk,
            "attacker": self.attacker.to_dict(),
            "defender": self.defender.to_dict(),
            "attacker_remaining_hp": self.attacker_remaining_hp,
            "defender_remaining_hp": self.defender_remaining_hp,
            "attacker_damage_dealt": self.attacker_damage_dealt,
            "defender_damage_dealt": self.defender_damage_dealt,
            "events": [event.to_dict() for event in self.events],
        }
