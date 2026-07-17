from dataclasses import dataclass, field

try:
    from .user import User
except ImportError:
    from models.user import User


@dataclass
class BattleAnalysis:
    attacker_win_rate: float
    analysis: str
    battle_log: list[str]
    raw_result: str = ""
    source: str = "local"


@dataclass
class BattleResult:
    attacker: User
    defender: User
    winner: User
    loser: User
    attacker_strategy: str
    defender_strategy: str
    attacker_strategy_random: bool
    defender_strategy_random: bool
    attacker_win_rate: float
    roll_value: float
    winner_exp_gain: int
    loser_exp_loss: int
    analysis: str
    battle_log: list[str] = field(default_factory=list)
    level_ups: list = field(default_factory=list)
    level_downs: list = field(default_factory=list)
    source: str = "local"
    target_created: bool = False
    is_counterattack: bool = False
