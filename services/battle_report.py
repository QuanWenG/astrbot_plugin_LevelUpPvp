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

    def build(self, result: SimulationResult) -> list[str]:
        names = {
            result.attacker.user_pk: result.attacker.name,
            result.defender.user_pk: result.defender.name,
        }
        winner_name = names[result.winner_pk]
        loser_name = names[result.loser_pk]
        damage_events = [event for event in result.events if event.kind == "damage"]
        attack_events = [event for event in result.events if event.kind == "attack"]
        ability_events = [event for event in result.events if event.kind in {"skill_use", "spell_cast_start", "spell_cast", "summon", "zone_create", "mana_backlash"}]
        knockback_events = [
            event for event in result.events if event.kind == "knockback"
        ]
        guard_count = sum(
            event.kind == "damage" and event.guarded for event in result.events
        )
        evade_count = sum(event.kind == "evade" for event in result.events)

        first_attack_tick = attack_events[0].tick if attack_events else result.duration_ticks
        lines = [
            (
                f"{result.attacker.name}以「{result.attacker.strategy}」迎战，"
                f"{result.defender.name}采用「{result.defender.strategy}」。"
            ),
            (
                f"双方沿一维战场接近，在战斗开始后"
                f"{self._seconds(first_attack_tick)}秒进入交锋距离。"
            ),
        ]
        if damage_events:
            lines.append(self._damage_line(damage_events[0], names))
        else:
            lines.append("双方始终没有形成有效命中，战局陷入僵持。")

        if ability_events:
            event = ability_events[0]
            actor = names.get(event.actor_pk, "参战者")
            definition = ACTIVE_ABILITY_DEFINITIONS.get(event.skill_id or "")
            ability_name = definition.name if definition else "特殊能力"
            if event.kind == "mana_backlash":
                lines.append(f"战斗开始后{self._seconds(event.tick)}秒，{actor}透支施法并承受{event.value}点魔力反噬。")
            else:
                action = "施放" if definition and definition.ability_type == "spell" else "发动"
                lines.append(f"战斗开始后{self._seconds(event.tick)}秒，{actor}{action}「{ability_name}」，战局随之改变。")
        if guard_count or evade_count:
            lines.append(f"全场出现{guard_count}次有效防御、{evade_count}次闪避，攻防节奏反复变化。")
        else:
            lines.append("双方没有选择退让，以连续正面交锋争夺主动。")

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
        return lines[: self.MAX_LINES]

    def _damage_line(
        self,
        event: BattleEvent,
        names: dict[int, str],
    ) -> str:
        actor = names.get(event.actor_pk, "进攻方")
        target = names.get(event.target_pk, "防守方")
        definition = ACTIVE_ABILITY_DEFINITIONS.get(event.skill_id or "")
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

    def _seconds(self, tick: int) -> str:
        return f"{tick * 0.1:.1f}"
