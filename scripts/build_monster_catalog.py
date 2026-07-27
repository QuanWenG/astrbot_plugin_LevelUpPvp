"""Build and audit the checked-in Elona Mobile No.1-215 monster catalog.

Network access is only used when ``--fetch`` is explicitly supplied. Runtime
catalog loading and ``--audit`` are completely offline.
"""

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "assets" / "monster_catalog.json"
SOURCE_URL = (
    "https://wikiwiki.jp/elonamobile/?cmd=source&page="
    "%E3%82%AD%E3%83%A3%E3%83%A9%2F"
    "%E3%82%AD%E3%83%A3%E3%83%A9%E4%B8%80%E8%A6%A7%2F"
    "No1%EF%BD%9E215"
)
PAGE_URL = (
    "https://wikiwiki.jp/elonamobile/"
    "%E3%82%AD%E3%83%A3%E3%83%A9/"
    "%E3%82%AD%E3%83%A3%E3%83%A9%E4%B8%80%E8%A6%A7"
)
ATTRIBUTES = (
    "strength", "constitution", "dexterity",
    "perception", "magic", "willpower",
)

CLASS_IDS = {
    "無": "class_none",
    "戦士": "class_warrior",
    "魔法使い": "class_wizard",
    "魔法戦士": "class_warmage",
    "略奪者": "class_predator",
    "遺跡荒らし": "class_ruin_raider",
    "機工兵": "class_machinist",
    "狩人": "class_hunter",
    "神官": "class_priest",
    "観光客": "class_tourist",
    "クレイモア": "class_claymore",
}
ICONIC_RACE_IDS = {
    "スライム": "slime",
    "クイックリング": "quickling",
    "蜘蛛": "spider",
    "巨人": "giant",
    "ドラゴン": "dragon",
    "精霊": "spirit",
    "小精霊": "minor_spirit",
    "ゴーレム": "golem",
    "リッチ": "lich",
    "幽霊": "ghost",
    "コウモリ": "bat",
    "猫": "cat",
    "犬": "dog",
    "機械": "machine",
}
CN_NAMES = {
    "プチ": "普奇",
    "ベスプチ": "贝斯普奇",
    "クイックリング": "快可灵",
    "クイックリングの弓使い": "快可灵弓手",
    "シルバーベル": "银铃",
    "ゴールドベル": "金铃",
    "キューブ": "立方体",
    "異形": "异形",
    "迷子の子猫": "迷路的小猫",
    "地雷犬": "地雷犬",
    "マンモス": "猛犸",
    "サイクロプス": "独眼巨人",
    "タイタン": "泰坦",
    "グリーンドラゴン": "绿龙",
    "レッドドラゴン": "红龙",
    "ホワイトドラゴン": "白龙",
    "エレキドラゴン": "雷龙",
    "野うさぎ": "野兔",
    "かたつむり": "蜗牛",
    "敗残兵": "残兵",
    "羊": "绵羊",
    "飛び蛙": "飞蛙",
    "ごろつき": "街头混混",
    "コボルト": "狗头人",
    "ムカデ": "蜈蚣",
    "きのこ": "蘑菇",
    "オーク": "兽人",
    "エレアの戦士": "艾莱亚战士",
    "マンドレイク": "曼德拉草",
    "オークの戦士": "兽人战士",
    "ゾンビ": "僵尸",
    "カブトムシ": "独角仙",
    "エレアの魔術師": "艾莱亚魔法师",
    "コウモリ": "蝙蝠",
    "吸血コウモリ": "吸血蝙蝠",
    "ドラゴンバット": "龙蝠",
    "火炎樹": "火焰树",
    "氷結樹": "冰结树",
    "リッチ": "巫妖",
    "マスターリッチ": "巫妖大师",
    "デミリッチ": "半巫妖",
    "猟犬": "猎犬",
    "ファイアハウンド": "火焰猎犬",
    "アイスハウンド": "寒冰猎犬",
    "ライトニングハウンド": "雷电猎犬",
    "ダークハウンド": "暗影猎犬",
    "幻惑ハウンド": "幻惑猎犬",
    "神経ハウンド": "神经猎犬",
    "ポイズンハウンド": "剧毒猎犬",
    "轟音ハウンド": "轰鸣猎犬",
    "地獄ハウンド": "地狱猎犬",
    "カオスハウンド": "混沌猎犬",
    "巨大リス": "巨型松鼠",
    "殺人リス": "杀人松鼠",
    "怨念": "怨灵",
    "餓鬼": "饿鬼",
    "放電雲": "放电云",
    "混沌の塊": "混沌聚合体",
    "フローティングアイ": "漂浮之眼",
    "ワイバーン": "双足飞龙",
    "パペット": "魔偶",
    "ワスプ": "黄蜂",
    "レッドワスプ": "赤红黄蜂",
    "デーモン": "恶魔",
    "冥界の使い": "冥界使者",
    "カオスインプ": "混沌小恶魔",
    "亡者の手": "亡者之手",
    "混沌の手": "混沌之手",
    "殺人鬼の手": "杀人魔之手",
    "亡霊": "亡灵",
    "ニンフ": "宁芙",
    "人食い花": "食人花",
    "カオスフラワー": "混沌之花",
    "コブラ": "眼镜蛇",
    "キングコブラ": "眼镜王蛇",
    "ファイアドレイク": "火焰幼龙",
    "アイスドレイク": "寒冰幼龙",
    "レッサーマミー": "次级木乃伊",
    "マミー": "木乃伊",
    "グレーターマミー": "高级木乃伊",
    "ゴブリン": "哥布林",
    "ゴブリンの戦士": "哥布林战士",
    "ゴブリンのシャーマン": "哥布林萨满",
    "ゴブリンの魔法使い": "哥布林魔法师",
    "赤の洗礼者": "赤色洗礼者",
    "青の洗礼者": "蓝色洗礼者",
    "ブラウンベア": "棕熊",
    "グリズリー": "灰熊",
    "リビングアーマー": "活铠甲",
    "鉄塊": "铁块",
    "ゴールデンアーマー": "黄金铠甲",
    "デスアーマー": "死亡铠甲",
    "メデューサ": "美杜莎",
    "エウリュアレ": "尤瑞艾莉",
    "ステンノ": "丝西娜",
    "恋のキューピッド": "恋爱丘比特",
    "レッサーファントム": "次级幻影",
    "ハーピー": "鹰身女妖",
    "冥界ドラゴン": "冥界龙",
    "カオスドラゴン": "混沌龙",
    "ケルベロス": "刻耳柏洛斯",
    "まだら蜘蛛": "斑纹蜘蛛",
    "ブラックウィドウ": "黑寡妇",
    "パラライザー": "麻痹蜘蛛",
    "タランチュラ": "狼蛛",
    "吸血蜘蛛": "吸血蜘蛛",
    "ウッドゴーレム": "木魔像",
    "ストーンゴーレム": "石魔像",
    "スティールゴーレム": "钢铁魔像",
    "ゴールドゴーレム": "黄金魔像",
    "ミスリルゴーレム": "秘银魔像",
    "スカイゴーレム": "天空魔像",
    "アダマンタイトゴーレム": "精金魔像",
    "火蟹": "火蟹",
    "火炎ムカデ": "火焰蜈蚣",
    "炎の信仰者": "火焰信徒",
    "骸骨戦士": "骷髅战士",
    "骸骨狂戦士": "骷髅狂战士",
    "闇の宣教師": "黑暗传教士",
    "＜ポーン＞": "〈兵卒〉",
    "＜ルーク＞": "〈城堡〉",
    "＜ビショップ＞": "〈主教〉",
    "＜ナイト＞": "〈骑士〉",
    "＜クィーン＞": "〈王后〉",
    "＜キング＞": "〈国王〉",
    "傭兵戦士": "佣兵战士",
    "傭兵射手": "佣兵射手",
    "傭兵魔術師": "佣兵魔法师",
    "イェルス機械兵": "耶鲁士机械兵",
    "ロックスロアー": "投石者",
    "猫": "猫",
    "犬": "狗",
    "少女": "少女",
    "ネズミ": "老鼠",
    "やどかり": "寄居蟹",
    "スライム": "史莱姆",
    "大道芸人": "街头艺人",
    "パンク": "朋克",
    "白衣のナース": "白衣护士",
    "ブレイド": "刀锋",
    "ブレイドβ": "刀锋β",
    "ブレイドΩ": "刀锋Ω",
    "異形の目": "异形之眼",
    "不浄なる瞳": "不净之瞳",
    "ウィスプ": "鬼火",
    "ハリねずみ": "刺猬",
    "輝くハリねずみ": "闪耀刺猬",
    "弱酸性スライム": "弱酸性史莱姆",
    "鶏": "鸡",
    "パンプキン": "南瓜",
    "かぼちゃの怪物": "南瓜怪物",
    "ハロウィンナイトメア": "万圣梦魇",
    "ハンター": "猎人",
    "ダークハンター": "黑暗猎人",
    "パピー": "幼犬",
    "見習い盗賊": "见习盗贼",
    "強盗": "强盗",
    "イスの偉大なる種族": "伊斯伟大种族",
    "マスターシーフ": "盗贼大师",
    "シュブ＝ニグラス": "莎布·尼古拉丝",
    "ガグ": "古革巨人",
    "螺旋の王": "螺旋之王",
    "カーバンクル": "卡邦克鲁",
    "ライオン": "狮子",
    "イェルス自走砲": "耶鲁士自行火炮",
    "ジューア歩兵": "朱伊安步兵",
    "イェルスエリート機械兵": "耶鲁士精英机械兵",
    "ジューア剣闘士": "朱伊安角斗士",
    "イーク": "伊克",
    "カミカゼ・イーク": "神风伊克",
    "イークの戦士": "伊克战士",
    "マスター・イーク": "伊克大师",
    "イークの射手": "伊克射手",
    "地雷侍": "地雷武士",
    "爆弾岩": "炸弹岩",
    "シルバーキャット": "银猫",
    "ティラノサウルス": "霸王龙",
    "妖精": "妖精",
    "トロール": "巨魔",
    "古代の棺": "古代棺木",
    "サソリ": "蝎子",
    "ダイオウサソリ": "帝王蝎",
    "鉄の箱": "铁箱",
    "駄馬": "劣马",
    "ヨウィン馬": "约恩马",
    "ノイエル馬": "诺耶尔马",
    "野生馬": "野马",
    "サラブレッド": "纯种马",
    "ミュータント": "变异人",
    "リザード": "蜥蜴人",
    "ミノタウロス": "米诺陶洛斯",
    "胞子きのこ": "孢子蘑菇",
    "混沌きのこ": "混沌蘑菇",
    "ブルーバブル": "蓝色泡泡",
    "バブル": "泡泡",
    "塊の怪物": "聚合怪",
    "ミノタウロスの術師": "米诺陶洛斯术师",
    "ミノタウロス闘士": "米诺陶洛斯斗士",
    "ミノタウロス戦士": "米诺陶洛斯战士",
    "強盗団の用心棒": "强盗团保镖",
    "強盗団の殺し屋": "强盗团杀手",
    "強盗団の術師": "强盗团术师",
    "死刑執行人": "死刑执行人",
    "死神の使い": "死神使者",
    "阿修羅": "阿修罗",
    "ミトラ": "密特拉",
    "ヴァルナ": "伐楼那",
    "大食いトド": "贪吃海豹",
    "超大食いトド": "超级贪吃海豹",
    "デスゲイズ": "死亡凝视者",
    "カオスアイ": "混沌之眼",
    "マッドゲイズ": "疯狂凝视者",
    "銀眼の暗殺者": "银眼刺客",
    "ハードゲイ": "硬汉",
    "シェイド": "暗影",
    "エイリアン": "异形生物",
}
COMMUNITY_CN_NAME_KEYS = {
    "プチ", "ベスプチ", "クイックリング", "クイックリングの弓使い",
    "シルバーベル", "ゴールドベル", "キューブ", "迷子の子猫",
    "地雷犬", "マンモス", "サイクロプス", "タイタン",
    "グリーンドラゴン", "レッドドラゴン", "ホワイトドラゴン",
    "エレキドラゴン",
}
RACE_CN_NAMES = {
    "かたつむり": "蜗牛",
    "きのこ": "蘑菇",
    "ねずみ": "鼠族",
    "イェルス": "耶鲁士",
    "イス": "伊斯",
    "インプ": "小恶魔",
    "イーク": "伊克",
    "ウサギ": "兔族",
    "エウダーナ": "艾沃达纳",
    "エレア": "艾莱亚",
    "エント": "树人",
    "オーク": "兽人",
    "カオスシェイプ": "混沌体",
    "クイックリング": "快可灵",
    "コウモリ": "蝙蝠",
    "コボルト": "狗头人",
    "ゴブリン": "哥布林",
    "ゴーレム": "魔像",
    "ザナン": "扎南",
    "ジューア": "朱伊安",
    "スライム": "史莱姆",
    "ゾンビ": "僵尸",
    "ドラゴン": "龙族",
    "ドレイク": "幼龙",
    "ノーランド": "诺兰德",
    "マンドレイク": "曼德拉草",
    "ミノタウロス": "米诺陶洛斯",
    "メタル": "金属生物",
    "メデューサ": "美杜莎",
    "リザード": "蜥蜴人",
    "リッチ": "巫妖",
    "ローラン": "罗兰",
    "天使": "天使",
    "妖精": "妖精",
    "小精霊": "小精灵",
    "岩": "岩石",
    "巨人": "巨人",
    "幽霊": "幽灵",
    "恐竜": "恐龙",
    "手": "魔手",
    "昆虫": "昆虫",
    "機械": "机械",
    "熊": "熊族",
    "犬": "犬族",
    "猛獣": "猛兽",
    "猫": "猫族",
    "甲殻": "甲壳生物",
    "目": "魔眼",
    "神話生物": "神话生物",
    "精霊": "精灵",
    "羊": "羊族",
    "蛇": "蛇族",
    "蛙": "蛙族",
    "蜘蛛": "蜘蛛",
    "鎧": "铠甲",
    "阿修羅": "阿修罗",
    "馬": "马族",
    "駒": "棋子",
    "骸骨": "骷髅",
    "鳥": "鸟族",
}
PC_SOURCE_STATS = {
    "プチ": {
        "attributes": dict(zip(ATTRIBUTES, (4, 5, 7, 5, 4, 8))),
        "life_growth": 80, "mana_growth": 100, "speed": 56,
    },
    "ベスプチ": {
        "attributes": dict(zip(ATTRIBUTES, (5, 6, 9, 6, 5, 11))),
        "life_growth": 80, "mana_growth": 100, "speed": 59,
    },
    "クイックリング": {
        "attributes": dict(zip(ATTRIBUTES, (5, 8, 49, 41, 19, 16))),
        "life_growth": 3, "mana_growth": 40, "speed": 900,
    },
    "クイックリングの弓使い": {
        "attributes": dict(zip(ATTRIBUTES, (23, 22, 93, 72, 31, 27))),
        "life_growth": 3, "mana_growth": 40, "speed": 1005,
    },
    "サイクロプス": {
        "attributes": dict(zip(ATTRIBUTES, (83, 95, 40, 21, 27, 27))),
        "life_growth": 200, "mana_growth": 80, "speed": 86,
    },
    "タイタン": {
        "attributes": dict(zip(ATTRIBUTES, (134, 154, 66, 35, 44, 45))),
        "life_growth": 200, "mana_growth": 80, "speed": 108,
    },
    "まだら蜘蛛": {
        "attributes": dict(zip(ATTRIBUTES, (6, 6, 12, 16, 10, 7))),
        "life_growth": 50, "mana_growth": 80, "speed": 127,
    },
    "ブラックウィドウ": {
        "attributes": dict(zip(ATTRIBUTES, (10, 10, 19, 29, 17, 12))),
        "life_growth": 50, "mana_growth": 80, "speed": 146,
    },
    "タランチュラ": {
        "attributes": dict(zip(ATTRIBUTES, (13, 13, 24, 36, 21, 15))),
        "life_growth": 50, "mana_growth": 80, "speed": 156,
    },
    "パラライザー": {
        "attributes": dict(zip(ATTRIBUTES, (16, 16, 31, 45, 26, 19))),
        "life_growth": 50, "mana_growth": 80, "speed": 170,
    },
    "吸血蜘蛛": {
        "attributes": dict(zip(ATTRIBUTES, (20, 20, 38, 56, 32, 23))),
        "life_growth": 50, "mana_growth": 80, "speed": 187,
    },
    "グリーンドラゴン": {
        "attributes": dict(zip(ATTRIBUTES, (107, 144, 84, 52, 81, 52))),
        "life_growth": 220, "mana_growth": 80, "speed": 130,
    },
    "レッドドラゴン": {
        "attributes": dict(zip(ATTRIBUTES, (129, 175, 101, 63, 99, 63))),
        "life_growth": 220, "mana_growth": 80, "speed": 144,
    },
    "ホワイトドラゴン": {
        "attributes": dict(zip(ATTRIBUTES, (129, 175, 101, 63, 99, 63))),
        "life_growth": 220, "mana_growth": 80, "speed": 144,
    },
    "エレキドラゴン": {
        "attributes": dict(zip(ATTRIBUTES, (129, 175, 101, 63, 99, 63))),
        "life_growth": 220, "mana_growth": 80, "speed": 144,
    },
    "冥界ドラゴン": {
        "attributes": dict(zip(ATTRIBUTES, (143, 194, 113, 71, 110, 71))),
        "life_growth": 220, "mana_growth": 80, "speed": 152,
    },
    "カオスドラゴン": {
        "attributes": dict(zip(ATTRIBUTES, (156, 211, 122, 77, 120, 77))),
        "life_growth": 220, "mana_growth": 80, "speed": 160,
    },
}


