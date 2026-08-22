from dataclasses import dataclass, field

try:
    from .combat import SimulationResult
    from .user import User
except ImportError:
    from models.combat import SimulationResult
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
    loser_exp_gain: int = 0
    attacker_exp_gain: int = 0
    defender_exp_gain: int = 0
    rated: bool = False
    reward_reason: str = ""
    attacker_rating_before: int = 1000
    attacker_rating_after: int = 1000
    defender_rating_before: int = 1000
    defender_rating_after: int = 1000
    winner_rating_delta: int = 0
    loser_rating_delta: int = 0
    battle_log: list[str] = field(default_factory=list)
    level_ups: list = field(default_factory=list)
    level_downs: list = field(default_factory=list)
    source: str = "local"
    target_created: bool = False
    is_counterattack: bool = False
    simulation: SimulationResult | None = None
    skill_growths: list = field(default_factory=list)
    attribute_growths: list = field(default_factory=list)
    spell_growths: list = field(default_factory=list)
    loser_level_ups: list = field(default_factory=list)
