import random
import unittest
from dataclasses import replace
from types import SimpleNamespace

from models.ability import ActionEffect, CombatStatus, UserSpell
from models.attributes import AdvancedAttributes, DerivedStats, PrimaryAttributes
from models.combat import BattleEvent, BattleState, FighterSnapshot, FighterState
from models.equipment import EquipmentBuild
from models.skill import SkillBuild
from services.ability_catalog import (
    SPELL_DEFINITIONS,
    TECHNIQUE_DEFINITIONS,
)
from services.ability_runtime import AbilityRuntime
from services.attribute_service import AttributeService
from services.balance_rules import spell_damage_amount
from services.combat_engine import SideviewCombatEngine
from services.skill_service import SkillService
from services.spell_rules import spell_base_power, spell_multiplier_for


def equipment(weight=10.0, style="light"):
    movement = {"light": 1.0, "medium": 0.9, "heavy": 0.75}[style]
    regen = {"light": 10, "medium": 8, "heavy": 6}[style]
    return EquipmentBuild(
        items=(), slots={}, stat_modifiers={}, skill_modifiers={},
        weapon_mode="one_hand", weapon_type="longsword",
        armor_style=style, total_weight=weight, carry_capacity=100.0,
        overloaded=False, attack_range=100, damage_multiplier=1.0,
        attack_windup=1, attack_recovery=2, attack_cooldown=6,
        attack_stamina=8, movement_multiplier=movement,
        stamina_regen=regen, max_stamina=100, weapon_power=10,
        armor_power=10, action_speed=100 * movement,
        physical_accuracy_multiplier={"light": 1.0, "medium": 0.95, "heavy": 0.85}[style],
        spell_accuracy_multiplier={"light": 1.0, "medium": 0.90, "heavy": 0.75}[style],
    )


def runtime_fighter(pk=1, *, levels=None, weight=10.0, style="light"):
    levels = levels or {}
    attrs = PrimaryAttributes(40, 40, 40, 40, 40, 40)
    advanced = AdvancedAttributes()
    build = equipment(weight, style)
    derived = AttributeService().derive(
        level=50,
        attributes=attrs,
        equipment=build,
        advanced=advanced,
        effective_skills=levels,
    )
    skills = SkillBuild({}, dict(levels))
    snapshot = FighterSnapshot(
        pk, f"角色{pk}", 50, 40, 40, 40, 40, 40, "稳扎稳打",
        equipment=build, skills=skills, attributes=attrs,
        advanced_attributes=advanced, derived=derived,
    )
    return FighterState(
        snapshot, derived.max_hp, 250 if pk == 1 else 750,
        stamina=derived.max_sp, mana=derived.max_mp,
    )


def fixed_derived(**changes):
    base = DerivedStats(
        max_hp=1000, max_mp=500, max_sp=300,
        attack_power=100, accuracy=100, defense=0, evasion=0,
        critical_rate=0.0, critical_damage=1.5, action_speed=100,
        carry_capacity=100,
    )
    return replace(base, **changes)


def simple_fighter(pk, derived=None, skills=None):
    attrs = PrimaryAttributes(40, 40, 40, 40, 40, 40)
    snapshot = FighterSnapshot(
        pk, f"角色{pk}", 50, 40, 40, 40, 40, 40, "稳扎稳打",
        skills=skills, attributes=attrs,
        advanced_attributes=AdvancedAttributes(),
        derived=derived or fixed_derived(),
    )
    return FighterState(
        snapshot, snapshot.max_hp, 250 if pk == 1 else 750,
        stamina=snapshot.max_sp, mana=snapshot.max_mp,
    )


