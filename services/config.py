import math


INITIAL_LEVEL = 1
INITIAL_EXP = 0
INITIAL_TOTAL_EXP = 0
INITIAL_STAT_POINTS = 0

INITIAL_STATS = {
    "strength": 1,
    "constitution": 1,
    "dexterity": 1,
    "perception": 1,
    "magic": 1,
    "willpower": 1,
}

STAT_LABELS = {
    "strength": "力量",
    "constitution": "体质",
    "dexterity": "灵巧",
    "perception": "感知",
    "magic": "魔力",
    "willpower": "意志",
    # Historical keys remain readable in old logs and LLM responses.
    "hp": "力量",
    "defense": "体质",
    "speed": "灵巧",
    "atk": "感知",
    "luck": "魔力",
}

STAT_ALIASES = {
    **{key: key for key in INITIAL_STATS},
    "力量": "strength", "str": "strength", "生命": "strength", "血量": "strength", "hp": "strength",
    "体质": "constitution", "con": "constitution", "防御": "constitution", "def": "constitution", "defense": "constitution",
    "灵巧": "dexterity", "dex": "dexterity",
    "感知": "perception", "per": "perception", "攻击": "perception", "atk": "perception",
    "魔力": "magic", "mag": "magic",
    "意志": "willpower", "wil": "willpower", "will": "willpower",
}

LEVEL_EXP_BASE = 100
LEVEL_EXP_GROWTH = 1.18
STAT_POINTS_PER_LEVEL = 1
SKILL_POINTS_PER_LEVEL = 1
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
BATTLE_EXP_TRANSFER_BASE_RATE = 0.24
BATTLE_EXP_TRANSFER_LEVEL_DIFF_RATE_STEP = 0.06
BATTLE_EXP_TRANSFER_LOWER_LEVEL_RATE_STEP = 0.02
BATTLE_EXP_TRANSFER_RANDOM_RATE_RANGE = (-0.04, 0.06)
BATTLE_EXP_TRANSFER_RATE_RANGE = (0.02, 1.00)
BATTLE_WIN_EXP_LEVEL_CAP_RATE = 1.00
BATTLE_WIN_EXP_LOSER_LEVEL_FLOOR_RATE = 0.05
BATTLE_WIN_EXP_ABSOLUTE_FLOOR = 5
LLM_RATE_WEIGHT = 0.3
BATTLE_LOG_MIN_LINES = 6
BATTLE_LOG_MAX_LINES = 10

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
    "稳扎稳打": ("均衡", "体质", "力量"),
    "全力猛攻": ("感知",),
    "防守反击": ("体质", "力量"),
    "游走消耗": ("灵巧", "魔力"),
    "先手压制": ("灵巧", "感知"),
    "诱敌深入": ("体质", "魔力"),
    "持久消耗": ("力量", "体质"),
    "奇袭爆发": ("感知", "魔力"),
    "以守为攻": ("体质", "感知"),
    "速度拉扯": ("灵巧",),
    "幸运赌局": ("魔力",),
    "破防强攻": ("感知",),
    "闪避拖延": ("灵巧", "魔力"),
    "控制节奏": ("均衡", "灵巧"),
    "血量压制": ("力量",),
    "精准打击": ("感知", "灵巧"),
    "扰乱节奏": ("魔力", "灵巧"),
    "背水一战": ("感知", "力量", "魔力"),
}