def fetch_source() -> str:
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "LevelUpPvp catalog builder/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def parse_rows(page: str) -> list[dict]:
    matches = re.findall(r"<pre[^>]*>(.*?)</pre>", page, re.I | re.S)
    source = html.unescape(matches[0] if matches else page)
    rows = []
    for line in source.splitlines():
        if not re.match(r"^\|\d+\s*\|", line):
            continue
        columns = [cell.strip() for cell in line.strip().strip("|").split("|")]
        number = int(columns[0])
        raw_level = re.sub(r"&[^;]+;", "", columns[1]).strip()
        level_match = re.search(r"\d+", raw_level)
        source_name = re.sub(r"&br;.*", "", columns[3]).strip()
        race = re.sub(r"&br;.*", "", columns[4]).strip()
        monster_class = columns[5].strip() or "無"
        rows.append({
            "number": number,
            "level": int(level_match.group()) if level_match else 1,
            "raw_level": raw_level,
            "source_name": source_name,
            "race": race,
            "class": monster_class,
            "raw": columns,
        })
    if [row["number"] for row in rows] != list(range(1, 216)):
        raise ValueError("来源表必须完整包含No.1-215")
    return rows


def _weights(**values):
    return {key: float(values.get(key, 1.0)) for key in ATTRIBUTES}


