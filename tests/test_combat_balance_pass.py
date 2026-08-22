import math
import random
import unittest
from dataclasses import replace
from unittest import mock

from models.attributes import AdvancedAttributes, DerivedStats, PrimaryAttributes
from models.ability import CombatStatus, UserSpell
from models.combat import AIProfile, BattleState, FighterSnapshot, FighterState
from models.user import User
from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
from services.ability_catalog import TECHNIQUE_DEFINITIONS
from services.ability_runtime import AbilityRuntime
from services.balance_rules import (
    mana_overcast_backlash,
    pvp_burst_cap,
    ranged_preferred_range_fraction,
    spell_interrupt_damage_threshold,
    spell_preferred_range_fraction,
    split_arrow_followup_multiplier,
)
from services.combat_ai import _ability_score, choose_action
from services.build_service import CombatBuildService
from services.attribute_service import AttributeService
from services.combat_engine import SideviewCombatEngine
from services.equipment_catalog import (
    DEFAULT_EQUIPMENT_CATALOG,
    EquipmentFactory,
)
from models.equipment import EquipmentBuild
from models.skill import SkillBuild
from services.spell_rules import spell_base_power, spell_effect_scale


def _fighter(user_pk: int) -> FighterState:
    snapshot = FighterSnapshot(
        user_pk,
        str(user_pk),
        25,
        12,
        12,
        10,
        10,
        10,
        "稳扎稳打",
    )
    return FighterState(
        snapshot,
        snapshot.max_hp,
        250 if user_pk == 1 else 750,
        mana=100,
        stamina=100,
    )


def _ranged_equipment(*, weapon_type: str = "bow", recovery: int = 2) -> EquipmentBuild:
    return EquipmentBuild(
        items=(),
        slots={},
        stat_modifiers={},
        skill_modifiers={},
        weapon_mode="two_hand_ranged",
        weapon_type=weapon_type,
        armor_style="light",
        total_weight=1.0,
        carry_capacity=100.0,
        overloaded=False,
        attack_range=450,
        damage_multiplier=1.0,
        attack_windup=1,
        attack_recovery=recovery,
        attack_cooldown=6,
        attack_stamina=8,
        movement_multiplier=1.0,
        stamina_regen=10,
        max_stamina=100,
    )
