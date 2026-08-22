"""Explain persisted battles without mutating combat or progression state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

try:
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
    from models.replay import (
        ReplayFighter,
        ReplayMoment,
        ReplayRecipe,
        ReplaySettlement,
        ReplayTacticPlan,
        ReplayView,
    )
    from services.db import connect_db
    from services.tactic_rules import (
        FAMILY_LABELS,
        PHASE_LABELS,
        CombatPhase,
        TacticFamily,
        TacticPlan,
    )
except ImportError:
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS
    from ..models.replay import (
        ReplayFighter,
        ReplayMoment,
        ReplayRecipe,
        ReplaySettlement,
        ReplayTacticPlan,
        ReplayView,
    )
    from .db import connect_db
    from .tactic_rules import (
        FAMILY_LABELS,
        PHASE_LABELS,
        CombatPhase,
        TacticFamily,
        TacticPlan,
    )


class ReplayAccessDenied(PermissionError):
    """Raised when a requester tries to inspect another group's battle."""


@dataclass(frozen=True, slots=True)
class _Requester:
    group_id: str
    platform: str = ""
    user_id: str = ""
    user_pk: int | None = None


_ENVIRONMENT_LABELS = {
    "calm": "平静",
    "rain": "雨天",
    "fog": "浓雾",
    "strong_wind": "强风",
    "close_quarters": "狭路",
    "mana_tide": "魔力潮汐",
    "ether_interference": "以太干扰",
    "legacy_unknown": "旧版未知环境",
}

_FINISH_LABELS = {
    "knockout": "击倒",
    "status_knockout": "异常状态击倒",
    "double_ko_tiebreak": "双倒裁定",
    "status_double_ko_tiebreak": "异常状态双倒裁定",
    "timeout_score": "超时评分裁定",
    "timeout_remaining_hp": "旧版超时生命裁定",
}

_STATUS_LABELS = {
    "stun": "眩晕",
    "paralysis": "麻痹",
    "confusion": "混乱",
    "haze": "迷雾",
    "blind": "致盲",
    "gravity": "重力",
    "slow": "缓速",
    "sleep": "睡眠",
    "silence": "沉默",
    "fear": "恐惧",
    "root": "定身",
    "snare": "束缚",
}

_CONTROL_STATUSES = frozenset(_STATUS_LABELS)
_DAMAGE_KINDS = frozenset(
    {
        "damage",
        "counter_damage",
        "equipment_proc_damage",
        "summon_strike",
        "status_damage",
        "zone_damage",
        "followup",
        "execute",
    }
)
_SKILL_KINDS = frozenset(
    {
        "attack_windup",
        "spell_cast",
        "spell_concentration",
        "equipment_proc",
        "summon",
        "zone_create",
        "stance",
        "ability_heal",
        "life_steal",
        "mana_barrier",
        "summon_strike",
        "resource_restore",
        "mana_drain",
        "cleanse",
        "dispel",
        "teleport",
        "status_apply",
        "execute",
        "attack",
    }
)
_UTILITY_RESULT_KINDS = frozenset(
    {
        "ability_heal",
        "cleanse",
        "dispel",
        "mana_drain",
        "resource_restore",
        "teleport",
        "status_apply",
    }
)
_UTILITY_RESULT_PRIORITY = (
    "cleanse",
    "dispel",
    "mana_drain",
    "teleport",
    "resource_restore",
    "status_apply",
    "ability_heal",
)


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_value(value: Any) -> tuple[Any, bool]:
    """Return ``(payload, damaged)`` for a persisted JSON value."""

    if isinstance(value, (dict, list)):
        return value, False
    if value is None or value == "":
        return {}, False
    try:
        return json.loads(value), False
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, True


def _json_dict(value: Any) -> tuple[dict[str, Any], bool]:
    payload, damaged = _json_value(value)
    if isinstance(payload, dict):
        return payload, damaged
    return {}, True


def _json_list(value: Any) -> tuple[list[Any], bool]:
    payload, damaged = _json_value(value)
    if isinstance(payload, list):
        return payload, damaged
    return [], True