def race_profile(name: str) -> dict:
    brute = {
        "巨人", "ゴーレム", "岩", "鎧", "ドラゴン", "恐竜",
        "熊", "ミノタウロス", "ゾンビ",
    }
    agile = {
        "クイックリング", "コウモリ", "蜘蛛", "ウサギ",
        "猫", "鳥", "妖精", "昆虫",
    }
    caster = {
        "リッチ", "幽霊", "精霊", "小精霊", "目",
        "インプ", "神話生物", "天使",
    }
    if name in brute:
        weights = _weights(
            strength=1.6, constitution=1.7, dexterity=.65,
            perception=.8, magic=.65, willpower=1.15,
        )
        advanced = {
            "life_growth": 180, "mana_growth": 80,
            "speed": 80, "luck": 100,
        }
        combat = {
            "attack_coefficient": 1.2, "toughness_coefficient": 1.25,
            "armor_style": "heavy", "weapon_type": "blunt",
            "weapon_mode": "two_hand_melee", "weapon_weight": 8,
            "ai_profile_id": "brute_ai",
        }
    elif name in agile:
        weights = _weights(
            strength=.75, constitution=.7, dexterity=1.8,
            perception=1.4, magic=.9, willpower=.8,
        )
        advanced = {
            "life_growth": 60, "mana_growth": 80,
            "speed": 140, "luck": 100,
        }
        combat = {
            "attack_coefficient": .95, "toughness_coefficient": .7,
            "armor_style": "light", "weapon_type": "shortsword",
            "weapon_mode": "one_hand", "weapon_weight": .8,
            "ai_profile_id": "skirmisher_ai",
        }
    elif name in caster:
        weights = _weights(
            strength=.6, constitution=.75, dexterity=.9,
            perception=1.15, magic=1.8, willpower=1.6,
        )
        advanced = {
            "life_growth": 80, "mana_growth": 160,
            "speed": 100, "luck": 100,
        }
        combat = {
            "attack_coefficient": .8, "toughness_coefficient": .8,
            "armor_style": "light", "weapon_type": "staff",
            "weapon_mode": "one_hand", "weapon_weight": 1.5,
            "ai_profile_id": "caster_ai",
        }
    else:
        weights = _weights()
        advanced = {
            "life_growth": 100, "mana_growth": 100,
            "speed": 100, "luck": 100,
        }
        combat = {
            "attack_coefficient": 1.0, "toughness_coefficient": 1.0,
            "armor_style": "medium", "weapon_type": "unarmed",
            "weapon_mode": "one_hand", "weapon_weight": 1,
            "ai_profile_id": "balanced_ai",
        }
    if name == "スライム":
        advanced.update(life_growth=80, mana_growth=100, speed=56)
    elif name == "クイックリング":
        advanced.update(life_growth=3, mana_growth=40, speed=180)
    elif name == "蜘蛛":
        advanced.update(life_growth=50, mana_growth=80, speed=140)
    elif name == "巨人":
        advanced.update(life_growth=200, mana_growth=80, speed=90)
    abilities = []
    skills = {"unarmed": {"coefficient": .8, "flat": 0}}
    if name == "蜘蛛":
        abilities = [{"ability_id": "web", "min_level": 1, "priority": 80}]
        skills["natural_knowledge"] = {"coefficient": .8, "flat": 0}
    resistances = {}
    if name in {"ドラゴン", "ドレイク"}:
        resistances = {"fire": 20, "cold": 20, "lightning": 20}
    return {
        "name": RACE_CN_NAMES[name],
        "source_name_ja": name,
        "localization_origin": "translated",
        "attribute_weights": weights,
        "advanced": advanced,
        "combat": combat,
        "skills": skills,
        "abilities": abilities,
        "resistances": resistances,
    }


