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

CHECKIN_ROLL_EXP_RANGE = (1, 100)
CHECKIN_FALLBACK_THRESHOLD_RATE = 0.10
CHECKIN_FALLBACK_EXP_RATE_RANGE = (0.08, 0.12)
CHECKIN_BASE_EXP_CAP_RATE = 0.60
CHECKIN_DAY_RESET_HOUR = 5
CHECKIN_MAX_STREAK_BONUS_DAYS = 7
CHECKIN_STREAK_BONUS_STEP = 5

BATTLE_ACTIVE_CHALLENGE_WINDOW_SECONDS = 10 * 60
BATTLE_ACTIVE_CHALLENGE_LIMIT = 3
BATTLE_COUNTERATTACK_WINDOW_SECONDS = 2 * 60
BATTLE_MIN_WIN_RATE = 0.15
BATTLE_MAX_WIN_RATE = 0.85
BATTLE_LEVEL_RATE_STEP = 0.025
BATTLE_LEVEL_RATE_MAX = 0.15
BATTLE_LUCK_RATE_STEP = 0.01
BATTLE_LUCK_RATE_MAX = 0.08
BATTLE_WIN_EXP_BASE_RATE = 0.24
BATTLE_WIN_EXP_LEVEL_DIFF_RATE_STEP = 0.06
BATTLE_WIN_EXP_RANDOM_RATE_RANGE = (-0.04, 0.06)
BATTLE_WIN_EXP_RATE_RANGE = (0.12, 1.00)
BATTLE_LOSE_EXP_BASE_RATE = 0.15
BATTLE_LOSE_EXP_LEVEL_DIFF_RATE_STEP = 0.04
BATTLE_LOSE_EXP_RANDOM_RATE_RANGE = (-0.03, 0.03)
BATTLE_LOSE_EXP_RATE_RANGE = (0.08, 0.45)
LLM_RATE_WEIGHT = 0.3
BATTLE_LOG_MAX_LINES = 3

BATTLE_STRATEGY_NAMES = (
    "稳扎稳打",
    "全力猛攻",
    "防守反击",
    "游走消耗",
    "先手压制",
    "诱敌深入",
    "持久消耗",
    "奇袭爆发",
    "以守为攻",
    "速度拉扯",
    "幸运赌局",
    "破防强攻",
    "闪避拖延",
    "控制节奏",
    "血量压制",
    "精准打击",
    "扰乱节奏",
    "背水一战",
)

BATTLE_STRATEGY_ALIASES = {
    "平衡": "稳扎稳打",
    "稳健": "稳扎稳打",
    "猛攻": "全力猛攻",
    "强攻": "全力猛攻",
    "反击": "防守反击",
    "游走": "游走消耗",
    "消耗": "游走消耗",
    "先手": "先手压制",
    "压制": "先手压制",
    "诱敌": "诱敌深入",
    "持久": "持久消耗",
    "拖延": "闪避拖延",
    "奇袭": "奇袭爆发",
    "爆发": "奇袭爆发",
    "守攻": "以守为攻",
    "拉扯": "速度拉扯",
    "赌": "幸运赌局",
    "赌局": "幸运赌局",
    "破防": "破防强攻",
    "闪避": "闪避拖延",
    "控节奏": "控制节奏",
    "节奏": "控制节奏",
    "血量": "血量压制",
    "精准": "精准打击",
    "扰乱": "扰乱节奏",
    "背水": "背水一战",
}

