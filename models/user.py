from dataclasses import dataclass, field


@dataclass
class UserIdentity:
    platform: str
    group_id: str
    user_id: str
    nickname: str = ""


@dataclass
class User:
    id: int
    platform: str
    group_id: str
    user_id: str
    nickname: str
    level: int
    exp: int
    total_exp: int
    stat_points: int
    level_up_count: int
    hp: int
    atk: int
    defense: int
    speed: int
    luck: int
    wins: int
    losses: int
    created_at: str
    updated_at: str

    def stats(self) -> dict[str, int]:
        return {
            "hp": self.hp,
            "atk": self.atk,
            "defense": self.defense,
            "speed": self.speed,
            "luck": self.luck,
        }


@dataclass
class LevelUpEvent:
    from_level: int
    to_level: int
    auto_growth: dict[str, int]
    stat_points_gain: int


@dataclass
class ExpChangeResult:
    user: User
    exp_delta: int
    level_ups: list[LevelUpEvent] = field(default_factory=list)


@dataclass
class CheckinResult:
    user: User
    exp_gain: int
    streak_days: int
    level_ups: list[LevelUpEvent]
    already_checked: bool = False


@dataclass
class StatPointResult:
    user: User
    stat_name: str
    points_spent: int
    rolls: list[int]

    @property
    def total_gain(self) -> int:
        return sum(self.rolls)