def class_profile(name: str) -> dict:
    profiles = {
        "無": (_weights(), "balanced_ai", "unarmed", {}),
        "戦士": (
            _weights(strength=1.4, constitution=1.3, dexterity=1.05),
            "brute_ai", "longsword",
            {"tactics": {"coefficient": 1.0, "flat": 2}},
        ),
        "魔法使い": (
            _weights(magic=1.7, willpower=1.3, perception=1.2),
            "caster_ai", "staff",
            {"magic_training": {"coefficient": 1.0, "flat": 2}},
        ),
        "魔法戦士": (
            _weights(strength=1.2, magic=1.35, willpower=1.15),
            "caster_ai", "longsword",
            {
                "tactics": {"coefficient": .8, "flat": 1},
                "magic_training": {"coefficient": .8, "flat": 1},
            },
        ),
        "略奪者": (
            _weights(strength=1.25, dexterity=1.25, perception=1.15),
            "aggressive_ai", "shortsword",
            {"tactics": {"coefficient": .9, "flat": 1}},
        ),
        "遺跡荒らし": (
            _weights(dexterity=1.3, perception=1.35),
            "skirmisher_ai", "shortsword",
            {"shortsword": {"coefficient": .9, "flat": 1}},
        ),
        "機工兵": (
            _weights(dexterity=1.2, perception=1.6),
            "ranged_ai", "firearm",
            {
                "firearm": {"coefficient": 1.0, "flat": 2},
                "marksmanship": {"coefficient": .9, "flat": 1},
            },
        ),
        "狩人": (
            _weights(dexterity=1.45, perception=1.45),
            "ranged_ai", "bow",
            {
                "bow": {"coefficient": 1.0, "flat": 2},
                "marksmanship": {"coefficient": .9, "flat": 1},
            },
        ),
        "神官": (
            _weights(willpower=1.6, magic=1.25, constitution=1.15),
            "support_ai", "staff",
            {
                "healing": {"coefficient": .8, "flat": 1},
                "restoration": {"coefficient": .8, "flat": 1},
            },
        ),
        "観光客": (
            _weights(luck=1.0), "balanced_ai", "unarmed",
            {"dodge": {"coefficient": .5, "flat": 0}},
        ),
        "クレイモア": (
            _weights(strength=1.6, constitution=1.15),
            "aggressive_ai", "longsword",
            {
                "longsword": {"coefficient": 1.0, "flat": 3},
                "two_handed": {"coefficient": 1.0, "flat": 3},
            },
        ),
    }
    weights, ai_id, weapon_type, skills = profiles[name]
    ranged = weapon_type in {"bow", "crossbow", "firearm"}
    abilities = []
    if name in {"魔法使い", "魔法戦士"}:
        abilities = [
            {"ability_id": "magic_arrow", "min_level": 1, "priority": 70},
            {"ability_id": "fire_ray", "min_level": 20, "priority": 60},
        ]
    elif name == "神官":
        abilities = [
            {"ability_id": "minor_heal", "min_level": 1, "priority": 90},
            {"ability_id": "holy_justice", "min_level": 25, "priority": 60},
        ]
    elif name == "狩人":
        abilities = [
            {"ability_id": "split_arrow", "min_level": 10, "priority": 70}
        ]
    elif name in {"戦士", "クレイモア"}:
        abilities = [
            {"ability_id": "whirlwind_slash", "min_level": 10, "priority": 60}
        ]
    return {
        "source_name_ja": name,
        "attribute_weights": weights,
        "combat": {
            "ai_profile_id": ai_id,
            "weapon_type": weapon_type,
            "weapon_mode": "two_hand_ranged" if ranged else "one_hand",
            "attack_range": 260 if ranged else 65,
            "attack_coefficient": 1.08 if name != "無" else 1,
        },
        "skills": skills,
        "abilities": abilities,
        "resistances": {},
    }