BATTLE_STRATEGY_COUNTERS = {
    "稳扎稳打": ("奇袭爆发", "扰乱节奏", "幸运赌局"),
    "全力猛攻": ("持久消耗", "血量压制", "闪避拖延"),
    "防守反击": ("全力猛攻", "破防强攻", "先手压制"),
    "游走消耗": ("防守反击", "以守为攻", "血量压制"),
    "先手压制": ("奇袭爆发", "幸运赌局", "控制节奏"),
    "诱敌深入": ("全力猛攻", "先手压制", "破防强攻"),
    "持久消耗": ("防守反击", "诱敌深入", "稳扎稳打"),
    "奇袭爆发": ("游走消耗", "速度拉扯", "精准打击"),
    "以守为攻": ("先手压制", "奇袭爆发", "全力猛攻"),
    "速度拉扯": ("防守反击", "破防强攻", "血量压制"),
    "幸运赌局": ("控制节奏", "稳扎稳打", "精准打击"),
    "破防强攻": ("稳扎稳打", "以守为攻", "持久消耗"),
    "闪避拖延": ("全力猛攻", "破防强攻", "精准打击"),
    "控制节奏": ("游走消耗", "速度拉扯", "扰乱节奏"),
    "血量压制": ("奇袭爆发", "精准打击", "幸运赌局"),
    "精准打击": ("闪避拖延", "扰乱节奏", "速度拉扯"),
    "扰乱节奏": ("诱敌深入", "防守反击", "以守为攻"),
    "背水一战": ("血量压制", "持久消耗", "稳扎稳打"),
}

BATTLE_STRATEGY_BUILD_TYPES = {
    "稳扎稳打": ("均衡", "防御", "生命"),
    "全力猛攻": ("攻击",),
    "防守反击": ("防御", "生命"),
    "游走消耗": ("速度", "幸运"),
    "先手压制": ("速度", "攻击"),
    "诱敌深入": ("防御", "幸运"),
    "持久消耗": ("生命", "防御"),
    "奇袭爆发": ("攻击", "幸运"),
    "以守为攻": ("防御", "攻击"),
    "速度拉扯": ("速度",),
    "幸运赌局": ("幸运",),
    "破防强攻": ("攻击",),
    "闪避拖延": ("速度", "幸运"),
    "控制节奏": ("均衡", "速度"),
    "血量压制": ("生命",),
    "精准打击": ("攻击", "速度"),
    "扰乱节奏": ("幸运", "速度"),
    "背水一战": ("攻击", "生命", "幸运"),
}