class TechniqueConditionTests(unittest.TestCase):
    def test_all_five_conditional_techniques_use_catalog_multipliers(self):
        cases = (
            ("courage_charge", "hp", 1.40),
            ("soldier_thrust", "stance", 1.60),
            ("blind_stab", "haze", 1.35),
            ("fear_judgment", "both", 1.50),
            ("prepared_shot", "hp", 1.40),
        )
        runtime = AbilityRuntime()
        for ability_id, condition, bonus in cases:
            with self.subTest(ability=ability_id):
                actor = simple_fighter(1)
                target = simple_fighter(2)
                definition = TECHNIQUE_DEFINITIONS[ability_id]
                if condition == "stance":
                    actor.stance_id = "test_stance"
                if condition in {"haze", "both"}:
                    target.statuses["haze"] = CombatStatus("haze", 1, 10)
                if condition == "both":
                    target.statuses["blind"] = CombatStatus("blind", 1, 10)
                actual = runtime.damage_result(
                    actor, target, definition, random.Random(77)
                )[0]
                physical = next(
                    effect for effect in definition.effects
                    if effect.effect_type == "physical_damage"
                )
                expected_definition = replace(
                    definition,
                    effects=(replace(physical, value=physical.value * bonus, params={}),),
                )
                expected = runtime.damage_result(
                    actor, target, expected_definition, random.Random(77)
                )[0]
                self.assertEqual(actual, expected)


class SpellMultiplierTests(unittest.TestCase):
    def _spell_pair(self, ability_id, school_level, multiplier_key):
        definition = SPELL_DEFINITIONS[ability_id]
        skills = SkillBuild(
            {}, {definition.unlock_skill_id: school_level},
            (ability_id,), {ability_id: definition}, {},
            {ability_id: UserSpell(ability_id, 20, 0, 100)},
        )
        multipliers = {multiplier_key: 1 + school_level * 0.004}
        actor = simple_fighter(
            1, fixed_derived(spell_multipliers=multipliers), skills
        )
        return actor, simple_fighter(2), definition

    def test_arcane_and_element_damage_keys_apply_at_0_50_100(self):
        runtime = AbilityRuntime()
        for ability_id, key in (("magic_arrow", "arcane"), ("fire_ray", "fire")):
            for level in (0, 50, 100):
                with self.subTest(ability=ability_id, level=level):
                    actor, target, definition = self._spell_pair(
                        ability_id, level, key
                    )
                    effect = next(
                        item for item in definition.effects
                        if item.effect_type == "magic_damage"
                    )
                    spell_level = 20
                    base = spell_base_power(
                        definition, actor, spell_level
                    )
                    variance = random.Random(91).uniform(0.90, 1.10)
                    expected = spell_damage_amount(
                        base_power=base,
                        effect_multiplier=effect.value,
                        spell_multiplier=spell_multiplier_for(
                            definition, actor
                        ),
                        variance=variance,
                        resistance=0,
                        attacker_level=actor.snapshot.level,
                        magical_reduction=0,
                    )
                    actual = runtime.damage_result(
                        actor, target, definition, random.Random(91)
                    )[0]
                    self.assertEqual(actual, expected)