# Each rule is:
# (own_stat, opponent_stat, own_factor, opponent_factor, margin,
#  success_bonus, fail_penalty, critical)
#
# A strategy can only turn a named counter into an actual counter if its
# critical checks pass and enough supporting checks are satisfied.
BATTLE_STRATEGY_ATTRIBUTE_RULES = {
    "稳扎稳打": (
        ("constitution", "perception", 1.0, 0.9, 0, 0.018, -0.018, False),
        ("strength", "strength", 1.0, 0.9, 0, 0.018, -0.018, True),
        ("perception", "constitution", 1.0, 0.65, 0, 0.012, -0.012, False),
    ),
    "全力猛攻": (
        ("perception", "constitution", 1.0, 1.0, 1, 0.026, -0.028, True),
        ("dexterity", "dexterity", 1.0, 0.85, 0, 0.016, -0.018, False),
        ("strength", "strength", 1.0, 0.8, 0, 0.014, -0.018, False),
    ),
    "防守反击": (
        ("constitution", "perception", 1.0, 0.9, 0, 0.026, -0.032, True),
        ("strength", "perception", 1.0, 1.6, 0, 0.018, -0.02, False),
        ("dexterity", "dexterity", 1.0, 0.75, 0, 0.012, -0.014, False),
    ),
    "游走消耗": (
        ("dexterity", "dexterity", 1.0, 1.0, 1, 0.028, -0.038, True),
        ("constitution", "dexterity", 1.0, 0.75, 0, 0.014, -0.018, False),
        ("magic", "magic", 1.0, 0.8, 0, 0.014, -0.014, False),
    ),
    "先手压制": (
        ("dexterity", "dexterity", 1.0, 1.0, 1, 0.03, -0.04, True),
        ("perception", "constitution", 1.0, 0.9, 0, 0.022, -0.022, False),
        ("magic", "strength", 1.0, 0.55, 0, 0.012, -0.012, False),
    ),
    "诱敌深入": (
        ("constitution", "perception", 1.0, 0.85, 0, 0.024, -0.03, True),
        ("strength", "perception", 1.0, 1.7, 0, 0.02, -0.022, False),
        ("magic", "magic", 1.0, 0.9, 0, 0.014, -0.014, False),
    ),
    "持久消耗": (
        ("strength", "strength", 1.0, 1.0, 0, 0.028, -0.034, True),
        ("constitution", "perception", 1.0, 0.8, 0, 0.018, -0.02, False),
        ("magic", "magic", 1.0, 0.75, 0, 0.012, -0.012, False),
    ),
    "奇袭爆发": (
        ("dexterity", "dexterity", 1.0, 1.0, 1, 0.03, -0.045, True),
        ("perception", "constitution", 1.0, 0.85, 0, 0.024, -0.024, False),
        ("magic", "magic", 1.0, 0.9, 0, 0.018, -0.018, False),
    ),
    "以守为攻": (
        ("constitution", "perception", 1.0, 0.9, 0, 0.026, -0.032, True),
        ("perception", "constitution", 1.0, 0.75, 0, 0.016, -0.018, False),
        ("strength", "strength", 1.0, 0.8, 0, 0.014, -0.016, False),
    ),
    "速度拉扯": (
        ("dexterity", "dexterity", 1.0, 1.0, 1, 0.032, -0.045, True),
        ("perception", "strength", 1.0, 0.6, 0, 0.016, -0.016, False),
        ("constitution", "strength", 1.0, 0.5, 0, 0.012, -0.014, False),
    ),
    "幸运赌局": (
        ("magic", "magic", 1.0, 1.0, 1, 0.03, -0.038, True),
        ("constitution", "perception", 1.0, 0.7, 0, 0.014, -0.014, False),
        ("perception", "constitution", 1.0, 0.7, 0, 0.012, -0.014, False),
    ),
    "破防强攻": (
        ("perception", "constitution", 1.0, 1.0, 2, 0.032, -0.04, True),
        ("dexterity", "dexterity", 1.0, 0.75, 0, 0.014, -0.016, False),
        ("strength", "strength", 1.0, 0.8, 0, 0.012, -0.014, False),
    ),
    "闪避拖延": (
        ("dexterity", "dexterity", 1.0, 1.0, 1, 0.03, -0.042, False),
        ("magic", "magic", 1.0, 0.9, 0, 0.018, -0.018, True),
        ("strength", "strength", 1.0, 0.8, 0, 0.012, -0.014, False),
    ),
    "控制节奏": (
        ("dexterity", "dexterity", 1.0, 0.9, 0, 0.022, -0.026, False),
        ("constitution", "dexterity", 1.0, 0.8, 0, 0.016, -0.016, True),
        ("strength", "magic", 1.0, 1.2, 0, 0.014, -0.014, False),
    ),
    "血量压制": (
        ("strength", "strength", 1.0, 1.0, 0, 0.032, -0.04, True),
        ("constitution", "perception", 1.0, 0.75, 0, 0.016, -0.018, False),
        ("perception", "constitution", 1.0, 0.7, 0, 0.012, -0.014, False),
    ),
    "精准打击": (
        ("perception", "constitution", 1.0, 0.9, 0, 0.026, -0.03, True),
        ("dexterity", "dexterity", 1.0, 0.9, 0, 0.018, -0.02, False),
        ("magic", "magic", 1.0, 0.9, 0, 0.016, -0.016, False),
    ),
    "扰乱节奏": (
        ("magic", "magic", 1.0, 0.95, 0, 0.024, -0.03, True),
        ("dexterity", "dexterity", 1.0, 0.85, 0, 0.018, -0.02, False),
        ("constitution", "strength", 1.0, 0.5, 0, 0.012, -0.014, False),
    ),
    "背水一战": (
        ("perception", "constitution", 1.0, 0.9, 0, 0.026, -0.032, True),
        ("magic", "magic", 1.0, 0.9, 0, 0.018, -0.018, False),
        ("strength", "strength", 1.0, 0.7, 0, 0.012, -0.02, False),
    ),
}


def exp_required_for_next_level(level: int) -> int:
    return math.floor(LEVEL_EXP_BASE * (LEVEL_EXP_GROWTH ** (level - 1)))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