class CombatBalancePassTests(unittest.TestCase):
    def test_late_technique_milestones_are_reachable_and_keep_tier_costs(self):
        self.assertEqual(TECHNIQUE_DEFINITIONS["courage_charge"].unlock_level, 35)
        self.assertEqual(TECHNIQUE_DEFINITIONS["soldier_thrust"].unlock_level, 40)
        self.assertEqual(TECHNIQUE_DEFINITIONS["courage_charge"].resource_cost, 26)
        self.assertEqual(TECHNIQUE_DEFINITIONS["soldier_thrust"].resource_cost, 34)

    def test_first_weapon_shots_unlock_before_the_level_twenty_plateau(self):
        split = ACTIVE_ABILITY_DEFINITIONS["split_arrow"]
        self.assertEqual(split.unlock_level, 4)
        self.assertEqual(split.resource_cost, 8)
        self.assertEqual(split.windup_ticks, 1)
        self.assertEqual(split.targeting, "single")
        self.assertEqual(split.cast_range, 450)
        self.assertEqual(split.effects[0].params["flat_attack_power"], 12.0)

        prepared = ACTIVE_ABILITY_DEFINITIONS["prepared_shot"]
        self.assertEqual(prepared.unlock_level, 20)
        armor_piercing = ACTIVE_ABILITY_DEFINITIONS["armor_piercing_shot"]
        self.assertEqual(armor_piercing.unlock_level, 20)

        for ability_id in ("destructive_shot", "ferocious_shot"):
            with self.subTest(ability_id=ability_id):
                definition = ACTIVE_ABILITY_DEFINITIONS[ability_id]
                self.assertEqual(definition.unlock_level, 4)
                self.assertEqual(definition.resource_cost, 8)
                self.assertEqual(definition.windup_ticks, 1)
                self.assertEqual(definition.recovery_ticks, 1)
                self.assertEqual(
                    definition.effects[0].params["flat_attack_power"],
                    4.0,
                )

        feint = ACTIVE_ABILITY_DEFINITIONS["feint_stab"]
        self.assertEqual(feint.unlock_level, 4)
        self.assertEqual(feint.resource_cost, 8)
        self.assertEqual(feint.effects[0].params["flat_attack_power"], 4.0)

    def test_early_tactics_has_a_real_gap_closer(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["closing_assault"]
        self.assertEqual(definition.unlock_level, 4)
        self.assertEqual(definition.resource_cost, 8)
        self.assertEqual(definition.cast_range, 220)
        self.assertEqual(definition.targeting, "line")
        self.assertIn("two_hand_heavy", definition.compatible_weapon_modes)

    def test_flat_attack_power_is_added_before_physical_mitigation(self):
        actor = _fighter(1)
        target = _fighter(2)
        derived = DerivedStats(
            max_hp=actor.snapshot.max_hp,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
        )
        actor.snapshot = replace(actor.snapshot, derived=derived)
        actor.current_derived = derived

        with mock.patch(
            "services.ability_runtime.physical_damage_amount",
            return_value=7,
        ) as calculate:
            AbilityRuntime().damage_result(
                actor,
                target,
                ACTIVE_ABILITY_DEFINITIONS["ferocious_shot"],
                random.Random(1),
            )

        self.assertEqual(calculate.call_args.kwargs["attack_power"], 24.0)

    def test_active_guard_weakly_resists_magic_but_passive_block_does_not(self):
        actor = _fighter(1)
        target = _fighter(2)
        definition = ACTIVE_ABILITY_DEFINITIONS["magic_arrow"]
        runtime = AbilityRuntime()

        with mock.patch(
            "services.ability_runtime.spell_damage_amount",
            return_value=100,
        ):
            normal = runtime.damage_result(
                actor, target, definition, random.Random(7)
            )
            target.guarding = True
            active_guard = runtime.damage_result(
                actor, target, definition, random.Random(7)
            )
            target.guarding = False
            target.snapshot = replace(
                target.snapshot,
                equipment=replace(_ranged_equipment(), block_rate=1.0),
            )
            passive_block = runtime.damage_result(
                actor, target, definition, random.Random(7)
            )

        self.assertEqual(normal[0], 100)
        self.assertEqual(active_guard[0], 75)
        self.assertTrue(active_guard[2])
        self.assertEqual(passive_block[0], 100)
        self.assertFalse(passive_block[2])

    def test_ranged_mastery_curve_has_no_midgame_jump(self):
        engine = SideviewCombatEngine()
        actor = _fighter(1)
        actor.snapshot = replace(
            actor.snapshot,
            equipment=_ranged_equipment(),
        )

        values = []
        for level in (14, 15, 16, 25, 40):
            actor.runtime_effective_skills["marksmanship"] = level
            values.append(
                engine._apply_ranged_mastery(
                    actor,
                    (100, False, False, 0, None, {"physical": 100}),
                )[0]
            )

        self.assertEqual(values, [99, 100, 101, 112, 130])
        self.assertTrue(all(left < right for left, right in zip(values, values[1:])))
        self.assertLessEqual(values[-1], 135)

    def test_ranged_spacing_and_split_echo_are_mastered_not_front_loaded(self):
        engine = SideviewCombatEngine()
        self.assertAlmostEqual(
            ranged_preferred_range_fraction(4, ruleset=engine.ruleset),
            0.736,
        )
        self.assertAlmostEqual(
            ranged_preferred_range_fraction(40, ruleset=engine.ruleset),
            0.88,
        )
        self.assertAlmostEqual(
            split_arrow_followup_multiplier(4, ruleset=engine.ruleset),
            0.17,
        )
        self.assertAlmostEqual(
            split_arrow_followup_multiplier(40, ruleset=engine.ruleset),
            0.35,
        )

    def test_staff_caster_uses_spell_range_without_whiffing_basic_attacks(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["fire_ray"]
        actor = _fighter(1)
        actor.snapshot = replace(
            actor.snapshot,
            equipment=replace(
                _ranged_equipment(),
                weapon_mode="one_hand",
                weapon_type="staff",
                attack_range=100,
            ),
            skills=SkillBuild(
                {},
                {"elemental_guidance": 20},
                active_skill_ids=(definition.ability_id,),
                active_definitions={definition.ability_id: definition},
                spells={
                    definition.ability_id: UserSpell(
                        definition.ability_id, 10, 0, 100
                    )
                },
            ),
        )
        actor.runtime_effective_skills = {"elemental_guidance": 20}
        actor.attack_cooldown = 3
        target = _fighter(2)
        target.position = actor.position + 300

        intent = choose_action(
            actor,
            target,
            AIProfile(0.70, 0.30, 0.76, 105, 0.28, 0.48),
            random.Random(11),
            attack_range=100,
        )

        self.assertAlmostEqual(
            spell_preferred_range_fraction(
                20, ruleset=SideviewCombatEngine().ruleset
            ),
            0.75,
        )
        self.assertIn(intent.action, {"guard", "retreat"})

    def test_firearm_technique_keeps_a_reload_recovery_window(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["ferocious_shot"]
        actor = _fighter(1)
        actor.snapshot = replace(
            actor.snapshot,
            equipment=_ranged_equipment(weapon_type="firearm", recovery=4),
            skills=SkillBuild(
                {},
                {"firearm": 20},
                active_skill_ids=(definition.ability_id,),
                active_definitions={definition.ability_id: definition},
            ),
        )
        target = _fighter(2)
        state = BattleState(1, actor, target, [], 9)
        actor.attack_pending = True
        actor.pending_skill_id = definition.ability_id

        SideviewCombatEngine()._attack_damage(
            state, actor, target, "resolve_attack", random.Random(4)
        )

        self.assertGreaterEqual(actor.recovery_ticks, 2)

    def test_spell_curve_frontloads_discovery_without_buffing_monsters(self):
        actor = _fighter(1)
        spell = ACTIVE_ABILITY_DEFINITIONS["magic_arrow"]
        monster_action = ACTIVE_ABILITY_DEFINITIONS[
            "monster_corrosive_splash"
        ]

        novice_bonus = spell_base_power(spell, actor, 1) - (8 + 1.35)
        trained_bonus = spell_base_power(spell, actor, 20) - (8 + 20 * 1.35)
        self.assertAlmostEqual(novice_bonus, 14.0)
        self.assertLess(trained_bonus, 0.1)
        self.assertAlmostEqual(
            spell_base_power(monster_action, actor, 1),
            8 + 1.35,
        )

    def test_fractional_spell_power_affix_is_a_percentage_bonus(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["magic_arrow"]
        actor = _fighter(1)
        actor.snapshot = replace(
            actor.snapshot,
            equipment=replace(
                _ranged_equipment(),
                combat_effects={"spell_power": 0.05},
            ),
        )

        self.assertAlmostEqual(
            spell_base_power(definition, actor, 20),
            (
                8
                + 20 * 1.35
                + 14 * math.exp(-(20 - 1) / 3.5)
            ) * 1.05,
        )

    def test_magic_arrow_grows_from_novice_floor_into_mastery(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["magic_arrow"]
        effect = definition.effects[0]
        floor = effect.params["mastery_multiplier_floor"]
        growth = effect.params["mastery_multiplier_growth"]
        cap = effect.params["mastery_multiplier_cap"]
        self.assertAlmostEqual(floor + growth * 4, 0.63)
        self.assertLess(floor + growth * 4, cap)
        self.assertEqual(cap, 0.85)

    def test_sword_shield_spends_some_raw_armor_on_guarding_identity(self):
        equipment = replace(
            _ranged_equipment(),
            weapon_mode="sword_shield",
            weapon_type="longsword",
        )
        attributes = PrimaryAttributes(30, 30, 10, 10, 10, 10)
        derived = AttributeService().derive(
            level=25,
            attributes=attributes,
            equipment=equipment,
            advanced=AdvancedAttributes(),
            effective_skills={},
        )
        # This is a catalog-independent contract for the mode multiplier.
        self.assertAlmostEqual(
            derived.defense,
            0.45 * (attributes.constitution * 0.8),
        )

    def test_control_potency_uses_its_own_school_not_highest_skill(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["confusion_spell"]
        actor = _fighter(1)
        actor.snapshot = replace(
            actor.snapshot,
            skills=SkillBuild(
                {},
                {"magic_training": 5, "tactics": 80},
                active_skill_ids=(definition.ability_id,),
                active_definitions={definition.ability_id: definition},
                spells={
                    definition.ability_id: UserSpell(
                        definition.ability_id, 50, 0, 100
                    )
                },
            ),
        )
        actor.runtime_effective_skills = {
            "magic_training": 5,
            "tactics": 80,
        }
        target = _fighter(2)
        state = BattleState(1, actor, target, [], 11)

        with mock.patch(
            "services.ability_runtime.status_chance",
            return_value=1.0,
        ) as calculate:
            AbilityRuntime().apply_secondary(
                state,
                actor,
                target,
                definition,
                (0, False, False, 0, definition.ability_id, {}),
                random.Random(3),
            )

        self.assertAlmostEqual(
            calculate.call_args.kwargs["potency"],
            actor.primary("magic")
            + 0.70 * 5
            + spell_effect_scale(definition, actor).status_power_bonus,
        )

    def test_utility_spell_level_improves_barrier_and_blink_effects(self):
        runtime = AbilityRuntime()
        actor = _fighter(1)

        def with_spell(definition, level):
            skills = SkillBuild(
                {},
                {definition.unlock_skill_id: 30},
                active_skill_ids=(definition.ability_id,),
                active_definitions={definition.ability_id: definition},
                spells={
                    definition.ability_id: UserSpell(
                        definition.ability_id, level, 0, 100
                    )
                },
            )
            snapshot = replace(actor.snapshot, skills=skills)
            return FighterState(
                snapshot,
                snapshot.max_hp,
                actor.position,
                mana=actor.mana,
                stamina=actor.stamina,
            )

        armor = ACTIVE_ABILITY_DEFINITIONS["armor_spell"]
        novice = with_spell(armor, 1)
        master = with_spell(armor, 50)
        novice_effect = runtime._scaled_spell_effect(
            armor, novice, armor.effects[0]
        )
        master_effect = runtime._scaled_spell_effect(
            armor, master, armor.effects[0]
        )
        self.assertGreater(
            master_effect.duration_ticks, novice_effect.duration_ticks
        )
        self.assertGreater(
            master_effect.params["defense"],
            novice_effect.params["defense"],
        )
        self.assertGreater(
            master_effect.params["physical_reduction"],
            novice_effect.params["physical_reduction"],
        )
        self.assertGreater(
            master_effect.params["mana_shield_ratio"],
            novice_effect.params["mana_shield_ratio"],
        )

        blink = ACTIVE_ABILITY_DEFINITIONS["blink"]
        novice_blink = runtime._scaled_spell_effect(
            blink, with_spell(blink, 1), blink.effects[0]
        )
        master_blink = runtime._scaled_spell_effect(
            blink, with_spell(blink, 50), blink.effects[0]
        )
        self.assertGreater(master_blink.value, novice_blink.value)

    def test_spell_focus_holds_chip_damage_but_committed_hits_interrupt(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["fire_ray"]
        target = _fighter(1)
        derived = DerivedStats(
            max_hp=100,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
        )
        target.snapshot = replace(
            target.snapshot,
            attributes=PrimaryAttributes(1, 1, 1, 1, 20, 20),
            derived=derived,
            skills=SkillBuild(
                {},
                {"elemental_guidance": 20},
                active_skill_ids=(definition.ability_id,),
                active_definitions={definition.ability_id: definition},
                spells={
                    definition.ability_id: UserSpell(
                        definition.ability_id, 20, 0, 100
                    )
                },
            ),
        )
        target.current_attributes = target.snapshot.attributes
        target.current_derived = derived
        target.runtime_effective_skills = {"elemental_guidance": 20}
        target.current_hp = 100
        target.attack_pending = True
        target.pending_skill_id = definition.ability_id
        target.windup_ticks = 2
        actor = _fighter(2)
        state = BattleState(1, actor, target, [], 17)
        engine = SideviewCombatEngine()

        threshold = spell_interrupt_damage_threshold(
            max_hp=100,
            focus=60,
            ruleset=engine.ruleset,
        )
        self.assertEqual(threshold, 12)

        engine._apply_damage(
            state,
            actor,
            target,
            (5, False, False, 0, None, {"physical": 5}),
        )
        self.assertTrue(target.attack_pending)
        self.assertEqual(target.pending_skill_id, definition.ability_id)
        self.assertTrue(
            any(event.kind == "spell_concentration" for event in state.events)
        )

        engine._apply_damage(
            state,
            actor,
            target,
            (20, False, False, 0, None, {"physical": 20}),
        )
        self.assertFalse(target.attack_pending)
        self.assertIsNone(target.pending_skill_id)
        self.assertTrue(
            any(event.kind == "attack_interrupted" for event in state.events)
        )

    def test_passive_spell_multiplier_is_consumed_by_damage(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["fire_ray"]
        actor = _fighter(1)
        target = _fighter(2)
        skills = SkillBuild(
            {},
            {"elemental_guidance": 20},
            active_skill_ids=(definition.ability_id,),
            active_definitions={definition.ability_id: definition},
            spells={
                definition.ability_id: UserSpell(
                    definition.ability_id, 20, 0, 100
                )
            },
        )
        derived = DerivedStats(
            max_hp=100,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
            spell_multipliers={"fire": 1.0},
        )
        actor.snapshot = replace(actor.snapshot, skills=skills, derived=derived)
        actor.current_derived = derived
        normal = AbilityRuntime().damage_result(
            actor, target, definition, random.Random(19)
        )[0]
        empowered = replace(
            derived, spell_multipliers={"fire": 1.8}
        )
        actor.snapshot = replace(actor.snapshot, derived=empowered)
        actor.current_derived = empowered
        boosted = AbilityRuntime().damage_result(
            actor, target, definition, random.Random(19)
        )[0]
        self.assertGreater(boosted, normal)

    def test_armor_spell_turns_mana_into_visible_damage_absorption(self):
        engine = SideviewCombatEngine()
        actor = _fighter(1)
        target = _fighter(2)
        derived = DerivedStats(
            max_hp=100,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
        )
        target.snapshot = replace(target.snapshot, derived=derived)
        target.current_derived = derived
        target.current_hp = 100
        target.mana = 50
        target.statuses["armor_spell"] = CombatStatus(
            "armor_spell",
            target.snapshot.user_pk,
            20,
            magnitude=0.2,
            beneficial=True,
            params={
                "mana_shield_ratio": 0.20,
                "source_ability_id": "armor_spell",
            },
        )
        state = BattleState(1, actor, target, [], 41)

        applied = engine._apply_damage(
            state,
            actor,
            target,
            (50, False, False, 0, None, {"physical": 50}),
        )

        capped = pvp_burst_cap(50, 100, ruleset=engine.ruleset)
        absorbed = round(capped * 0.20)
        self.assertEqual(applied[0], capped - absorbed)
        self.assertEqual(target.current_hp, 100 - (capped - absorbed))
        self.assertEqual(target.mana, 40)
        barrier = next(
            event for event in state.events if event.kind == "mana_barrier"
        )
        self.assertEqual(barrier.value, absorbed)
        self.assertEqual(barrier.skill_id, "armor_spell")

    def test_ai_and_engine_share_the_same_bounded_overcast_price(self):
        definition = ACTIVE_ABILITY_DEFINITIONS["magic_arrow"]
        runtime = AbilityRuntime()
        actor = _fighter(1)
        skills = SkillBuild(
            {},
            {"magic_training": 20},
            active_skill_ids=(definition.ability_id,),
            active_definitions={definition.ability_id: definition},
            spells={
                definition.ability_id: UserSpell(
                    definition.ability_id, 1, 0, 100
                )
            },
        )
        derived = DerivedStats(
            max_hp=1_000,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
        )
        actor.snapshot = replace(actor.snapshot, skills=skills, derived=derived)
        actor.current_derived = derived
        actor.current_hp = derived.max_hp
        actor.mana = 0
        target = _fighter(2)
        target.position = 350
        score = _ability_score(
            definition,
            actor,
            target,
            abs(actor.position - target.position),
            0,
            random.Random(5),
            ability_runtime=runtime,
        )
        self.assertIsNotNone(score)

        cost = runtime.effective_cost(definition, actor)
        expected = mana_overcast_backlash(
            max_hp=actor.max_hp,
            max_mp=actor.max_mp,
            projected_mana=-cost,
            reduction=0.0,
            ruleset=runtime.ruleset,
        )
        state = BattleState(1, actor, target, [], 29)
        SideviewCombatEngine()._begin_attack(
            state, actor, "use_skill", definition.ability_id
        )
        backlash = next(
            item for item in state.events if item.kind == "mana_backlash"
        )
        self.assertEqual(backlash.value, expected)
        self.assertEqual(actor.current_hp, actor.max_hp - expected)

        actor.attack_pending = False
        actor.mana = -49
        self.assertIsNone(
            _ability_score(
                definition,
                actor,
                target,
                abs(actor.position - target.position),
                0,
                random.Random(5),
                ability_runtime=runtime,
            )
        )

    def test_great_axe_uses_the_heavy_two_hand_action_profile(self):
        entry = DEFAULT_EQUIPMENT_CATALOG.snapshot.by_template_id[
            "elona_great_axe"
        ]
        self.assertEqual(entry.template.hand_mode, "two_hand_heavy")

    def test_production_dual_wield_uses_partial_offhand_power_and_real_sp_cost(self):
        catalog = DEFAULT_EQUIPMENT_CATALOG.snapshot.by_template_id
        factory = EquipmentFactory()
        main = replace(
            factory.create_from_catalog(
                1, catalog["training_dagger_left"], 1
            ),
            id=101,
        )
        off = replace(
            factory.create_from_catalog(
                1, catalog["training_dagger_right"], 2
            ),
            id=102,
        )
        user = User(
            1, "test", "group", "user", "双持测试", 25,
            0, 0, 0, 24, 10, 10, 24, 10, 0, 0, 0, "", "",
        )
        builds = CombatBuildService(None, None)
        main_only = builds.resolve_equipment(
            user, {"main_hand": main.id}, [main], {}
        )
        dual = builds.resolve_equipment(
            user,
            {"main_hand": main.id, "off_hand": off.id},
            [main, off],
            {},
        )

        self.assertEqual(dual.weapon_mode, "dual_wield")
        self.assertEqual(dual.attack_stamina, 14)
        self.assertGreater(dual.weapon_power, main_only.weapon_power)
        self.assertLess(
            dual.weapon_power,
            dual.main_hand_weapon_power + dual.off_hand_weapon_power,
        )
        self.assertAlmostEqual(dual.dual_wield_efficiency, 0.35)
        self.assertAlmostEqual(dual.dual_wield_followup_scale, 0.1525)

        snapshot = replace(
            _fighter(1).snapshot,
            equipment=dual,
            derived=DerivedStats(
                max_hp=100,
                max_mp=100,
                max_sp=100,
                attack_power=20,
                accuracy=50,
                defense=20,
                evasion=20,
                critical_rate=0.05,
                critical_damage=1.5,
                action_speed=100,
                carry_capacity=50,
            ),
        )
        actor = FighterState(snapshot, 100, 250, stamina=100)
        state = BattleState(1, actor, _fighter(2), [], 3)
        SideviewCombatEngine()._begin_attack(
            state, actor, "basic_attack", "basic_attack"
        )
        self.assertEqual(actor.stamina, 86)

    def test_engine_consumes_equipment_attack_cost_and_restoration_fields(self):
        equipment = replace(
            _ranged_equipment(),
            attack_stamina=7,
            stamina_regen=9,
        )
        actor = _fighter(1)
        actor.snapshot = replace(actor.snapshot, equipment=equipment)
        actor.__post_init__()
        target = _fighter(2)
        state = BattleState(1, actor, target, [], 31)
        engine = SideviewCombatEngine()

        engine._begin_attack(state, actor, "basic_attack", "basic_attack")
        self.assertEqual(actor.stamina, 93)

        actor.attack_pending = False
        actor.stamina = 0
        engine._rest(state, actor)
        self.assertEqual(actor.stamina, 9)

    def test_ai_uses_the_same_equipment_attack_cost_as_the_engine(self):
        actor = _fighter(1)
        actor.snapshot = replace(
            actor.snapshot,
            equipment=replace(_ranged_equipment(), attack_stamina=30),
        )
        actor.stamina = 20
        target = _fighter(2)
        target.position = actor.position + 100

        intent = choose_action(
            actor,
            target,
            AIProfile(),
            random.Random(7),
            attack_range=100,
        )

        self.assertEqual(intent.action, "rest")

    def test_dual_wield_followup_reuses_first_hit_resolution(self):
        equipment = replace(
            CombatBuildService(None, None).resolve_equipment(
                User(
                    1, "test", "group", "user", "双持测试", 25,
                    0, 0, 0, 24, 10, 10, 24, 10, 0, 0, 0, "", "",
                ),
                {},
                [],
                {},
            ),
            weapon_mode="dual_wield",
            dual_wield_followup_scale=0.25,
        )
        actor = _fighter(1)
        actor.snapshot = replace(actor.snapshot, equipment=equipment)
        target = _fighter(2)
        state = BattleState(1, actor, target, [], 5)
        engine = SideviewCombatEngine()
        with mock.patch.object(engine, "_evade_chance", return_value=0.0):
            result = engine._followup_damage(
                state,
                actor,
                target,
                (100, True, True, 0, None, {"physical": 100}),
                random.Random(2),
            )

        self.assertEqual(result[:3], (25, True, True))
        self.assertEqual(result[5], {"physical": 25})

    def test_sudden_death_erosion_only_applies_after_the_start_tick(self):
        engine = SideviewCombatEngine()
        actor = _fighter(1)
        target = _fighter(2)
        target.snapshot = replace(
            target.snapshot,
            derived=DerivedStats(
                max_hp=1_000,
                max_mp=100,
                max_sp=100,
                attack_power=20,
                accuracy=50,
                defense=20,
                evasion=20,
                critical_rate=0.05,
                critical_damage=1.5,
                action_speed=100,
                carry_capacity=50,
            ),
        )
        target.current_derived = target.snapshot.derived
        target.current_hp = 1_000

        before = BattleState(
            engine.ruleset.timeout.sudden_death_start_tick,
            actor,
            target,
            [],
            7,
        )
        engine._apply_damage(
            before,
            actor,
            target,
            (1, False, False, 0, None, {"physical": 1}),
            allow_on_hit_effects=False,
        )
        self.assertEqual(
            next(event for event in before.events if event.kind == "damage").value,
            1,
        )

        target.current_hp = 1_000
        after = BattleState(
            engine.ruleset.timeout.sudden_death_start_tick + 1,
            actor,
            target,
            [],
            7,
        )
        engine._apply_damage(
            after,
            actor,
            target,
            (1, False, False, 0, None, {"physical": 1}),
            allow_on_hit_effects=False,
        )
        expected = round(
            target.max_hp
            * (
                engine.ruleset.timeout.sudden_death_minimum_hit_ratio
                + engine.ruleset.timeout.sudden_death_minimum_hit_ratio_growth
            )
        )
        self.assertEqual(
            next(event for event in after.events if event.kind == "damage").value,
            expected,
        )

    def test_damage_application_returns_the_actual_capped_breakdown(self):
        engine = SideviewCombatEngine()
        actor = _fighter(1)
        target = _fighter(2)
        state = BattleState(1, actor, target, [], 17)
        applied = engine._apply_damage(
            state,
            actor,
            target,
            (
                10_000,
                True,
                False,
                0,
                "oversized_strike",
                {"physical": 8_000, "fire": 2_000},
            ),
        )

        event = next(item for item in state.events if item.kind == "damage")
        self.assertIsNotNone(applied)
        self.assertEqual(applied[0], event.value)
        self.assertEqual(sum(applied[5].values()), event.value)
        self.assertEqual(applied[5], event.damage_breakdown)
        self.assertLess(event.value, 10_000)

    def test_periodic_damage_uses_the_same_gateway_without_fake_hitstun(self):
        engine = SideviewCombatEngine()
        actor = _fighter(1)
        target = _fighter(2)
        derived = DerivedStats(
            max_hp=100,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
        )
        target.snapshot = replace(target.snapshot, derived=derived)
        target.current_derived = derived
        target.current_hp = 100
        target.attack_pending = True
        target.pending_skill_id = "fire_ray"
        target.windup_ticks = 2
        state = BattleState(1, actor, target, [], 31)

        applied = AbilityRuntime._deal_tick_damage(
            state,
            actor,
            target,
            1_000,
            event_kind="zone_damage",
            damage_type="fire",
            apply_damage=engine._apply_damage,
            skill_id="fire_zone",
            zone_id="fire_zone:1:1",
        )

        expected = pvp_burst_cap(1_000, 100, ruleset=engine.ruleset)
        self.assertEqual(applied, expected)
        self.assertTrue(target.attack_pending)
        self.assertEqual(target.hitstun_ticks, 0)
        event = next(event for event in state.events if event.kind == "zone_damage")
        self.assertEqual(event.zone_id, "fire_zone:1:1")
        self.assertEqual(event.damage_breakdown, {"fire": expected})

    def test_scythe_secondary_damage_uses_the_engine_damage_gateway(self):
        runtime = AbilityRuntime()
        actor = _fighter(1)
        actor.statuses["scythe_awakening"] = CombatStatus(
            "scythe_awakening", actor.snapshot.user_pk, 50
        )
        actor.runtime_effective_skills["scythe"] = 60
        target = _fighter(2)
        state = BattleState(1, actor, target, [], 23)
        apply_damage = mock.Mock()
        definition = ACTIVE_ABILITY_DEFINITIONS["power_strike"]

        runtime.apply_secondary(
            state,
            actor,
            target,
            definition,
            (100, False, False, 0, definition.ability_id, {"physical": 100}),
            random.Random(1),
            apply_damage=apply_damage,
        )

        self.assertEqual(apply_damage.call_count, 2)
        self.assertEqual(
            {
                next(iter(call.args[3][5]))
                for call in apply_damage.call_args_list
            },
            {"hell", "shadow"},
        )
        self.assertTrue(
            all(call.args[3][0] == 20 for call in apply_damage.call_args_list)
        )
        self.assertEqual(target.current_hp, target.max_hp)


if __name__ == "__main__":
    unittest.main()