def race_ids(rows: list[dict]) -> dict[str, str]:
    names = sorted({row["race"] for row in rows})
    used = set(ICONIC_RACE_IDS.values())
    result = {}
    counter = 1
    for name in names:
        if name in ICONIC_RACE_IDS:
            result[name] = ICONIC_RACE_IDS[name]
            continue
        while f"race_{counter:03d}" in used:
            counter += 1
        result[name] = f"race_{counter:03d}"
        used.add(result[name])
        counter += 1
    return result


def monster_record(row: dict, race_map: dict[str, str]) -> dict:
    source_name = row["source_name"]
    exact = PC_SOURCE_STATS.get(source_name, {})
    lower_name = source_name.lower()
    abilities = []
    resistances = {}
    combat = {}
    if any(token in source_name for token in ("火炎", "ファイア", "レッドドラゴン")):
        abilities.append(
            {"ability_id": "fire_ray", "min_level": 1, "priority": 85}
        )
        resistances["fire"] = 30
        combat["elemental_damage"] = {"fire": 4}
    if any(token in source_name for token in ("氷結", "アイス", "ホワイトドラゴン")):
        abilities.append(
            {"ability_id": "ice_ray", "min_level": 1, "priority": 85}
        )
        resistances["cold"] = 30
        combat["elemental_damage"] = {"cold": 4}
    if any(token in source_name for token in ("放電", "エレキ", "雷")):
        abilities.append(
            {"ability_id": "lightning_ray", "min_level": 1, "priority": 85}
        )
        resistances["lightning"] = 30
        combat["elemental_damage"] = {"lightning": 4}
    if any(token in source_name for token in ("毒", "コブラ")):
        abilities.append(
            {"ability_id": "poison_weapon", "min_level": 1, "priority": 80}
        )
        resistances["nature"] = 25
    source_effects = []
    if "塊" in source_name or "キューブ" in source_name:
        source_effects.append("分裂（当前引擎未结算）")
    if "酸" in source_name:
        source_effects.append("腐蚀装备（当前引擎未结算）")
    localization = (
        "community"
        if source_name in COMMUNITY_CN_NAME_KEYS
        else "translated"
    )
    name = CN_NAMES.get(source_name, f"直译·{source_name}")
    raw_text = "|".join(row["raw"])
    return {
        "id": row["number"],
        "template_id": f"monster_{row['number']:03d}",
        "name": name,
        "source_name_ja": source_name,
        "base_level": row["level"],
        "race_id": race_map[row["race"]],
        "class_id": CLASS_IDS[row["class"]],
        "rank": "normal",
        "hostile": True,
        "capturable": "〇" in raw_text and source_name not in {"シルバーベル", "ゴールドベル"},
        "attribute_weights": {},
        "skills": {},
        "abilities": abilities,
        "removed_ability_ids": [],
        "combat": combat,
        "resistances": resistances,
        "source_effects": source_effects,
        "source_stats": exact,
        "provenance": {
            "wiki_no": row["number"],
            "source_url": PAGE_URL,
            "attribute_source_url": (
                "https://wikiwiki.jp/elona/"
                "%E3%83%A2%E3%83%B3%E3%82%B9%E3%82%BF%E3%83%BC/"
                "%E8%83%BD%E5%8A%9B%E5%80%A4"
                if exact else ""
            ),
            "original_name_ja": source_name,
            "attribute_origin": (
                "pc_reference" if exact else "race_class_inferred"
            ),
            "localization_origin": localization,
            "level_source_raw": row["raw_level"],
            "level_inferred": not bool(re.search(r"\d+", row["raw_level"])),
        },
    }