# Each rule is:
# (own_stat, opponent_stat, own_factor, opponent_factor, margin,
#  success_bonus, fail_penalty, critical)
#
# A strategy can only turn a named counter into an actual counter if its
# critical checks pass and enough supporting checks are satisfied.
BATTLE_STRATEGY_ATTRIBUTE_RULES = {
    "稳扎稳打": (
        ("defense", "atk", 1.0, 0.9, 0, 0.018, -0.018, False),
        ("hp", "hp", 1.0, 0.9, 0, 0.018, -0.018, True),
        ("atk", "defense", 1.0, 0.65, 0, 0.012, -0.012, False),
    ),
    "全力猛攻": (
        ("atk", "defense", 1.0, 1.0, 1, 0.026, -0.028, True),
        ("speed", "speed", 1.0, 0.85, 0, 0.016, -0.018, False),
        ("hp", "hp", 1.0, 0.8, 0, 0.014, -0.018, False),
    ),
    "防守反击": (
        ("defense", "atk", 1.0, 0.9, 0, 0.026, -0.032, True),
        ("hp", "atk", 1.0, 1.6, 0, 0.018, -0.02, False),
        ("speed", "speed", 1.0, 0.75, 0, 0.012, -0.014, False),
    ),
    "游走消耗": (
        ("speed", "speed", 1.0, 1.0, 1, 0.028, -0.038, True),
        ("defense", "speed", 1.0, 0.75, 0, 0.014, -0.018, False),
        ("luck", "luck", 1.0, 0.8, 0, 0.014, -0.014, False),
    ),
    "先手压制": (
        ("speed", "speed", 1.0, 1.0, 1, 0.03, -0.04, True),
        ("atk", "defense", 1.0, 0.9, 0, 0.022, -0.022, False),
        ("luck", "hp", 1.0, 0.55, 0, 0.012, -0.012, False),
    ),
    "诱敌深入": (
        ("defense", "atk", 1.0, 0.85, 0, 0.024, -0.03, True),
        ("hp", "atk", 1.0, 1.7, 0, 0.02, -0.022, False),
        ("luck", "luck", 1.0, 0.9, 0, 0.014, -0.014, False),
    ),
    "持久消耗": (
        ("hp", "hp", 1.0, 1.0, 0, 0.028, -0.034, True),
        ("defense", "atk", 1.0, 0.8, 0, 0.018, -0.02, False),
        ("luck", "luck", 1.0, 0.75, 0, 0.012, -0.012, False),
    ),
    "奇袭爆发": (
        ("speed", "speed", 1.0, 1.0, 1, 0.03, -0.045, True),
        ("atk", "defense", 1.0, 0.85, 0, 0.024, -0.024, False),
        ("luck", "luck", 1.0, 0.9, 0, 0.018, -0.018, False),
    ),
    "以守为攻": (
        ("defense", "atk", 1.0, 0.9, 0, 0.026, -0.032, True),
        ("atk", "defense", 1.0, 0.75, 0, 0.016, -0.018, False),
        ("hp", "hp", 1.0, 0.8, 0, 0.014, -0.016, False),
    ),
    "速度拉扯": (
        ("speed", "speed", 1.0, 1.0, 1, 0.032, -0.045, True),
        ("atk", "hp", 1.0, 0.6, 0, 0.016, -0.016, False),
        ("defense", "hp", 1.0, 0.5, 0, 0.012, -0.014, False),
    ),
    "幸运赌局": (
        ("luck", "luck", 1.0, 1.0, 1, 0.03, -0.038, True),
        ("defense", "atk", 1.0, 0.7, 0, 0.014, -0.014, False),
        ("atk", "defense", 1.0, 0.7, 0, 0.012, -0.014, False),
    ),
    "破防强攻": (
        ("atk", "defense", 1.0, 1.0, 2, 0.032, -0.04, True),
        ("speed", "speed", 1.0, 0.75, 0, 0.014, -0.016, False),
        ("hp", "hp", 1.0, 0.8, 0, 0.012, -0.014, False),
    ),
    "闪避拖延": (
        ("speed", "speed", 1.0, 1.0, 1, 0.03, -0.042, False),
        ("luck", "luck", 1.0, 0.9, 0, 0.018, -0.018, True),
        ("hp", "hp", 1.0, 0.8, 0, 0.012, -0.014, False),
    ),
    "控制节奏": (
        ("speed", "speed", 1.0, 0.9, 0, 0.022, -0.026, False),
        ("defense", "speed", 1.0, 0.8, 0, 0.016, -0.016, True),
        ("hp", "luck", 1.0, 1.2, 0, 0.014, -0.014, False),
    ),
    "血量压制": (
        ("hp", "hp", 1.0, 1.0, 0, 0.032, -0.04, True),
        ("defense", "atk", 1.0, 0.75, 0, 0.016, -0.018, False),
        ("atk", "defense", 1.0, 0.7, 0, 0.012, -0.014, False),
    ),
    "精准打击": (
        ("atk", "defense", 1.0, 0.9, 0, 0.026, -0.03, True),
        ("speed", "speed", 1.0, 0.9, 0, 0.018, -0.02, False),
        ("luck", "luck", 1.0, 0.9, 0, 0.016, -0.016, False),
    ),
    "扰乱节奏": (
        ("luck", "luck", 1.0, 0.95, 0, 0.024, -0.03, True),
        ("speed", "speed", 1.0, 0.85, 0, 0.018, -0.02, False),
        ("defense", "hp", 1.0, 0.5, 0, 0.012, -0.014, False),
    ),
    "背水一战": (
        ("atk", "defense", 1.0, 0.9, 0, 0.026, -0.032, True),
        ("luck", "luck", 1.0, 0.9, 0, 0.018, -0.018, False),
        ("hp", "hp", 1.0, 0.7, 0, 0.012, -0.02, False),
    ),
}


def exp_required_for_next_level(level: int) -> int:
    return math.floor(LEVEL_EXP_BASE * (LEVEL_EXP_GROWTH ** (level - 1)))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
