import math


INITIAL_LEVEL = 1
INITIAL_EXP = 0
INITIAL_TOTAL_EXP = 0
INITIAL_STAT_POINTS = 0

INITIAL_STATS = {
    "hp": 10,
    "atk": 5,
    "defense": 5,
    "speed": 5,
    "luck": 5,
}

STAT_LABELS = {
    "hp": "生命",
    "atk": "攻击",
    "defense": "防御",
    "speed": "速度",
    "luck": "幸运",
}

STAT_ALIASES = {
    "生命": "hp",
    "血量": "hp",
    "hp": "hp",
    "攻击": "atk",
    "atk": "atk",
    "防御": "defense",
    "def": "defense",
    "defense": "defense",
    "速度": "speed",
    "speed": "speed",
    "幸运": "luck",
    "luck": "luck",
}

LEVEL_EXP_BASE = 100
LEVEL_EXP_GROWTH = 1.18
STAT_POINTS_PER_LEVEL = 3

AUTO_GROWTH_STAT_COUNT_RANGE = (2, 3)
AUTO_GROWTH_RANGES = {
    "hp": (2, 5),
    "atk": (1, 3),
    "defense": (1, 3),
    "speed": (1, 2),
    "luck": (1, 2),
}

STAT_POINT_RANGES = {
    "hp": (2, 4),
    "atk": (1, 3),
    "defense": (1, 3),
    "speed": (1, 2),
    "luck": (1, 2),
}

CHECKIN_BASE_EXP_RANGE = (20, 50)
CHECKIN_MAX_STREAK_BONUS_DAYS = 7
CHECKIN_STREAK_BONUS_STEP = 5

BATTLE_USER_COOLDOWN_SECONDS = 5 * 60
BATTLE_PAIR_COOLDOWN_SECONDS = 10 * 60
BATTLE_MIN_WIN_RATE = 0.15
BATTLE_MAX_WIN_RATE = 0.85
BATTLE_LEVEL_RATE_STEP = 0.025
BATTLE_LEVEL_RATE_MAX = 0.15
BATTLE_LUCK_RATE_STEP = 0.01
BATTLE_LUCK_RATE_MAX = 0.08
BATTLE_WIN_EXP_BASE = 40
BATTLE_WIN_EXP_PER_LOSER_LEVEL = 5
BATTLE_LOSE_EXP_BASE = 20
BATTLE_LOSE_EXP_PER_LOSER_LEVEL = 3
LLM_RATE_WEIGHT = 0.3


def exp_required_for_next_level(level: int) -> int:
    return math.floor(LEVEL_EXP_BASE * (LEVEL_EXP_GROWTH ** (level - 1)))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