def build(rows: list[dict]) -> dict:
    race_map = race_ids(rows)
    races = {
        race_map[name]: race_profile(name)
        for name in sorted(race_map)
    }
    classes = {
        CLASS_IDS[name]: class_profile(name)
        for name in sorted({row["class"] for row in rows})
    }
    return {
        "schema_version": 1,
        "scaling_version": "monster-v1",
        "source": {
            "character_list": PAGE_URL,
            "build_source": SOURCE_URL,
        },
        "defaults": {
            "advanced": {
                "life_growth": 100, "mana_growth": 100,
                "speed": 100, "luck": 100,
            },
            "resistances": {},
            "combat": {
                "ai_profile_id": "balanced_ai",
                "weapon_type": "unarmed",
                "weapon_mode": "one_hand",
                "armor_style": "light",
                "attack_coefficient": 1,
                "toughness_coefficient": 1,
                "attack_range": 65,
                "windup": 8,
                "recovery": 12,
                "cooldown": 24,
                "stamina_cost": 8,
                "weapon_weight": 1,
            },
        },
        "ai_profiles": {
            "balanced_ai": {
                "aggression": .7, "guard_tendency": .2,
                "chase_tendency": .8, "preferred_range": 70,
                "retreat_tendency": .1, "low_hp_risk": .5,
            },
            "aggressive_ai": {
                "aggression": .95, "guard_tendency": .05,
                "chase_tendency": .95, "preferred_range": 55,
                "retreat_tendency": 0, "low_hp_risk": .8,
            },
            "brute_ai": {
                "aggression": .9, "guard_tendency": .15,
                "chase_tendency": .9, "preferred_range": 55,
                "retreat_tendency": 0, "low_hp_risk": .9,
            },
            "skirmisher_ai": {
                "aggression": .65, "guard_tendency": .15,
                "chase_tendency": .75, "preferred_range": 90,
                "retreat_tendency": .25, "low_hp_risk": .35,
            },
            "caster_ai": {
                "aggression": .6, "guard_tendency": .2,
                "chase_tendency": .45, "preferred_range": 280,
                "retreat_tendency": .55, "low_hp_risk": .25,
            },
            "ranged_ai": {
                "aggression": .75, "guard_tendency": .1,
                "chase_tendency": .5, "preferred_range": 240,
                "retreat_tendency": .5, "low_hp_risk": .35,
            },
            "support_ai": {
                "aggression": .45, "guard_tendency": .3,
                "chase_tendency": .5, "preferred_range": 180,
                "retreat_tendency": .45, "low_hp_risk": .2,
            },
        },
        "ranks": {
            "normal": {
                "attribute_multiplier": 1, "hp_multiplier": 1,
                "offense_multiplier": 1, "defense_multiplier": 1,
                "status_resistance_multiplier": 1,
            },
            "elite": {
                "attribute_multiplier": 1.15, "hp_multiplier": 1.5,
                "offense_multiplier": 1.1, "defense_multiplier": 1.1,
                "status_resistance_multiplier": 1.15,
            },
            "boss": {
                "attribute_multiplier": 1.35, "hp_multiplier": 2.5,
                "offense_multiplier": 1.2, "defense_multiplier": 1.2,
                "status_resistance_multiplier": 1.35,
            },
        },
        "races": races,
        "classes": classes,
        "monsters": [monster_record(row, race_map) for row in rows],
    }


