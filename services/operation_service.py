"""Deterministic daily, weekly, and seasonal operation rules for QQ groups.

The public generation methods are pure.  Persistence is limited to progress,
claim reservations, and season windows; reward settlement intentionally lives
outside this service.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Iterable

try:
    from ..models.operation import (
        BossAffix,
        BossEncounter,
        ClaimResult,
        DailyNefia,
        EnvironmentRule,
        OperationEffect,
        OperationOverview,
        OperationPeriods,
        OperationProgress,
        OperationTask,
        OperationTaskState,
        PeriodWindow,
        ProgressUpdate,
        RewardIntent,
        RiskChoice,
        RiskEvent,
        SeasonSummary,
        WeeklySimulationResult,
        operation_reward_definition,
        operation_reward_intent,
        stable_operation_seed,
    )
    from .combat_random import KeyedEntropy
    from .db import connect_db
except ImportError:
    from models.operation import (
        BossAffix,
        BossEncounter,
        ClaimResult,
        DailyNefia,
        EnvironmentRule,
        OperationEffect,
        OperationOverview,
        OperationPeriods,
        OperationProgress,
        OperationTask,
        OperationTaskState,
        PeriodWindow,
        ProgressUpdate,
        RewardIntent,
        RiskChoice,
        RiskEvent,
        SeasonSummary,
        WeeklySimulationResult,
        operation_reward_definition,
        operation_reward_intent,
        stable_operation_seed,
    )
    from services.combat_random import KeyedEntropy
    from services.db import connect_db


DEFAULT_RULESET_ID = "sideview-v11"
DEFAULT_RESET_HOUR = 4
SEASON_DAYS = 28
WEEKLY_TASK_COUNT = 7
WEEKLY_TASKS_REQUIRED = 5
DAILY_TASK_COUNT = 3
DAILY_TASKS_REQUIRED = 2
WEEKLY_SIMULATION_LIMIT = 4
WEEKLY_SIMULATION_BEST_COUNT = 2
WEEKLY_SIMULATION_SCORE_CAP = 1_000_000
_SEASON_EPOCH = date(2026, 1, 5)  # Monday, aligned with the PvP season code.
_EVENT_KEYS_METADATA = "_event_keys"
CURRENTLY_RECORDABLE_EVENT_TYPES = frozenset(
    {
        "pvp_battle",
        "unique_opponent",
        "active_skill",
        "combat_endgame",
        "battle_review",
        "workshop_action",
        "stance_unique",
        "close_fight",
        "daily_reward",
        "battle_win",
        "fortune_trigger",
        "spell_cast",
        "guard_action",
        "environment_unique",
        "nefia_node",
        "risk_choice",
        "boss_attempt",
        "nefia_boss_clear",
        "risk_choice_unique",
    }
)


def _effect(
    key: str,
    label: str,
    magnitude: float,
    unit: str,
    cap: float,
    applies_to: str = "both",
) -> OperationEffect:
    return OperationEffect(key, label, magnitude, unit, cap, applies_to)


ENVIRONMENTS: tuple[EnvironmentRule, ...] = (
    EnvironmentRule(
        "ember_foundry",
        "余烬铸场",
        "地面持续升温，强攻更凶但恢复受抑。",
        (
            _effect("fire_damage", "火焰伤害", 0.12, "ratio", 0.15),
            _effect("healing", "治疗效果", -0.10, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "whispering_fog",
        "低语浓雾",
        "远处轮廓难辨，近身控制更容易延续。",
        (
            _effect("ranged_accuracy", "远程命中", -6, "percentage_point", 10),
            _effect("control_duration", "控制时长", 0.10, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "narrow_fortress",
        "狭城回廊",
        "走位空间收紧，近战压迫与护甲价值上升。",
        (
            _effect("melee_damage", "近战伤害", 0.08, "ratio", 0.15),
            _effect("movement_speed", "移动速度", -0.10, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "ether_storm",
        "以太风暴",
        "魔力被放大，也会让每次施法付出更多。",
        (
            _effect("spell_damage", "法术伤害", 0.12, "ratio", 0.15),
            _effect("spell_cost", "法术消耗", 0.10, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "thorn_maze",
        "荆棘迷林",
        "掩体鼓励游击，长时间硬顶会被荆棘拖垮。",
        (
            _effect("evasion", "回避", 6, "percentage_point", 10),
            _effect("regeneration", "持续恢复", -0.08, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "frozen_clock",
        "冻时洞窟",
        "行动变慢，蓄出的重击却更加危险。",
        (
            _effect("action_speed", "行动速度", -0.10, "ratio", 0.15),
            _effect("critical_damage", "暴击伤害", 0.12, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "gravity_well",
        "重力祭坛",
        "击退近乎失效，站稳脚跟者得到额外防护。",
        (
            _effect("forced_movement", "强制位移", -8, "percentage_point", 10),
            _effect("armor", "护甲", 0.10, "ratio", 0.15),
        ),
    ),
    EnvironmentRule(
        "lucky_ruins",
        "幸运遗迹",
        "古老骰子仍在转动：更容易出奇迹，但不会一击定胜负。",
        (
            _effect("critical_chance", "暴击率", 4, "percentage_point", 6),
            _effect("damage_variance", "伤害浮动", 0.08, "ratio", 0.10),
        ),
    ),
)


def _choice(
    choice_id: str,
    title: str,
    description: str,
    risk: str,
    reward_multiplier: float,
    *effects: OperationEffect,
) -> RiskChoice:
    return RiskChoice(
        choice_id,
        title,
        description,
        risk,
        reward_multiplier,
        tuple(effects),
    )


RISK_EVENTS: tuple[RiskEvent, ...] = (
    RiskEvent(
        "sealed_shrine",
        "封印神龛",
        "石门后传来心跳般的回声。",
        (
            _choice("pray", "低声祈祷", "稳妥换取短暂护佑。", "低", 1.00,
                    _effect("shield", "开场护盾", 0.08, "ratio", 0.12, "player")),
            _choice("break", "砸开封印", "惊醒守卫，战利品也会增加。", "高", 1.20,
                    _effect("enemy_damage", "敌方伤害", 0.10, "ratio", 0.15, "enemy")),
        ),
    ),
    RiskEvent(
        "hungry_chest",
        "饥饿宝箱",
        "宝箱长着牙，并礼貌地等你伸手。",
        (
            _choice("feed", "投喂口粮", "减少下一战消耗。", "低", 0.95,
                    _effect("resource_cost", "资源消耗", -0.10, "ratio", 0.15, "player")),
            _choice("wrestle", "徒手夺宝", "负伤风险换更高掉落。", "中", 1.15,
                    _effect("starting_hp", "开场生命", -0.10, "ratio", 0.15, "player")),
        ),
    ),
    RiskEvent(
        "mirror_oracle",
        "镜中先知",
        "镜子愿意展示一个未来，但只展示一半。",
        (
            _choice("future", "看见胜利", "首个主动技更稳定。", "低", 1.00,
                    _effect("first_skill_accuracy", "首技命中", 6, "percentage_point", 10, "player")),
            _choice("crack", "打碎未来", "放弃稳定，换取爆发。", "中", 1.12,
                    _effect("critical_damage", "暴击伤害", 0.12, "ratio", 0.15, "player")),
        ),
    ),
    RiskEvent(
        "ether_well",
        "以太井",
        "井水同时泛着蓝光与不祥的紫光。",
        (
            _choice("sip", "浅尝一口", "恢复少量资源。", "低", 1.00,
                    _effect("resource_restore", "资源恢复", 0.10, "ratio", 0.15, "player")),
            _choice("drink", "一饮而尽", "法术增幅，但更容易受控。", "高", 1.20,
                    _effect("spell_damage", "法术伤害", 0.12, "ratio", 0.15, "player"),
                    _effect("status_resistance", "异常抗性", -6, "percentage_point", 10, "player")),
        ),
    ),
    RiskEvent(
        "sleeping_caravan",
        "沉睡商队",
        "无人看守的货车停在岔路中央。",
        (
            _choice("guard", "替他们守夜", "获得祝福，不拿额外货物。", "低", 1.00,
                    _effect("healing", "治疗效果", 0.08, "ratio", 0.15, "player")),
            _choice("borrow", "借走一箱", "掉落增加，精英会闻迹追来。", "中", 1.15,
                    _effect("elite_hp", "精英生命", 0.12, "ratio", 0.15, "enemy")),
        ),
    ),
    RiskEvent(
        "singing_bridge",
        "会唱歌的桥",
        "每走一步，桥面就要求你跟上节拍。",
        (
            _choice("slow", "稳步合拍", "降低速度换取命中。", "低", 1.00,
                    _effect("action_speed", "行动速度", -0.06, "ratio", 0.15, "player"),
                    _effect("accuracy", "命中", 5, "percentage_point", 10, "player")),
            _choice("dance", "即兴狂舞", "速度提升，但容易露出破绽。", "中", 1.12,
                    _effect("action_speed", "行动速度", 0.10, "ratio", 0.15, "player"),
                    _effect("armor", "护甲", -0.08, "ratio", 0.15, "player")),
        ),
    ),
    RiskEvent(
        "wounded_rival",
        "负伤的对手",
        "昨日的敌人靠在墙边，武器已经断裂。",
        (
            _choice("aid", "留下药剂", "首领战获得一次容错。", "低", 0.95,
                    _effect("fortune_guard", "厄运保护", 1, "charge", 1, "player")),
            _choice("duel", "坚持决斗", "多打一场，战利品增加。", "中", 1.18,
                    _effect("starting_sp", "开场精力", -0.10, "ratio", 0.15, "player")),
        ),
    ),
    RiskEvent(
        "cursed_dice",
        "诅咒骰桌",
        "桌上的六面骰每一面都写着“再来一次”。",
        (
            _choice("leave", "收起骰子", "不触发额外波动。", "低", 1.00),
            _choice("roll", "掷下骰子", "双方暴击率上升，奖励同步增加。", "高", 1.20,
                    _effect("critical_chance", "双方暴击率", 5, "percentage_point", 6)),
        ),
    ),
)


BOSS_AFFIXES: tuple[BossAffix, ...] = (
    BossAffix("bloodthirst", "嗜血", "生命低于一半后攻击泛红。", (
        _effect("low_hp_damage", "残血伤害", 0.12, "ratio", 0.15, "enemy"),)),
    BossAffix("bulwark", "壁垒", "每隔一段时间展开可打破的护盾。", (
        _effect("periodic_shield", "周期护盾", 0.12, "ratio", 0.15, "enemy"),)),
    BossAffix("swift", "迅捷", "行动条闪烁，出手间隔缩短。", (
        _effect("action_speed", "行动速度", 0.12, "ratio", 0.15, "enemy"),)),
    BossAffix("stormbound", "缚雷", "蓄力动作后落下可预判的雷击。", (
        _effect("lightning_damage", "雷击伤害", 0.12, "ratio", 0.15, "enemy"),)),
    BossAffix("mirror_skin", "镜肤", "首次高伤害会被削弱并反射光芒。", (
        _effect("single_hit_reduction", "单击减伤", 0.15, "ratio", 0.15, "enemy"),)),
    BossAffix("regrowth", "再生", "未受压制时缓慢恢复。", (
        _effect("regeneration", "持续恢复", 0.08, "ratio", 0.10, "enemy"),)),
    BossAffix("gravity", "沉重", "近身攻击带来短暂减速。", (
        _effect("on_hit_slow", "命中减速", 0.10, "ratio", 0.15, "enemy"),)),
    BossAffix("unstable", "不稳定", "受击会累积能量，满层后爆散。", (
        _effect("burst_damage", "蓄能爆发", 0.12, "ratio", 0.15, "enemy"),)),
    BossAffix("hexed", "咒缚", "控制失败时会提高下一次施加概率。", (
        _effect("status_chance", "异常概率", 6, "percentage_point", 10, "enemy"),)),
    BossAffix("duelist", "决斗者", "连续命中会积累精准，失手即清空。", (
        _effect("accuracy", "叠层命中", 6, "percentage_point", 10, "enemy"),)),
)


_BOSSES: tuple[tuple[str, str], ...] = (
    ("clockwork_minotaur", "发条牛头王"),
    ("ether_witch", "以太织咒者"),
    ("moss_colossus", "苔冠巨像"),
    ("glass_dragon", "镜砂幼龙"),
    ("void_jester", "虚空弄臣"),
    ("iron_choir", "铁铸圣歌团"),
)


DAILY_TASK_CATALOG: tuple[OperationTask, ...] = (
    OperationTask("pvp_participate", "试一套打法", "完成1场切磋或排位。", 1, "pvp_battle"),
    OperationTask("unique_opponent", "新面孔", "与1名今日尚未交手的玩家对战。", 1, "unique_opponent"),
    OperationTask("nefia_nodes", "深入异变", "完成异变奈菲亚的2个节点。", 2, "nefia_node"),
    OperationTask("risk_choice", "自己做决定", "完成1次风险二选一事件。", 1, "risk_choice"),
    OperationTask("boss_attempt", "敲门者", "挑战1次今日精英或首领。", 1, "boss_attempt"),
    OperationTask("active_skills", "招式热身", "在有效战斗中使用3次主动技能。", 3, "active_skill"),
    OperationTask("survive_endgame", "撑到终局", "进入1次战斗终局阶段。", 1, "combat_endgame"),
    OperationTask("review", "看一眼复盘", "查看1份战斗复盘。", 1, "battle_review"),
    OperationTask("workshop", "工坊清理", "分解或重铸1件装备。", 1, "workshop_action"),
)


WEEKLY_TASK_CATALOG: tuple[OperationTask, ...] = (
    OperationTask("daily_rewards", "规律冒险", "完成3天的每日任选委托。", 3, "daily_reward"),
    OperationTask("unique_opponents", "群内巡礼", "与5名不同玩家完成有效对战。", 5, "unique_opponent"),
    OperationTask("nefia_bosses", "异变清扫", "击败3次每日异变首领。", 3, "nefia_boss_clear"),
    OperationTask("risk_variety", "不同的答案", "选择4种不同的风险选项。", 4, "risk_choice_unique"),
    OperationTask("pvp_battles", "战术实验", "完成8场有效PVP。", 8, "pvp_battle"),
    OperationTask("battle_reviews", "复盘习惯", "查看2份不同战斗复盘。", 2, "battle_review"),
    OperationTask("workshop_actions", "修修补补", "完成3次工坊操作。", 3, "workshop_action"),
    OperationTask("stance_variety", "换个姿势", "使用4种不同战术姿态完成行动。", 4, "stance_unique"),
    OperationTask("close_fights", "险胜或惜败", "完成2场终局血量差不超过15%的战斗。", 2, "close_fight"),
    OperationTask("battle_wins", "赢得漂亮", "在有效对战中获胜3次。", 3, "battle_win"),
    OperationTask("fortune_turns", "命运转身", "在有效对战中触发2次奇运逆转。", 2, "fortune_trigger"),
    OperationTask("spell_casts", "咒文练习", "在有效对战中施放8次法术。", 8, "spell_cast"),
    OperationTask("guard_actions", "稳住阵脚", "在有效对战中防御8次。", 8, "guard_action"),
    OperationTask("environment_tour", "风土巡礼", "在3种不同环境中完成有效对战。", 3, "environment_unique"),
)


class OperationService:
    """Generate shared operations and persist player-local progress safely."""

    def __init__(
        self,
        db_path: str,
        ruleset_id: str = DEFAULT_RULESET_ID,
        reset_hour: int = DEFAULT_RESET_HOUR,
    ) -> None:
        ruleset_id = str(ruleset_id).strip()
        if not ruleset_id or ruleset_id.casefold() == "latest":
            raise ValueError("operation ruleset_id must be a concrete version")
        if not 0 <= int(reset_hour) <= 23:
            raise ValueError("reset_hour must be within 0-23")
        self.db_path = db_path
        self.ruleset_id = ruleset_id
        self.reset_hour = int(reset_hour)

    def periods(self, now: datetime | None = None) -> OperationPeriods:
        current = now or datetime.now()
        operational_date = (current - timedelta(hours=self.reset_hour)).date()
        daily_start = self._at_reset(operational_date, current)
        week_date = operational_date - timedelta(days=operational_date.weekday())
        weekly_start = self._at_reset(week_date, current)
        season_index = (operational_date - _SEASON_EPOCH).days // SEASON_DAYS
        season_date = _SEASON_EPOCH + timedelta(days=season_index * SEASON_DAYS)
        season_start = self._at_reset(season_date, current)
        generation = "v11" if "v11" in self.ruleset_id.casefold() else self.ruleset_id
        return OperationPeriods(
            daily=PeriodWindow(
                "daily",
                operational_date.isoformat(),
                int(daily_start.timestamp()),
                int((daily_start + timedelta(days=1)).timestamp()),
            ),
            weekly=PeriodWindow(
                "weekly",
                week_date.isoformat(),
                int(weekly_start.timestamp()),
                int((weekly_start + timedelta(days=7)).timestamp()),
            ),
            season=PeriodWindow(
                "season",
                f"{season_date.isoformat()}-{generation}",
                int(season_start.timestamp()),
                int((season_start + timedelta(days=SEASON_DAYS)).timestamp()),
            ),
        )

    def daily_nefia(
        self,
        group_id: str,
        now: datetime | None = None,
    ) -> DailyNefia:
        group = str(group_id)
        daily_key = self.periods(now).daily.key
        # This is intentionally the exact shared coordinate promised by the UI.
        group_seed = stable_operation_seed(group, daily_key, self.ruleset_id)
        entropy = KeyedEntropy(self.ruleset_id, group_seed)
        environment = entropy.choice(ENVIRONMENTS, stream="operation.environment")
        risk_event = entropy.choice(RISK_EVENTS, stream="operation.risk_event")
        boss_id, boss_name = entropy.choice(_BOSSES, stream="operation.boss")
        ranked_affixes = sorted(
            BOSS_AFFIXES,
            key=lambda affix: entropy.random(
                stream="operation.boss_affix",
                actor=affix.affix_id,
            ),
        )
        boss = BossEncounter(boss_id, boss_name, tuple(ranked_affixes[:2]))
        return DailyNefia(
            group,
            self.ruleset_id,
            daily_key,
            group_seed,
            environment,
            risk_event,
            boss,
        )

    def daily_tasks(
        self,
        group_id: str,
        now: datetime | None = None,
    ) -> tuple[OperationTask, ...]:
        period = self.periods(now).daily
        return self._recordable_ranked_tasks(
            DAILY_TASK_CATALOG,
            group_id,
            period.key,
            "operation.daily_tasks",
            count=DAILY_TASK_COUNT,
            minimum_recordable=DAILY_TASKS_REQUIRED,
        )

    def weekly_tasks(
        self,
        group_id: str,
        now: datetime | None = None,
    ) -> tuple[OperationTask, ...]:
        period = self.periods(now).weekly
        return self._recordable_ranked_tasks(
            WEEKLY_TASK_CATALOG,
            group_id,
            period.key,
            "operation.weekly_tasks",
            count=WEEKLY_TASK_COUNT,
            minimum_recordable=WEEKLY_TASKS_REQUIRED,
        )

    async def advance_daily_task(
        self,
        *,
        user_pk: int,
        group_id: str,
        task_id: str,
        event_key: str,
        amount: int = 1,
        now: datetime | None = None,
    ) -> ProgressUpdate:
        task = self._task_by_id(self.daily_tasks(group_id, now), task_id)
        period = self.periods(now).daily
        return await self.update_progress(
            user_pk=user_pk,
            group_id=group_id,
            period_kind=period.kind,
            period_key=period.key,
            operation_key=self._task_key("daily", task.task_id),
            target=task.target,
            event_key=event_key,
            amount=amount,
            now=now,
        )

    async def advance_weekly_task(
        self,
        *,
        user_pk: int,
        group_id: str,
        task_id: str,
        event_key: str,
        amount: int = 1,
        now: datetime | None = None,
    ) -> ProgressUpdate:
        task = self._task_by_id(self.weekly_tasks(group_id, now), task_id)
        period = self.periods(now).weekly
        return await self.update_progress(
            user_pk=user_pk,
            group_id=group_id,
            period_kind=period.kind,
            period_key=period.key,
            operation_key=self._task_key("weekly", task.task_id),
            target=task.target,
            event_key=event_key,
            amount=amount,
            now=now,
        )

    async def record_event(
        self,
        *,
        user_pk: int,
        group_id: str,
        event_type: str,
        event_key: str,
        amount: int = 1,
        now: datetime | None = None,
    ) -> tuple[ProgressUpdate, ...]:
        """Advance every active daily/weekly task matching one domain event.

        Callers name only the stable event type, not today's randomly selected
        task IDs.  The same ``event_key`` is intentionally stored in every
        matching progress row, so a retry converges even if the process stopped
        between the daily and weekly updates.
        """

        requested_type = str(event_type).strip()
        requested_key = str(event_key).strip()
        if not requested_type:
            raise ValueError("event_type must not be empty")
        known_event_types = {
            task.event_type
            for task in (*DAILY_TASK_CATALOG, *WEEKLY_TASK_CATALOG)
        }
        if requested_type not in known_event_types:
            raise ValueError("event_type is not present in the operation catalog")
        if not requested_key or len(requested_key) > 200:
            raise ValueError("event_key must contain 1-200 characters")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")

        current = now or datetime.now()
        periods = self.periods(current)
        updates: list[ProgressUpdate] = []
        for period_kind, tasks in (
            ("daily", self.daily_tasks(group_id, current)),
            ("weekly", self.weekly_tasks(group_id, current)),
        ):
            for task in tasks:
                if task.event_type != requested_type:
                    continue
                period = getattr(periods, period_kind)
                updates.append(
                    await self.update_progress(
                        user_pk=user_pk,
                        group_id=group_id,
                        period_kind=period.kind,
                        period_key=period.key,
                        operation_key=self._task_key(period_kind, task.task_id),
                        target=task.target,
                        event_key=requested_key,
                        amount=amount,
                        now=current,
                    )
                )
        return tuple(updates)

    async def update_progress(
        self,
        *,
        user_pk: int,
        group_id: str,
        period_kind: str,
        period_key: str,
        operation_key: str,
        target: int,
        event_key: str,
        amount: int = 1,
        metadata: dict | None = None,
        now: datetime | None = None,
    ) -> ProgressUpdate:
        """Apply one uniquely keyed progress event in an atomic transaction."""

        self._validate_progress_args(
            user_pk,
            period_kind,
            period_key,
            operation_key,
            target,
            event_key,
            amount,
        )
        timestamp = int((now or datetime.now()).timestamp())
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._progress_row_in_db(
                    db,
                    user_pk,
                    str(group_id),
                    period_kind,
                    period_key,
                    operation_key,
                )
                if row is None:
                    stored_metadata = dict(metadata or {})
                    stored_metadata[_EVENT_KEYS_METADATA] = [event_key]
                    new_progress = min(int(target), int(amount))
                    await db.execute(
                        """
                        INSERT INTO operation_progress (
                            user_pk, group_id, period_kind, period_key,
                            operation_key, progress, target, completed, claimed,
                            metadata_json, updated_at_ts
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            user_pk,
                            str(group_id),
                            period_kind,
                            period_key,
                            operation_key,
                            new_progress,
                            target,
                            int(new_progress >= target),
                            self._encode_metadata(stored_metadata),
                            timestamp,
                        ),
                    )
                    applied = amount > 0
                else:
                    record = self._progress_from_row(row)
                    if record.target != target:
                        raise ValueError("operation target changed inside one period")
                    event_keys = list(record.metadata.get(_EVENT_KEYS_METADATA, []))
                    if record.completed or event_key in event_keys or amount == 0:
                        await db.commit()
                        return ProgressUpdate(record, False)
                    event_keys.append(event_key)
                    stored_metadata = dict(record.metadata)
                    stored_metadata.update(metadata or {})
                    stored_metadata[_EVENT_KEYS_METADATA] = event_keys
                    new_progress = min(target, record.progress + amount)
                    await db.execute(
                        """
                        UPDATE operation_progress
                        SET progress = ?, completed = ?, metadata_json = ?,
                            updated_at_ts = ?
                        WHERE user_pk = ? AND group_id = ? AND period_kind = ?
                          AND period_key = ? AND operation_key = ?
                        """,
                        (
                            new_progress,
                            int(new_progress >= target),
                            self._encode_metadata(stored_metadata),
                            timestamp,
                            user_pk,
                            str(group_id),
                            period_kind,
                            period_key,
                            operation_key,
                        ),
                    )
                    applied = True
                row = await self._progress_row_in_db(
                    db,
                    user_pk,
                    str(group_id),
                    period_kind,
                    period_key,
                    operation_key,
                )
                await db.commit()
                return ProgressUpdate(self._progress_from_row(row), applied)
            except Exception:
                await db.rollback()
                raise

    async def claim_daily_reward(
        self,
        *,
        user_pk: int,
        group_id: str,
        now: datetime | None = None,
    ) -> ClaimResult:
        period = self.periods(now).daily
        tasks = self.daily_tasks(group_id, now)
        reward = self._daily_reward_intent(user_pk, group_id, period.key)
        return await self._claim_bundle(
            user_pk=user_pk,
            group_id=group_id,
            period=period,
            operation_keys=tuple(self._task_key("daily", task.task_id) for task in tasks),
            bundle_key="daily:choice-two",
            required=DAILY_TASKS_REQUIRED,
            reward=reward,
            now=now,
        )

    async def claim_weekly_reward(
        self,
        *,
        user_pk: int,
        group_id: str,
        now: datetime | None = None,
    ) -> ClaimResult:
        period = self.periods(now).weekly
        tasks = self.weekly_tasks(group_id, now)
        reward = self._weekly_reward_intent(user_pk, group_id, period.key)
        return await self._claim_bundle(
            user_pk=user_pk,
            group_id=group_id,
            period=period,
            operation_keys=tuple(self._task_key("weekly", task.task_id) for task in tasks),
            bundle_key="weekly:five-of-seven",
            required=WEEKLY_TASKS_REQUIRED,
            reward=reward,
            now=now,
        )

    async def record_weekly_simulation(
        self,
        *,
        user_pk: int,
        group_id: str,
        submission_key: str,
        score: int,
        now: datetime | None = None,
    ) -> WeeklySimulationResult:
        """Store at most four unique submissions and score only the best two."""

        submission = str(submission_key).strip()
        if not submission or len(submission) > 160:
            raise ValueError("submission_key must contain 1-160 characters")
        if isinstance(score, bool) or not isinstance(score, int):
            raise TypeError("simulation score must be an integer")
        if not 0 <= score <= WEEKLY_SIMULATION_SCORE_CAP:
            raise ValueError(
                f"simulation score must be within 0-{WEEKLY_SIMULATION_SCORE_CAP}"
            )
        period = self.periods(now).weekly
        operation_key = f"weekly_simulation:{submission}"
        timestamp = int((now or datetime.now()).timestamp())
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await self._simulation_rows_in_db(
                    db, user_pk, str(group_id), period.key
                )
                existing = next(
                    (row for row in rows if row["operation_key"] == operation_key),
                    None,
                )
                if existing is not None:
                    stored_score = self._score_from_row(existing)
                    result = self._simulation_result(
                        rows,
                        accepted=True,
                        duplicate=True,
                        submitted_score=stored_score,
                    )
                    await db.commit()
                    return result
                if len(rows) >= WEEKLY_SIMULATION_LIMIT:
                    result = self._simulation_result(
                        rows,
                        accepted=False,
                        duplicate=False,
                        submitted_score=score,
                    )
                    await db.commit()
                    return result
                await db.execute(
                    """
                    INSERT INTO operation_progress (
                        user_pk, group_id, period_kind, period_key,
                        operation_key, progress, target, completed, claimed,
                        metadata_json, updated_at_ts
                    ) VALUES (?, ?, 'weekly', ?, ?, 1, 1, 1, 0, ?, ?)
                    """,
                    (
                        user_pk,
                        str(group_id),
                        period.key,
                        operation_key,
                        self._encode_metadata(
                            {"submission_key": submission, "score": score}
                        ),
                        timestamp,
                    ),
                )
                rows = await self._simulation_rows_in_db(
                    db, user_pk, str(group_id), period.key
                )
                await db.commit()
                return self._simulation_result(
                    rows,
                    accepted=True,
                    duplicate=False,
                    submitted_score=score,
                )
            except Exception:
                await db.rollback()
                raise

    async def overview(
        self,
        *,
        user_pk: int,
        group_id: str,
        now: datetime | None = None,
    ) -> OperationOverview:
        periods = self.periods(now)
        season = await self.ensure_season(
            user_pk=user_pk,
            group_id=group_id,
            now=now,
        )
        daily_tasks = self.daily_tasks(group_id, now)
        weekly_tasks = self.weekly_tasks(group_id, now)
        daily_keys = {self._task_key("daily", task.task_id) for task in daily_tasks}
        weekly_keys = {self._task_key("weekly", task.task_id) for task in weekly_tasks}
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT * FROM operation_progress
                WHERE user_pk = ? AND group_id = ?
                  AND ((period_kind = 'daily' AND period_key = ?)
                    OR (period_kind = 'weekly' AND period_key = ?))
                """,
                (user_pk, str(group_id), periods.daily.key, periods.weekly.key),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            simulation_rows = [
                row for row in rows
                if row["period_kind"] == "weekly"
                and row["operation_key"].startswith("weekly_simulation:")
            ]
        daily_completed = sum(
            bool(row["completed"])
            for row in rows
            if row["period_kind"] == "daily"
            and row["operation_key"] in daily_keys
        )
        weekly_completed = sum(
            bool(row["completed"])
            for row in rows
            if row["period_kind"] == "weekly"
            and row["operation_key"] in weekly_keys
        )
        daily_claimed = any(
            row["period_kind"] == "daily"
            and row["operation_key"] == "daily:choice-two"
            and bool(row["claimed"])
            for row in rows
        )
        weekly_claimed = any(
            row["period_kind"] == "weekly"
            and row["operation_key"] == "weekly:five-of-seven"
            and bool(row["claimed"])
            for row in rows
        )
        progress_rows = {
            (str(row["period_kind"]), str(row["operation_key"])): row
            for row in rows
        }

        def task_states(
            period_kind: str,
            tasks: tuple[OperationTask, ...],
        ) -> tuple[OperationTaskState, ...]:
            states = []
            for task in tasks:
                operation_key = self._task_key(period_kind, task.task_id)
                row = progress_rows.get((period_kind, operation_key))
                progress = 0 if row is None else int(row["progress"])
                states.append(
                    OperationTaskState(
                        task.task_id,
                        min(task.target, max(0, progress)),
                        task.target,
                        bool(row is not None and row["completed"]),
                    )
                )
            return tuple(states)

        return OperationOverview(
            periods,
            self.daily_nefia(group_id, now),
            daily_tasks,
            daily_completed,
            daily_claimed,
            weekly_tasks,
            weekly_completed,
            weekly_claimed,
            self._simulation_result(
                simulation_rows,
                accepted=len(simulation_rows) < WEEKLY_SIMULATION_LIMIT,
                duplicate=False,
                submitted_score=0,
            ),
            season,
            task_states("daily", daily_tasks),
            task_states("weekly", weekly_tasks),
        )

    async def ensure_season(
        self,
        *,
        user_pk: int,
        group_id: str,
        now: datetime | None = None,
    ) -> SeasonSummary:
        current = now or datetime.now()
        period = self.periods(current).season
        now_ts = int(current.timestamp())
        now_text = current.isoformat(timespec="seconds")
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE seasons SET status = 'completed', updated_at = ?
                    WHERE group_id = ? AND status = 'active' AND end_at_ts <= ?
                    """,
                    (now_text, str(group_id), now_ts),
                )
                await db.execute(
                    """
                    INSERT INTO seasons (
                        group_id, season_key, ruleset_id, start_at_ts, end_at_ts,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(group_id, season_key) DO UPDATE SET
                        status = 'active', updated_at = excluded.updated_at
                    """,
                    (
                        str(group_id),
                        period.key,
                        self.ruleset_id,
                        period.start_at_ts,
                        period.end_at_ts,
                        now_text,
                        now_text,
                    ),
                )
                cursor = await db.execute(
                    """
                    SELECT id, status FROM seasons
                    WHERE group_id = ? AND season_key = ?
                    """,
                    (str(group_id), period.key),
                )
                season_row = await cursor.fetchone()
                await cursor.close()
                cursor = await db.execute(
                    """
                    SELECT rating, games, wins, losses FROM season_users
                    WHERE season_id = ? AND user_pk = ?
                    """,
                    (season_row["id"], user_pk),
                )
                user_row = await cursor.fetchone()
                await cursor.close()
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        operational_date = (current - timedelta(hours=self.reset_hour)).date()
        season_index = (operational_date - _SEASON_EPOCH).days // SEASON_DAYS
        season_start_date = _SEASON_EPOCH + timedelta(
            days=season_index * SEASON_DAYS
        )
        day_number = max(
            1,
            min(SEASON_DAYS, (operational_date - season_start_date).days + 1),
        )
        return SeasonSummary(
            int(season_row["id"]),
            period.key,
            str(season_row["status"]),
            day_number,
            SEASON_DAYS,
            None if user_row is None else round(float(user_row["rating"])),
            0 if user_row is None else int(user_row["games"]),
            0 if user_row is None else int(user_row["wins"]),
            0 if user_row is None else int(user_row["losses"]),
        )

    def player_drop_seed(
        self,
        group_id: str,
        user_pk: int | str,
        now: datetime | None = None,
    ) -> int:
        return self.daily_nefia(group_id, now).drop_seed_for(user_pk)

    def _at_reset(self, value: date, current: datetime) -> datetime:
        return datetime(
            value.year,
            value.month,
            value.day,
            self.reset_hour,
            tzinfo=current.tzinfo,
        )

    def _ranked_tasks(
        self,
        catalog: tuple[OperationTask, ...],
        group_id: str,
        period_key: str,
        stream: str,
        count: int,
    ) -> tuple[OperationTask, ...]:
        seed = stable_operation_seed(str(group_id), period_key, self.ruleset_id)
        entropy = KeyedEntropy(self.ruleset_id, seed)
        ranked = sorted(
            catalog,
            key=lambda task: entropy.random(stream=stream, actor=task.task_id),
        )
        return tuple(ranked[:count])

    def _recordable_ranked_tasks(
        self,
        catalog: tuple[OperationTask, ...],
        group_id: str,
        period_key: str,
        stream: str,
        *,
        count: int,
        minimum_recordable: int,
    ) -> tuple[OperationTask, ...]:
        """Preserve seeded ranking while keeping the claim threshold reachable."""

        if not 0 <= minimum_recordable <= count <= len(catalog):
            raise ValueError("invalid constrained operation task counts")
        ranked = self._ranked_tasks(
            catalog,
            group_id,
            period_key,
            stream,
            len(catalog),
        )
        recordable = tuple(
            task
            for task in ranked
            if task.event_type in CURRENTLY_RECORDABLE_EVENT_TYPES
        )
        if len(recordable) < max(count, minimum_recordable):
            raise RuntimeError("operation catalog lacks enough recordable tasks")
        # Entries whose production event does not exist yet remain useful as
        # future Nefia design data, but are never shown as an impossible task.
        return recordable[:count]

    @staticmethod
    def _task_by_id(
        tasks: Iterable[OperationTask], task_id: str
    ) -> OperationTask:
        requested = str(task_id).strip()
        for task in tasks:
            if task.task_id == requested:
                return task
        raise ValueError("task is not active in the requested operation period")

    @staticmethod
    def _task_key(period_kind: str, task_id: str) -> str:
        return f"{period_kind}_task:{task_id}"

    @staticmethod
    def _encode_metadata(metadata: dict) -> str:
        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode_metadata(raw: str) -> dict:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _progress_from_row(self, row) -> OperationProgress:
        if row is None:
            raise ValueError("operation progress row does not exist")
        return OperationProgress(
            int(row["user_pk"]),
            str(row["group_id"]),
            str(row["period_kind"]),
            str(row["period_key"]),
            str(row["operation_key"]),
            int(row["progress"]),
            int(row["target"]),
            bool(row["completed"]),
            bool(row["claimed"]),
            self._decode_metadata(row["metadata_json"]),
        )

    async def _progress_row_in_db(
        self,
        db,
        user_pk: int,
        group_id: str,
        period_kind: str,
        period_key: str,
        operation_key: str,
    ):
        cursor = await db.execute(
            """
            SELECT * FROM operation_progress
            WHERE user_pk = ? AND group_id = ? AND period_kind = ?
              AND period_key = ? AND operation_key = ?
            """,
            (user_pk, group_id, period_kind, period_key, operation_key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @staticmethod
    def _validate_progress_args(
        user_pk: int,
        period_kind: str,
        period_key: str,
        operation_key: str,
        target: int,
        event_key: str,
        amount: int,
    ) -> None:
        if isinstance(user_pk, bool) or not isinstance(user_pk, int) or user_pk <= 0:
            raise ValueError("user_pk must be a positive integer")
        if period_kind not in {"daily", "weekly", "season"}:
            raise ValueError("invalid operation period kind")
        for label, value in (
            ("period_key", period_key),
            ("operation_key", operation_key),
            ("event_key", event_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be empty")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ValueError("target must be a positive integer")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("amount must be a non-negative integer")

    async def _claim_bundle(
        self,
        *,
        user_pk: int,
        group_id: str,
        period: PeriodWindow,
        operation_keys: tuple[str, ...],
        bundle_key: str,
        required: int,
        reward: RewardIntent,
        now: datetime | None,
    ) -> ClaimResult:
        timestamp = int((now or datetime.now()).timestamp())
        definition = operation_reward_definition(period.kind)
        if bundle_key != definition.bundle_key:
            raise ValueError("operation reward bundle does not match its period")
        placeholders = ",".join("?" for _ in operation_keys)
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    SELECT COUNT(*) AS completed_count
                    FROM operation_progress
                    WHERE user_pk = ? AND group_id = ? AND period_kind = ?
                      AND period_key = ? AND operation_key IN ({placeholders})
                      AND completed = 1
                    """,
                    (
                        user_pk,
                        str(group_id),
                        period.kind,
                        period.key,
                        *operation_keys,
                    ),
                )
                completed_count = int((await cursor.fetchone())["completed_count"])
                await cursor.close()
                await db.execute(
                    """
                    INSERT INTO operation_progress (
                        user_pk, group_id, period_kind, period_key,
                        operation_key, progress, target, completed, claimed,
                        metadata_json, updated_at_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(
                        user_pk, group_id, period_kind, period_key, operation_key
                    ) DO UPDATE SET
                        progress = excluded.progress,
                        target = excluded.target,
                        completed = excluded.completed,
                        metadata_json = excluded.metadata_json,
                        updated_at_ts = excluded.updated_at_ts
                    WHERE operation_progress.claimed = 0
                    """,
                    (
                        user_pk,
                        str(group_id),
                        period.kind,
                        period.key,
                        bundle_key,
                        min(completed_count, required),
                        required,
                        int(completed_count >= required),
                        self._encode_metadata(
                            {
                                "task_keys": list(operation_keys),
                                "ruleset_id": self.ruleset_id,
                            }
                        ),
                        timestamp,
                    ),
                )
                bundle_row = await self._progress_row_in_db(
                    db,
                    user_pk,
                    str(group_id),
                    period.kind,
                    period.key,
                    bundle_key,
                )
                already_claimed = bool(bundle_row["claimed"])
                # A claimed row is a durable reward reservation.  Even if a
                # ruleset deploy changes today's random task selection before
                # settlement retries, that reservation remains eligible and
                # continues to describe the original immutable intent.
                eligible = completed_count >= required or already_claimed
                if already_claimed:
                    completed_count = max(
                        completed_count,
                        int(bundle_row["progress"]),
                    )
                reserved_reward = reward
                bundle_metadata = self._decode_metadata(
                    bundle_row["metadata_json"]
                )
                reserved_ruleset = bundle_metadata.get("ruleset_id")
                if isinstance(reserved_ruleset, str) and reserved_ruleset.strip():
                    reserved_reward = operation_reward_intent(
                        period_kind=period.kind,
                        user_pk=user_pk,
                        group_id=str(group_id),
                        period_key=period.key,
                        ruleset_id=reserved_ruleset,
                    )
                granted = eligible and not already_claimed
                if granted:
                    await db.execute(
                        """
                        UPDATE operation_progress SET claimed = 1, updated_at_ts = ?
                        WHERE user_pk = ? AND group_id = ? AND period_kind = ?
                          AND period_key = ? AND operation_key = ? AND claimed = 0
                        """,
                        (
                            timestamp,
                            user_pk,
                            str(group_id),
                            period.kind,
                            period.key,
                            bundle_key,
                        ),
                    )
                await db.commit()
                return ClaimResult(
                    eligible,
                    granted,
                    already_claimed,
                    completed_count,
                    required,
                    # Returning the stable intent for an already-reserved
                    # eligible claim lets a separate idempotent settlement
                    # retry after a crash.  The reward key prevents any second
                    # grant; withholding it here could permanently strand a
                    # player's reward between the two transactions.
                    reserved_reward if eligible else None,
                )
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def _score_from_row(row) -> int:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            return max(0, int(metadata.get("score", 0)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0

    async def _simulation_rows_in_db(
        self, db, user_pk: int, group_id: str, weekly_key: str
    ) -> list:
        cursor = await db.execute(
            """
            SELECT operation_key, metadata_json FROM operation_progress
            WHERE user_pk = ? AND group_id = ? AND period_kind = 'weekly'
              AND period_key = ? AND operation_key LIKE 'weekly_simulation:%'
            """,
            (user_pk, group_id, weekly_key),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    def _simulation_result(
        self,
        rows: Iterable,
        *,
        accepted: bool,
        duplicate: bool,
        submitted_score: int,
    ) -> WeeklySimulationResult:
        row_list = list(rows)
        scores = sorted(
            (self._score_from_row(row) for row in row_list),
            reverse=True,
        )[:WEEKLY_SIMULATION_BEST_COUNT]
        return WeeklySimulationResult(
            accepted,
            duplicate,
            len(row_list),
            WEEKLY_SIMULATION_LIMIT,
            submitted_score,
            tuple(scores),
        )

    def _daily_reward_intent(
        self, user_pk: int, group_id: str, daily_key: str
    ) -> RewardIntent:
        return operation_reward_intent(
            period_kind="daily",
            user_pk=user_pk,
            group_id=str(group_id),
            period_key=daily_key,
            ruleset_id=self.ruleset_id,
        )

    def _weekly_reward_intent(
        self, user_pk: int, group_id: str, weekly_key: str
    ) -> RewardIntent:
        return operation_reward_intent(
            period_kind="weekly",
            user_pk=user_pk,
            group_id=str(group_id),
            period_key=weekly_key,
            ruleset_id=self.ruleset_id,
        )


__all__ = [
    "BOSS_AFFIXES",
    "CURRENTLY_RECORDABLE_EVENT_TYPES",
    "DAILY_TASK_CATALOG",
    "ENVIRONMENTS",
    "OperationService",
    "RISK_EVENTS",
    "WEEKLY_TASK_CATALOG",
    "WEEKLY_SIMULATION_SCORE_CAP",
]