class RuntimeStatTests(unittest.TestCase):
    def setUp(self):
        self.runtime = AbilityRuntime()
        self.actor = runtime_fighter(
            levels={"light_armor": 50, "medium_armor": 50, "heavy_armor": 50},
            weight=36.0,
            style="heavy",
        )
        self.target = runtime_fighter(2)
        self.state = BattleState(1, self.actor, self.target, [], 123)

    def _apply(self, effect, seed=1):
        return self.runtime.apply_status(
            self.state, self.actor, effect, self.actor.snapshot.user_pk,
            random.Random(seed),
        )

    def test_primary_statuses_rescale_resources_and_expire_without_reviving(self):
        self.actor.current_hp = self.actor.max_hp // 2
        self.actor.mana = self.actor.max_mp // 2
        self.actor.stamina = self.actor.max_sp // 2
        old_ratios = (
            self.actor.hp_ratio,
            self.actor.mana / self.actor.max_mp,
            self.actor.stamina / self.actor.max_sp,
        )
        elm = SPELL_DEFINITIONS["elm_blessing"].effects[0]
        self._apply(elm)
        self.assertEqual(self.actor.primary("magic"), 48)
        self.assertAlmostEqual(self.actor.hp_ratio, old_ratios[0], places=2)
        self.assertAlmostEqual(self.actor.mana / self.actor.max_mp, old_ratios[1], places=2)
        self.assertAlmostEqual(self.actor.stamina / self.actor.max_sp, old_ratios[2], places=2)
        self.runtime.remove_status(self.state, self.actor, "elm_blessing")
        self.assertEqual(self.actor.primary("magic"), 40)
        self.actor.current_hp = 0
        self._apply(SPELL_DEFINITIONS["hero"].effects[0])
        self.assertEqual(self.actor.current_hp, 0)

    def test_negative_mana_and_stance_pools_survive_cap_changes(self):
        elm = SPELL_DEFINITIONS["elm_blessing"].effects[0]
        self.actor.mana = -37
        self._apply(elm)
        self.assertEqual(self.actor.mana, -37)
        self.runtime.remove_status(self.state, self.actor, "elm_blessing")
        self.actor.mana = self.actor.max_mp // 2
        self.actor.stance_id = "test_stance"
        self.actor.frozen_mana_capacity = (self.actor.max_mp + 3) // 4
        self.actor.frozen_mana = self.actor.frozen_mana_capacity // 2
        old_available = self.actor.max_mp - self.actor.frozen_mana_capacity
        old_mana_ratio = self.actor.mana / old_available
        old_frozen_ratio = (
            self.actor.frozen_mana / self.actor.frozen_mana_capacity
        )
        self._apply(elm)
        self.assertAlmostEqual(
            self.actor.mana / (self.actor.max_mp - self.actor.frozen_mana_capacity),
            old_mana_ratio,
            places=2,
        )
        self.assertAlmostEqual(
            self.actor.frozen_mana / self.actor.frozen_mana_capacity,
            old_frozen_ratio,
            places=2,
        )

    def test_floating_recomputes_armor_passives_accuracy_movement_and_regen(self):
        heavy_defense = self.actor.current_derived.defense
        floating = SPELL_DEFINITIONS["feather_float"].effects[0]
        self._apply(floating)
        self.assertEqual(self.actor.runtime_weight, 27.0)
        self.assertEqual(self.actor.runtime_armor_style, "medium")
        self.assertEqual(
            self.actor.current_derived.physical_accuracy_multiplier, 0.95
        )
        self.assertNotEqual(self.actor.current_derived.defense, heavy_defense)
        engine = SideviewCombatEngine()
        self.assertEqual(engine._accuracy_multiplier(self.actor, True), 0.90)
        self.runtime.remove_status(self.state, self.actor, "floating")
        self.assertEqual(self.actor.runtime_armor_style, "heavy")
        self.assertEqual(
            self.actor.current_derived.physical_accuracy_multiplier, 0.85
        )