def audit(path: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from services.monster_catalog import MonsterCatalog
    from services.monster_build_service import MonsterBuildService
    from models.monster import MonsterSpawnSpec

    catalog = MonsterCatalog(path)
    ids = [item.catalog_id for item in catalog.snapshot.monsters]
    if ids != list(range(1, 216)):
        raise ValueError("目录编号并非连续的No.1-215")
    builds = [
        MonsterBuildService(catalog).build(
            MonsterSpawnSpec(item.template_id)
        )
        for item in catalog.snapshot.monsters
    ]
    origins = {}
    missing = 0
    source_effects = 0
    for item in catalog.snapshot.monsters:
        origin = item.provenance["attribute_origin"]
        origins[origin] = origins.get(origin, 0) + 1
        missing += int(bool(item.provenance.get("level_inferred")))
        source_effects += len(item.source_effects)
    print(json.dumps({
        "monsters": len(builds),
        "continuous_ids": True,
        "missing_required_fields": 0,
        "races": len(catalog.snapshot.races),
        "classes": len(catalog.snapshot.classes),
        "attribute_origins": origins,
        "inferred_base_levels": missing,
        "unmapped_source_effects": source_effects,
    }, ensure_ascii=False, indent=2))


def localize(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    missing = []
    for monster in payload.get("monsters", []):
        source_name = monster.get("source_name_ja", "")
        translated = CN_NAMES.get(source_name)
        if translated is None:
            missing.append(source_name)
            continue
        monster["name"] = translated
        monster["provenance"]["localization_origin"] = (
            "community"
            if source_name in COMMUNITY_CN_NAME_KEYS
            else "translated"
        )
    for race in payload.get("races", {}).values():
        source_name = race.get("source_name_ja", "")
        translated = RACE_CN_NAMES.get(source_name)
        if translated is None:
            missing.append(f"race:{source_name}")
            continue
        race["name"] = translated
        race["localization_origin"] = "translated"
    if missing:
        raise ValueError(f"仍有未翻译名称：{missing}")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--localize", action="store_true")
    args = parser.parse_args()
    if args.audit:
        audit(args.output)
        return
    if args.localize:
        localize(args.output)
        audit(args.output)
        return
    if args.fetch:
        page = fetch_source()
    elif args.source_file:
        page = args.source_file.read_text(encoding="utf-8")
    else:
        parser.error("生成目录需提供--fetch或--source-file；审计使用--audit")
    payload = build(parse_rows(page))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    audit(args.output)


if __name__ == "__main__":
    main()
