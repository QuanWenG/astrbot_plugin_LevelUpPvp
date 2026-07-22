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
    skill_points: int = 0
    willpower: int = 5
    life_growth: int = 100
    mana_growth: int = 100
    advanced_speed: int = 100
    advanced_luck: int = 100
    frozen_stats: dict[str, int] = field(default_factory=dict)
    frozen_stat_points: int = 0
    frozen_skill_points: int = 0
    frozen_levels: list[int] = field(default_factory=list)

    @property
    def strength(self) -> int:
        return self.hp

    @property
    def constitution(self) -> int:
        return self.defense

    @property
    def dexterity(self) -> int:
        return self.speed

    @property
    def perception(self) -> int:
        return self.atk

    @property
    def magic(self) -> int:
        return self.luck

    def stats(self) -> dict[str, int]:
        return {
            "strength": self.strength,
            "constitution": self.constitution,
            "dexterity": self.dexterity,
            "perception": self.perception,
            "magic": self.magic,
            "willpower": self.willpower,
        }


@dataclass
class LevelUpEvent:
    from_level: int
    to_level: int
    auto_growth: dict[str, int]
    stat_points_gain: int
    restored_from_freeze: bool = False
    skill_points_gain: int = 0


@dataclass
class LevelDownEvent:
    from_level: int
    to_level: int
    frozen_stats: dict[str, int]
    frozen_stat_points: int
    frozen_skill_points: int = 0


@dataclass
class ExpChangeResult:
    user: User
    exp_delta: int
    level_ups: list[LevelUpEvent] = field(default_factory=list)
    level_downs: list[LevelDownEvent] = field(default_factory=list)


@dataclass
class CheckinResult:
    user: User
    exp_gain: int
    streak_days: int
    level_ups: list[LevelUpEvent]
    already_checked: bool = False
    attribute_potential_restore: int = 0


@dataclass
class StatPointResult:
    user: User
    stat_name: str
    points_spent: int
    rolls: list[int]

    @property
    def total_gain(self) -> int:
        return sum(self.rolls)