class RegisteredEffectTests(unittest.TestCase):
    def test_blessing_and_summon_power_scale_only_numeric_aura_values(self):
        levels = {
            "blessing": 100, "pact": 100,
            "spiritualism": 100, "ritual": 100,
        }
        actor = runtime_fighter(levels=levels)
        target = runtime_fighter(2)
        state = BattleState(1, actor, target, [], 44)
        runtime = AbilityRuntime()
        holy = SPELL_DEFINITIONS["holy_justice"]
        runtime.apply_secondary(
            state, actor, target, holy,
            runtime.damage_result(actor, target, holy, random.Random(1)),
            random.Random(1),
        )
        self.assertAlmostEqual(
            actor.statuses["holy_justice"].params["physical_damage"], 0.35
        )
        totem = TECHNIQUE_DEFINITIONS["warrior_totem"]
        runtime.apply_secondary(
            state, actor, target, totem,
            runtime.damage_result(actor, target, totem, random.Random(2)),
            random.Random(2),
        )
        self.assertAlmostEqual(
            actor.statuses["warrior_totem"].params["physical_damage"], 0.38
        )
        self.assertEqual(state.entities[0].aura_radius, 250)
        self.assertEqual(state.entities[0].remaining_ticks, 40)

    def test_control_and_weapon_status_semantics(self):
        actor = runtime_fighter(levels={"mind_control": 50})
        target = runtime_fighter(2)
        state = BattleState(1, actor, target, [], 55)
        runtime = AbilityRuntime()
        runtime.apply_status(
            state, actor, SPELL_DEFINITIONS["mental_rebound"].effects[0],
            actor.snapshot.user_pk, random.Random(1),
        )
        self.assertEqual(actor.skill_level("mind_control"), 70)
        runtime.apply_status(
            state, actor, SPELL_DEFINITIONS["free_thought"].effects[0],
            actor.snapshot.user_pk, random.Random(2),
        )
        haze = ActionEffect(
            "apply_status", "enemy", 0.0, 20, 1.0,
            "mind", "haze",
        )
        runtime.apply_status(
            state, actor, haze, target.snapshot.user_pk, random.Random(3)
        )
        self.assertEqual(actor.statuses["haze"].remaining_ticks, 10)
        runtime.apply_status(
            state, actor, SPELL_DEFINITIONS["tree_skin"].effects[0],
            actor.snapshot.user_pk, random.Random(4),
        )
        bleed = replace(haze, status_id="bleed")
        self.assertFalse(
            runtime.apply_status(
                state, actor, bleed, target.snapshot.user_pk, random.Random(2)
            )
        )
        runtime.apply_status(
            state, actor, SPELL_DEFINITIONS["evil_fear"].effects[0],
            target.snapshot.user_pk, random.Random(5),
        )
        self.assertAlmostEqual(runtime.modifier(actor, "slow"), 0.35)
        self.assertAlmostEqual(runtime.modifier(actor, "healing"), -0.50)
        hunting = TECHNIQUE_DEFINITIONS["hunting_moment"].effects[0]
        runtime.apply_status(
            state, actor, hunting, actor.snapshot.user_pk, random.Random(6)
        )
        self.assertAlmostEqual(runtime.modifier(actor, "ranged_followup"), 0.15)


class GrowthClassificationTests(unittest.TestCase):
    def test_spell_damage_does_not_train_physical_skills(self):
        definition = SPELL_DEFINITIONS["magic_arrow"]
        spells = {"magic_arrow": UserSpell("magic_arrow", 1, 0, 100)}
        skills = SkillBuild(
            {}, {"magic_training": 50}, ("magic_arrow",),
            {"magic_arrow": definition}, {}, spells,
        )
        actor = replace(simple_fighter(1, skills=skills).snapshot, equipment=equipment())
        target = replace(simple_fighter(2).snapshot, equipment=equipment())
        result = SimpleNamespace(
            attacker=actor,
            defender=target,
            duration_ticks=10,
            events=(
                BattleEvent(1, "spell_cast", 1, 2, skill_id="magic_arrow"),
                BattleEvent(2, "damage", 1, 2, value=20, skill_id="magic_arrow"),
                BattleEvent(3, "whiff", 1, 2, skill_id="magic_arrow"),
            ),
        )
        usage = SkillService("").usage_from_simulation(result)[1]
        self.assertEqual(usage, {"magic_training": 3})

    def test_damage_event_uses_runtime_armor_style_for_growth(self):
        actor = replace(simple_fighter(1).snapshot, equipment=equipment())
        target = replace(simple_fighter(2).snapshot, equipment=equipment())
        result = SimpleNamespace(
            attacker=actor,
            defender=target,
            duration_ticks=10,
            events=(BattleEvent(1, "damage", 1, 2, armor_style="medium"),),
        )
        usage = SkillService("").usage_from_simulation(result)
        self.assertEqual(usage[2]["medium_armor"], 1)
        self.assertNotIn("light_armor", usage[2])


if __name__ == "__main__":
    unittest.main()
