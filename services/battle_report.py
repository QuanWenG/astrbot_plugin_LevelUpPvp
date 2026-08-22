try:
    from ..models.combat import BattleEvent, SimulationResult
    from .attribute_service import DAMAGE_TYPE_LABELS
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS
except ImportError:
    from models.combat import BattleEvent, SimulationResult
    from services.attribute_service import DAMAGE_TYPE_LABELS
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS


class BattleReportBuilder:
    """Turns canonical simulation events into a compact, factual chat report."""

    MIN_LINES = 6
    MAX_LINES = 10
    ENVIRONMENT_LABELS = {
        "calm": "平稳场地",
        "rain": "雨地",
        "fog": "浓雾",
        "strong_wind": "强风",
        "close_quarters": "狭地",
        "mana_tide": "魔力潮",
        "ether_disturbance": "以太扰动",
    }
    TACTIC_LABELS = {
        "pressure": "压制",
        "counter": "反制",
        "skirmish": "游击",
        "control": "控制",
        "sustain": "坚守",
        "gambit": "奇策",
    }
    ABILITY_EVENT_KINDS = frozenset({
        "skill_use", "spell_cast_start", "spell_cast", "summon",
        "summon_strike", "zone_create", "mana_backlash", "mana_barrier",
        "life_steal", "status_apply", "ability_heal", "cleanse", "dispel",
        "resource_restore", "mana_drain", "teleport", "stance",
    })
    OUTCOME_PRIORITY = (
        "cleanse", "dispel", "mana_drain", "teleport",
        "resource_restore", "mana_backlash", "mana_barrier",
        "life_steal", "status_apply", "ability_heal", "summon",
        "zone_create", "stance",
    )

    def build(self, result: SimulationResult) -> list[str]:
        names = {
            result.attacker.user_pk: result.attacker.name,
            result.defender.user_pk: result.defender.name,
        }
        winner_name = names[result.winner_pk]
        loser_name = names[result.loser_pk]
        damage_events = [
            event
            for event in result.events
            if event.kind in {"damage", "summon_strike"}
        ]
        attack_events = [event for event in result.events if event.kind == "attack"]
        ability_events = [
            event for event in result.events
            if event.kind in self.ABILITY_EVENT_KINDS
        ]
        knockback_events = [
            event for event in result.events if event.kind == "knockback"
        ]
        guard_count = sum(
            event.kind == "damage" and event.guarded for event in result.events
        )
        evade_count = sum(event.kind == "evade" for event in result.events)
        status_resist_count = sum(
            event.kind == "status_resist" for event in result.events
        )
        tactic_events = [
            event for event in result.events
            if event.kind == "strategy_trigger" and event.skill_id == "opening"
        ]
        fortune_events = [
            event for event in result.events if event.kind == "fortune_swing"
        ]

        first_attack_tick = attack_events[0].tick if attack_events else result.duration_ticks
        lines = [
            (
                f"{result.attacker.name}以「{result.attacker.strategy}」迎战，"
                f"{result.defender.name}采用「{result.defender.strategy}」；"
                f"本场环境为{self.ENVIRONMENT_LABELS.get(result.environment_id, result.environment_id)}。"
            ),
            (
                f"双方沿一维战场接近，在战斗开始后"
                f"{self._seconds(first_attack_tick)}秒进入交锋距离。"
            ),
        ]
        if len(tactic_events) >= 2:
            left = self.TACTIC_LABELS.get(
                tactic_events[0].status_id,
                tactic_events[0].status_id or "未知",
            )
            right = self.TACTIC_LABELS.get(
                tactic_events[1].status_id,
                tactic_events[1].status_id or "未知",
            )
            relation = (
                "取得战术先机" if tactic_events[0].value > 0
                else "受到对方克制" if tactic_events[0].value < 0
                else "没有形成直接克制"
            )
            lines.append(
                f"开局战术为{left}对{right}，{result.attacker.name}{relation}。"
            )
        if damage_events:
            lines.append(self._damage_line(damage_events[0], names))
        else:
            lines.append("双方始终没有形成有效命中，战局陷入僵持。")

        if ability_events:
            event = next(
                (
                    candidate
                    for kind in self.OUTCOME_PRIORITY
                    for candidate in ability_events
                    if candidate.kind == kind
                ),
                ability_events[0],
            )
            lines.append(self._ability_line(event, names))
        if guard_count or evade_count or status_resist_count:
            lines.append(
                f"全场出现{guard_count}次有效防御、{evade_count}次闪避、"
                f"{status_resist_count}次异常抵抗，攻防节奏反复变化。"
            )
        else:
            lines.append("双方没有选择退让，以连续正面交锋争夺主动。")

        if fortune_events:
            event = fortune_events[0]
            actor = names.get(event.actor_pk, "参战者")
            lines.append(
                f"战斗开始后{self._seconds(event.tick)}秒，{actor}触发运势，"
                "让一次本应发生的坏结果重新判定。"
            )

        special = next(
            (event for event in damage_events if event.critical or event.guarded),
            damage_events[len(damage_events) // 2] if damage_events else None,
        )
        if special is not None:
            lines.append(self._damage_line(special, names))
        else:
            lines.append("体力差距逐渐显现，最后阶段的每次判断都更加关键。")

        if knockback_events:
            knockback = max(knockback_events, key=lambda event: event.value)
            actor = names.get(knockback.actor_pk, "进攻方")
            target = names.get(knockback.target_pk, "防守方")
            lines.append(
                f"战斗开始后{self._seconds(knockback.tick)}秒，"
                f"{target}被{actor}击退{knockback.value}距离并陷入受击硬直。"
            )

        lines.append(
            f"交锋结束时，{result.attacker.name}剩余"
            f"{result.attacker_remaining_hp}/{result.attacker.max_hp}生命，"
            f"{result.defender.name}剩余{result.defender_remaining_hp}/{result.defender.max_hp}生命；"
            f"双方SP为{result.attacker_remaining_stamina}/{result.defender_remaining_stamina}，"
            f"MP为{result.attacker_remaining_mana}/{result.defender_remaining_mana}。"
        )
        if result.finish_reason in {
            "timeout_hp_ratio",
            "timeout_remaining_hp",
            "timeout_score",
        }:
            lines.append(
                f"战斗在{self._seconds(result.duration_ticks)}秒时达到上限，"
                f"{winner_name}凭剩余生命值与有效输出取得裁决胜利。"
            )
        elif result.finish_reason == "double_ko_tiebreak":
            lines.append(f"双方在同一时刻倒下，经规则裁决由{winner_name}击败{loser_name}。")
        else:
            lines.append(
                f"战斗开始后{self._seconds(result.duration_ticks)}秒，"
                f"{winner_name}完成击倒，战胜{loser_name}。"
            )
        if len(lines) <= self.MAX_LINES:
            return lines
        return lines[: self.MAX_LINES - 2] + lines[-2:]

    def _damage_line(
        self,
        event: BattleEvent,
        names: dict[int, str],
    ) -> str:
        actor = names.get(event.actor_pk, "进攻方")
        target = names.get(event.target_pk, "防守方")
        definition = ACTIVE_ABILITY_DEFINITIONS.get(event.skill_id or "")
        if event.kind == "summon_strike":
            summon_name = definition.name if definition else "召唤物"
            skill_prefix = f"召唤的守卫借「{summon_name}」"
        else:
            skill_prefix = f"发动「{definition.name}」" if definition else ""
        if event.critical:
            action = f"{skill_prefix}打出暴击命中{target}"
        elif event.guarded:
            action = f"{skill_prefix}的攻击被{target}防御后仍然命中"
        else:
            action = f"{skill_prefix}命中{target}"
        damage_label = ""
        if event.damage_breakdown and any(
            damage_type != "physical" for damage_type in event.damage_breakdown
        ):
            labels = "+".join(
                DAMAGE_TYPE_LABELS.get(damage_type, damage_type)
                for damage_type in event.damage_breakdown
            )
            damage_label = f"（{labels}）"
        return (
            f"战斗开始后{self._seconds(event.tick)}秒，"
            f"{actor}{action}，造成{event.value}{damage_label}伤害，"
            f"目标剩余{event.remaining_hp}生命。"
        )

    def _ability_line(
        self,
        event: BattleEvent,
        names: dict[int, str],
    ) -> str:
        actor = names.get(event.actor_pk, "参战者")
        target = names.get(event.target_pk, "对手")
        definition = ACTIVE_ABILITY_DEFINITIONS.get(event.skill_id or "")
        ability_name = definition.name if definition else "特殊能力"
        prefix = f"战斗开始后{self._seconds(event.tick)}秒，"
        if event.kind == "mana_backlash":
            return f"{prefix}{actor}透支施法并承受{event.value}点魔力反噬。"
        if event.kind == "mana_barrier":
            return (
                f"{prefix}{actor}的法力护盾消耗魔力抵消"
                f"{event.value}点伤害。"
            )
        if event.kind == "life_steal":
            return (
                f"{prefix}{actor}借「{ability_name}」汲取{event.value}点生命，"
                f"恢复至{event.remaining_hp}。"
            )
        if event.kind == "ability_heal":
            return (
                f"{prefix}{actor}施放「{ability_name}」，为{target}恢复"
                f"{event.value}点生命。"
            )
        if event.kind == "cleanse":
            return (
                f"{prefix}{actor}借「{ability_name}」净化了{target}身上的"
                f"{event.value}个异常状态。"
            )
        if event.kind == "dispel":
            return (
                f"{prefix}{actor}借「{ability_name}」驱散了{target}的"
                f"{event.value}项增益或召唤效果。"
            )
        if event.kind == "mana_drain":
            return (
                f"{prefix}{actor}借「{ability_name}」从{target}吸取"
                f"{event.value}点魔力。"
            )
        if event.kind == "resource_restore":
            resource = "体力" if event.status_id == "sp" else "魔力"
            return (
                f"{prefix}{actor}借「{ability_name}」恢复"
                f"{event.value}点{resource}。"
            )
        if event.kind == "teleport":
            return (
                f"{prefix}{actor}施放「{ability_name}」完成位移，"
                f"移动了{event.value}距离。"
            )
        if event.kind == "summon":
            return f"{prefix}{actor}施放「{ability_name}」，召唤物随之入场。"
        if event.kind == "zone_create":
            return f"{prefix}{actor}施放「{ability_name}」，在战场上展开区域效果。"
        if event.kind == "stance":
            return f"{prefix}{actor}发动「{ability_name}」，战斗姿态正式生效。"
        if (
            event.kind == "status_apply"
            and event.skill_id == "monster_corrosive_splash"
            and event.status_id == "defense_down"
        ):
            return (
                f"{prefix}{actor}的弱酸腐蚀了{target}的护甲，"
                "防御暂时下降。"
            )
        if event.kind == "status_apply":
            if event.actor_pk == event.target_pk:
                return (
                    f"{prefix}{actor}施放「{ability_name}」，使自身获得"
                    f"「{event.status_id or '特殊'}」状态。"
                )
            return (
                f"{prefix}{actor}施放「{ability_name}」，使{target}受到"
                f"「{event.status_id or '特殊'}」状态影响。"
            )
        if event.kind == "summon_strike":
            return (
                f"{prefix}{actor}召唤的守卫发动「{ability_name}」，"
                f"造成{event.value}点伤害。"
            )
        action = "施放" if definition and definition.ability_type == "spell" else "发动"
        return f"{prefix}{actor}{action}「{ability_name}」，战局随之改变。"

    def _seconds(self, tick: int) -> str:
        return f"{tick * 0.1:.1f}"