def _event_dicts(simulation: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    events = simulation.get("events", ())
    if not isinstance(events, list):
        return ()
    return tuple(event for event in events if isinstance(event, dict))


def _normalize_family(value: Any) -> str:
    text = _text(value).lower()
    for family, label in FAMILY_LABELS.items():
        if text in {family.value, family.name.lower(), label.lower()}:
            return family.value
    return "unknown"


def _legacy_plan(strategy: Any) -> ReplayTacticPlan | None:
    text = _text(strategy)
    if not text:
        return None
    plan = TacticPlan.from_legacy(text)
    return ReplayTacticPlan(
        plan.opening.value,
        plan.midgame.value,
        plan.endgame.value,
        "legacy_strategy",
    )


def _plan_from_sources(
    raw_plan: Any,
    events: tuple[dict[str, Any], ...],
    actor_pk: int,
    legacy_strategy: Any,
) -> tuple[ReplayTacticPlan, bool]:
    payload, damaged = _json_dict(raw_plan)
    values = {
        phase: _normalize_family(payload.get(phase))
        for phase in ("opening", "midgame", "endgame")
    }
    source = "battle_row"

    for event in events:
        if (
            _text(event.get("kind")) == "strategy_trigger"
            and _optional_int(event.get("actor_pk")) == actor_pk
        ):
            phase = _text(event.get("skill_id")).lower()
            if phase in values and values[phase] == "unknown":
                values[phase] = _normalize_family(event.get("status_id"))
                source = "simulation_events"

    legacy = _legacy_plan(legacy_strategy)
    if legacy is not None:
        legacy_values = dict(
            zip(("opening", "midgame", "endgame"), legacy.as_tuple())
        )
        recovered = False
        for phase, value in values.items():
            if value == "unknown":
                values[phase] = legacy_values[phase]
                recovered = True
        if recovered:
            source = (
                "legacy_strategy"
                if all(value == legacy_values[phase] for phase, value in values.items())
                else "mixed_legacy_fallback"
            )

    if all(value == "unknown" for value in values.values()):
        source = "missing"

    return ReplayTacticPlan(
        values["opening"],
        values["midgame"],
        values["endgame"],
        source,
    ), damaged


def _status_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for status in value:
        if isinstance(status, dict):
            status_id = _text(status.get("status_id"))
        else:
            status_id = _text(status)
        if status_id:
            result.append(status_id)
    return tuple(result)


def _fighter(
    side: str,
    row: Mapping[str, Any],
    simulation: Mapping[str, Any],
) -> ReplayFighter:
    snapshot = simulation.get(side)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    user_pk = _int(
        snapshot.get("user_pk"),
        _int(row.get(f"{side}_pk")),
    )
    name = _text(
        snapshot.get("name"),
        _text(row.get(f"{side}_nickname"), f"玩家#{user_pk}"),
    )
    return ReplayFighter(
        side=side,
        user_pk=user_pk,
        name=name,
        level=max(0, _int(snapshot.get("level"))),
        remaining_hp=_optional_int(simulation.get(f"{side}_remaining_hp")),
        max_hp=_optional_int(snapshot.get("max_hp")),
        remaining_mana=_optional_int(
            simulation.get(f"{side}_remaining_mana")
        ),
        max_mana=_optional_int(snapshot.get("max_mp")),
        remaining_stamina=_optional_int(
            simulation.get(f"{side}_remaining_stamina")
        ),
        max_stamina=_optional_int(snapshot.get("max_sp")),
        damage_dealt=max(0, _int(simulation.get(f"{side}_damage_dealt"))),
        final_statuses=_status_ids(
            simulation.get(f"{side}_final_statuses")
        ),
    )


def _name_for(pk: int | None, names: Mapping[int, str]) -> str:
    if pk is None:
        return "未知角色"
    return names.get(pk, f"玩家#{pk}")


def _moment(
    category: str,
    event: Mapping[str, Any],
    summary: str,
) -> ReplayMoment:
    return ReplayMoment(
        category=category,
        tick=max(0, _int(event.get("tick"))),
        kind=_text(event.get("kind"), "unknown"),
        summary=summary,
        actor_pk=_optional_int(event.get("actor_pk")),
        target_pk=_optional_int(event.get("target_pk")),
        value=max(0, _int(event.get("value"))),
        skill_id=_text(event.get("skill_id")),
        status_id=_text(event.get("status_id")),
    )


def _ability_name(skill_id: str) -> str:
    definition = ACTIVE_ABILITY_DEFINITIONS.get(skill_id)
    return definition.name if definition else skill_id or "特殊能力"


def _is_utility_result(event: Mapping[str, Any]) -> bool:
    kind = _text(event.get("kind"))
    if kind not in _UTILITY_RESULT_KINDS:
        return False
    # Hostile control already receives a richer, dedicated turning point.
    return not (
        kind == "status_apply"
        and _text(event.get("status_id")) in _CONTROL_STATUSES
    )


def _utility_summary(
    event: Mapping[str, Any],
    names: Mapping[int, str],
) -> str:
    kind = _text(event.get("kind"))
    actor_pk = _optional_int(event.get("actor_pk"))
    target_pk = _optional_int(event.get("target_pk"))
    actor = _name_for(actor_pk, names)
    target = _name_for(target_pk, names)
    value = max(0, _int(event.get("value")))
    skill_id = _text(event.get("skill_id"))
    ability = _ability_name(skill_id)
    if kind == "cleanse":
        return f"{actor}借{ability}净化{target}的{value}个异常状态"
    if kind == "dispel":
        return f"{actor}借{ability}驱散{target}的{value}项增益或召唤效果"
    if kind == "mana_drain":
        return f"{actor}借{ability}从{target}吸取{value}点魔力"
    if kind == "resource_restore":
        resource = "体力" if _text(event.get("status_id")) == "sp" else "魔力"
        return f"{actor}借{ability}恢复{value}点{resource}"
    if kind == "teleport":
        return f"{actor}借{ability}完成{value}距离的位移"
    if kind == "ability_heal":
        return f"{actor}借{ability}为{target}恢复{value}点生命"
    if kind == "status_apply":
        status_id = _text(event.get("status_id"), "特殊状态")
        recipient = "自身" if actor_pk == target_pk else target
        return f"{actor}借{ability}使{recipient}获得{status_id}状态"
    return f"{actor}使用{ability}并产生效果"


def _turning_points(
    events: tuple[dict[str, Any], ...],
    names: Mapping[int, str],
    duration_ticks: int,
    winner_pk: int,
    loser_pk: int,
    finish_reason: str,
) -> tuple[ReplayMoment, ...]:
    points: list[ReplayMoment] = []

    first_skill = next(
        (
            event
            for event in events
            if _text(event.get("kind")) in _SKILL_KINDS
            and _text(event.get("skill_id"))
            and _text(event.get("skill_id")) != "basic_attack"
        ),
        None,
    )
    if first_skill is not None:
        actor_pk = _optional_int(first_skill.get("actor_pk"))
        skill_id = _text(first_skill.get("skill_id"))
        first_utility = next(
            (
                event
                for kind in _UTILITY_RESULT_PRIORITY
                for event in events
                if _is_utility_result(event)
                and _text(event.get("kind")) == kind
                and _text(event.get("skill_id")) == skill_id
                and _optional_int(event.get("actor_pk")) == actor_pk
                and _int(event.get("tick")) >= _int(first_skill.get("tick"))
            ),
            None,
        )
        points.append(
            _moment(
                "first_skill",
                first_utility or first_skill,
                (
                    _utility_summary(first_utility, names)
                    if first_utility is not None
                    else f"{_name_for(actor_pk, names)}率先使用 {_ability_name(skill_id)}"
                ),
            )
        )
    else:
        first_utility = None

    first_control = next(
        (
            event
            for event in events
            if _text(event.get("kind")) == "status_apply"
            and _text(event.get("status_id")) in _CONTROL_STATUSES
        ),
        None,
    )
    if first_control is not None:
        actor_pk = _optional_int(first_control.get("actor_pk"))
        target_pk = _optional_int(first_control.get("target_pk"))
        status_id = _text(first_control.get("status_id"))
        ability = _ability_name(_text(first_control.get("skill_id")))
        points.append(
            _moment(
                "first_control",
                first_control,
                f"{_name_for(actor_pk, names)}借{ability}令"
                f"{_name_for(target_pk, names)}陷入"
                f"{_STATUS_LABELS.get(status_id, status_id)}",
            )
        )

    later_utility = next(
        (
            event
            for kind in _UTILITY_RESULT_PRIORITY
            for event in events
            if _is_utility_result(event)
            and _text(event.get("kind")) == kind
            and not (
                first_utility is not None
                and _optional_int(event.get("actor_pk"))
                == _optional_int(first_utility.get("actor_pk"))
                and _text(event.get("skill_id"))
                == _text(first_utility.get("skill_id"))
            )
        ),
        None,
    )
    if later_utility is not None:
        points.append(
            _moment(
                "utility",
                later_utility,
                _utility_summary(later_utility, names),
            )
        )

    fortune = next(
        (
            event
            for event in events
            if _text(event.get("kind")) == "fortune_swing"
        ),
        None,
    )
    if fortune is not None:
        actor_pk = _optional_int(fortune.get("actor_pk"))
        reason = _text(fortune.get("status_id"), "命运改写")
        points.append(
            _moment(
                "fortune",
                fortune,
                f"{_name_for(actor_pk, names)}触发幸运转机（{reason}）",
            )
        )

    damage_events = [
        event
        for event in events
        if _text(event.get("kind")) in _DAMAGE_KINDS
        and _int(event.get("value")) > 0
        and _optional_int(event.get("actor_pk"))
        != _optional_int(event.get("target_pk"))
    ]
    if damage_events:
        largest = max(
            damage_events,
            key=lambda event: (_int(event.get("value")), -_int(event.get("tick"))),
        )
        actor_pk = _optional_int(largest.get("actor_pk"))
        target_pk = _optional_int(largest.get("target_pk"))
        value = _int(largest.get("value"))
        skill_suffix = (
            f"（{_text(largest.get('skill_id'))}）"
            if _text(largest.get("skill_id"))
            else ""
        )
        points.append(
            _moment(
                "largest_hit",
                largest,
                f"{_name_for(actor_pk, names)}对"
                f"{_name_for(target_pk, names)}造成最大单击 {value}"
                f"{skill_suffix}",
            )
        )

    endgame = next(
        (
            event
            for event in events
            if _text(event.get("kind")) == "strategy_trigger"
            and _text(event.get("skill_id")) == "endgame"
        ),
        None,
    )
    if endgame is not None:
        actor_pk = _optional_int(endgame.get("actor_pk"))
        family = _text(endgame.get("status_id"), "unknown")
        points.append(
            _moment(
                "endgame",
                endgame,
                f"进入终局，{_name_for(actor_pk, names)}采用"
                f"{_family_label(family)}",
            )
        )
    else:
        points.append(
            ReplayMoment(
                "endgame",
                max(0, duration_ticks),
                "phase_absent",
                "战斗在终局战术触发前结束",
            )
        )

    finish_event = next(
        (
            event
            for event in reversed(events)
            if _text(event.get("kind")) in {"knockout", "timeout"}
        ),
        None,
    )
    finish_summary = (
        f"{_name_for(winner_pk, names)}击败{_name_for(loser_pk, names)}"
        if "timeout" not in finish_reason
        else f"超时评分判定{_name_for(winner_pk, names)}获胜"
    )
    if finish_event is None:
        finish_event = {
            "tick": duration_ticks,
            "kind": finish_reason or "finish",
            "actor_pk": winner_pk,
            "target_pk": loser_pk,
        }
    points.append(_moment("finish", finish_event, finish_summary))
    return tuple(points)


def _family_label(value: str) -> str:
    try:
        return FAMILY_LABELS[TacticFamily(value)]
    except (KeyError, ValueError):
        return "未知战术"


def _plan_text(plan: ReplayTacticPlan) -> str:
    return "→".join(_family_label(value) for value in plan.as_tuple())


def _resource_text(value: int | None, maximum: int | None) -> str:
    if value is None:
        return "?"
    if maximum is None:
        return str(value)
    return f"{value}/{maximum}"


def _requester(value: Any) -> _Requester:
    if isinstance(value, str):
        return _Requester(group_id=value.strip())
    return _Requester(
        group_id=_text(getattr(value, "group_id", "")),
        platform=_text(getattr(value, "platform", "")),
        user_id=_text(getattr(value, "user_id", "")),
        user_pk=_optional_int(getattr(value, "id", None)),
    )


class ReplayService:
    """Read and explain one persisted battle as a stable replay view."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def get_latest_replay(self, requester: Any) -> ReplayView | None:
        return await self.get_replay(requester)

    async def get_replay_by_id(
        self,
        requester: Any,
        battle_id: int,
    ) -> ReplayView | None:
        return await self.get_replay(requester, battle_id=battle_id)

    async def get_replay(
        self,
        requester: Any,
        battle_id: int | None = None,
    ) -> ReplayView | None:
        identity = _requester(requester)
        if not identity.group_id and not identity.user_id and identity.user_pk is None:
            raise ValueError("复盘查询者缺少群或用户身份")
        if battle_id is not None and _int(battle_id) <= 0:
            raise ValueError("battle_id 必须是正整数")

        async with await connect_db(self.db_path) as db:
            columns_cursor = await db.execute("PRAGMA table_info(battles)")
            battle_columns = {
                row["name"] for row in await columns_cursor.fetchall()
            }
            await columns_cursor.close()

            group_parts = []
            if "group_id" in battle_columns:
                group_parts.append("NULLIF(b.group_id, '')")
            group_parts.extend(
                ("NULLIF(ua.group_id, '')", "NULLIF(ud.group_id, '')", "''")
            )
            group_expression = f"COALESCE({', '.join(group_parts)})"
            select_sql = f"""
                SELECT b.*,
                       {group_expression} AS resolved_group_id,
                       ua.nickname AS attacker_nickname,
                       ua.platform AS attacker_platform,
                       ua.user_id AS attacker_user_id,
                       ua.group_id AS attacker_group_id,
                       ud.nickname AS defender_nickname,
                       ud.platform AS defender_platform,
                       ud.user_id AS defender_user_id,
                       ud.group_id AS defender_group_id
                FROM battles b
                LEFT JOIN users ua ON ua.id = b.attacker_pk
                LEFT JOIN users ud ON ud.id = b.defender_pk
            """
            if battle_id is None:
                if not identity.group_id:
                    raise ValueError("查看最近复盘需要群身份")
                cursor = await db.execute(
                    select_sql
                    + f" WHERE {group_expression} = ?"
                    + " ORDER BY b.created_at_ts DESC, b.id DESC LIMIT 1",
                    (identity.group_id,),
                )
            else:
                cursor = await db.execute(
                    select_sql + " WHERE b.id = ? LIMIT 1",
                    (_int(battle_id),),
                )
            row = await cursor.fetchone()
            await cursor.close()

        if row is None:
            return None
        payload = _row_dict(row)
        if not self._can_view(identity, payload):
            raise ReplayAccessDenied("只能查看自己参战或本群发生的战斗")
        return self._build_view(payload)

    @staticmethod
    def _can_view(identity: _Requester, row: Mapping[str, Any]) -> bool:
        battle_group = _text(row.get("resolved_group_id"), _text(row.get("group_id")))
        same_group = bool(identity.group_id and identity.group_id == battle_group)
        same_pk = identity.user_pk is not None and identity.user_pk in {
            _optional_int(row.get("attacker_pk")),
            _optional_int(row.get("defender_pk")),
        }
        participant = False
        if identity.user_id:
            for side in ("attacker", "defender"):
                if identity.user_id != _text(row.get(f"{side}_user_id")):
                    continue
                stored_platform = _text(row.get(f"{side}_platform"))
                if not identity.platform or not stored_platform or identity.platform == stored_platform:
                    participant = True
                    break
        return same_group or same_pk or participant

    @staticmethod
    def _build_view(row: Mapping[str, Any]) -> ReplayView:
        notes: list[str] = []
        simulation, damaged_simulation = _json_dict(row.get("simulation_json"))
        if damaged_simulation:
            notes.append("simulation_json 已损坏，已用战斗表字段降级复盘")
        elif not simulation:
            notes.append("旧版记录没有逐刻模拟数据")

        events = _event_dicts(simulation)
        attacker = _fighter("attacker", row, simulation)
        defender = _fighter("defender", row, simulation)
        attacker_pk = attacker.user_pk or _int(row.get("attacker_pk"))
        defender_pk = defender.user_pk or _int(row.get("defender_pk"))

        strategy_payload, damaged_strategy = _json_dict(row.get("strategy"))
        if damaged_strategy and _text(row.get("strategy")):
            strategy_payload = {"attacker": row.get("strategy")}
        attacker_plan, damaged_attacker_plan = _plan_from_sources(
            row.get("attacker_tactic_plan_json"),
            events,
            attacker_pk,
            strategy_payload.get("attacker"),
        )
        defender_plan, damaged_defender_plan = _plan_from_sources(
            row.get("defender_tactic_plan_json"),
            events,
            defender_pk,
            strategy_payload.get("defender"),
        )
        if damaged_attacker_plan or damaged_defender_plan:
            notes.append("战术方案 JSON 已损坏，已从模拟事件或旧策略恢复")
        if not attacker_plan.complete or not defender_plan.complete:
            notes.append("旧版记录缺少完整三阶段战术")

        winner_pk = _int(simulation.get("winner_pk"), _int(row.get("winner_pk")))
        loser_pk = _int(simulation.get("loser_pk"), _int(row.get("loser_pk")))
        ruleset_id = _text(
            simulation.get("ruleset_id"),
            _text(row.get("ruleset_id"), "legacy-v1"),
        )
        engine_version = _text(
            simulation.get("engine_version"),
            _text(row.get("engine_version"), ruleset_id),
        )
        random_seed = _optional_int(row.get("random_seed"))
        if random_seed is None:
            random_seed = _optional_int(simulation.get("random_seed"))
        environment_id = _text(
            simulation.get("environment_id"),
            _text(row.get("environment_id"), "legacy_unknown"),
        )
        duration_ticks = max(
            0,
            _int(
                simulation.get("duration_ticks"),
                _int(row.get("duration_ticks")),
            ),
        )
        finish_reason = _text(
            simulation.get("finish_reason"),
            _text(row.get("finish_reason"), "legacy_unknown"),
        )

        winner_exp_gain = max(0, _int(row.get("winner_exp_gain")))
        loser_exp_gain = max(0, _int(row.get("loser_exp_gain")))
        loser_exp_loss = max(0, _int(row.get("loser_exp_loss")))
        attacker_won = attacker_pk == winner_pk
        settlement = ReplaySettlement(
            rated=bool(_int(row.get("rated"))),
            reward_reason=_text(row.get("reward_reason")),
            winner_exp_gain=winner_exp_gain,
            loser_exp_gain=loser_exp_gain,
            loser_exp_loss=loser_exp_loss,
            attacker_exp_delta=(
                winner_exp_gain if attacker_won else loser_exp_gain - loser_exp_loss
            ),
            defender_exp_delta=(
                loser_exp_gain - loser_exp_loss if attacker_won else winner_exp_gain
            ),
            attacker_rating_before=_optional_float(row.get("attacker_rating_before")),
            attacker_rating_after=_optional_float(row.get("attacker_rating_after")),
            defender_rating_before=_optional_float(row.get("defender_rating_before")),
            defender_rating_after=_optional_float(row.get("defender_rating_after")),
        )

        names = {attacker_pk: attacker.name, defender_pk: defender.name}
        moments = _turning_points(
            events,
            names,
            duration_ticks,
            winner_pk,
            loser_pk,
            finish_reason,
        )
        attacker_snapshot = simulation.get("attacker")
        defender_snapshot = simulation.get("defender")
        attacker_snapshot = attacker_snapshot if isinstance(attacker_snapshot, dict) else {}
        defender_snapshot = defender_snapshot if isinstance(defender_snapshot, dict) else {}
        missing_inputs: list[str] = []
        for missing, value in (
            ("random_seed", random_seed),
            ("ruleset_id", ruleset_id if ruleset_id != "legacy-v1" else None),
            ("environment_id", environment_id if environment_id != "legacy_unknown" else None),
            ("attacker_snapshot", attacker_snapshot or None),
            ("defender_snapshot", defender_snapshot or None),
            ("attacker_tactic_plan", attacker_plan.complete or None),
            ("defender_tactic_plan", defender_plan.complete or None),
        ):
            if value is None:
                missing_inputs.append(missing)
        audit_complete = not missing_inputs
        # The persisted inputs are sufficient for auditing, but this project
        # does not yet ship a snapshot deserializer/ReplayRunner.  Do not label
        # a JSON recipe as executable when no public API can actually run it.
        reproducible = False
        execution_blockers = ("replay_runner_unavailable",)
        command_payload = {
            "call": "SideviewCombatEngine.simulate",
            "ruleset_id": ruleset_id,
            "engine_version": engine_version,
            "kwargs": {
                "random_seed": random_seed,
                "environment_id": environment_id,
                "attacker_tactic_plan": list(attacker_plan.as_tuple()),
                "defender_tactic_plan": list(defender_plan.as_tuple()),
            },
            "attacker_snapshot": attacker_snapshot,
            "defender_snapshot": defender_snapshot,
        }
        recipe = ReplayRecipe(
            engine_call="SideviewCombatEngine.simulate",
            ruleset_id=ruleset_id,
            engine_version=engine_version,
            random_seed=random_seed,
            environment_id=environment_id,
            attacker_snapshot=attacker_snapshot,
            defender_snapshot=defender_snapshot,
            attacker_tactic_plan=attacker_plan,
            defender_tactic_plan=defender_plan,
            audit_complete=audit_complete,
            reproducible=reproducible,
            missing_inputs=tuple(missing_inputs),
            execution_blockers=execution_blockers,
            command_info=json.dumps(
                command_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        if not audit_complete:
            notes.append("该旧记录缺少完整复现输入：" + ", ".join(missing_inputs))
        else:
            notes.append("审计参数完整；当前版本仅回放已保存事件，不重新执行引擎")

        return ReplayView(
            battle_id=max(0, _int(row.get("id"))),
            group_id=_text(row.get("resolved_group_id"), _text(row.get("group_id"))),
            created_at=_text(row.get("created_at")),
            attacker=attacker,
            defender=defender,
            winner_pk=winner_pk,
            loser_pk=loser_pk,
            ruleset_id=ruleset_id,
            engine_version=engine_version,
            random_seed=random_seed,
            environment_id=environment_id,
            duration_ticks=duration_ticks,
            finish_reason=finish_reason,
            attacker_tactic_plan=attacker_plan,
            defender_tactic_plan=defender_plan,
            turning_points=moments,
            settlement=settlement,
            recipe=recipe,
            compatibility_notes=tuple(dict.fromkeys(notes)),
        )

    @staticmethod
    def format_replay(view: ReplayView) -> str:
        return format_replay(view)


def format_replay(view: ReplayView) -> str:
    """Format a compact Chinese replay; never emits the huge snapshot JSON."""

    mode = "排位" if view.settlement.rated else "切磋"
    environment = _ENVIRONMENT_LABELS.get(view.environment_id, view.environment_id)
    finish = _FINISH_LABELS.get(view.finish_reason, view.finish_reason or "未知")
    lines = [
        f"【战斗复盘 #{view.battle_id}】{view.attacker.name} Lv.{view.attacker.level} "
        f"vs {view.defender.name} Lv.{view.defender.level}",
        f"结果：{view.winner.name}获胜｜{mode}｜{finish}｜{view.duration_ticks} 刻",
        f"规则：{view.ruleset_id}｜种子 {view.random_seed if view.random_seed is not None else '?'}｜环境 {environment}",
        f"战术：{view.attacker.name} {_plan_text(view.attacker_tactic_plan)}；"
        f"{view.defender.name} {_plan_text(view.defender_tactic_plan)}",
    ]
    if view.turning_points:
        lines.append("关键转折：")
        lines.extend(
            f"- T{moment.tick} {moment.summary}"
            for moment in view.turning_points
        )
    lines.append(
        f"终场：{view.attacker.name} HP {_resource_text(view.attacker.remaining_hp, view.attacker.max_hp)}"
        f" / MP {_resource_text(view.attacker.remaining_mana, view.attacker.max_mana)}"
        f" / SP {_resource_text(view.attacker.remaining_stamina, view.attacker.max_stamina)}；"
        f"{view.defender.name} HP {_resource_text(view.defender.remaining_hp, view.defender.max_hp)}"
        f" / MP {_resource_text(view.defender.remaining_mana, view.defender.max_mana)}"
        f" / SP {_resource_text(view.defender.remaining_stamina, view.defender.max_stamina)}"
    )
    settlement_line = (
        f"结算：{view.attacker.name} EXP {view.settlement.attacker_exp_delta:+d}；"
        f"{view.defender.name} EXP {view.settlement.defender_exp_delta:+d}"
    )
    if view.settlement.rated:
        attacker_delta = view.settlement.attacker_rating_delta
        defender_delta = view.settlement.defender_rating_delta
        settlement_line += (
            f"｜评级 {view.attacker.name} "
            f"{attacker_delta:+.1f}" if attacker_delta is not None else "｜评级数据缺失"
        )
        if attacker_delta is not None and defender_delta is not None:
            settlement_line += f"，{view.defender.name} {defender_delta:+.1f}"
    lines.append(settlement_line)
    if view.recipe.audit_complete:
        lines.append(
            "审计参数："
            f"ruleset={view.recipe.ruleset_id} seed={view.recipe.random_seed} "
            f"environment={view.recipe.environment_id} battle_id={view.battle_id}"
        )
    else:
        lines.append("审计参数：旧记录不完整（" + ", ".join(view.recipe.missing_inputs) + "）")
    if view.compatibility_notes:
        lines.append("兼容提示：" + "；".join(view.compatibility_notes))
    return "\n".join(lines)


__all__ = ["ReplayAccessDenied", "ReplayService", "format_replay"]
