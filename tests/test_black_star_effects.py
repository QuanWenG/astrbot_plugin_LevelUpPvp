import random
import types
import unittest

from models.combat import BattleState, FighterSnapshot, FighterState
from models.equipment import EquipmentProc
from services.combat_engine import SideviewCombatEngine
from services.equipment_catalog import DEFAULT_EQUIPMENT_CATALOG


class _AlwaysTrigger(random.Random):
    def random(self):
        return 0.0


def _equipment(*, effects=None, procs=(), attack_range=100):
    return types.SimpleNamespace(
        combat_effects=dict(effects or {}),
        equipment_procs=tuple(procs),
        armor_style="light",
        total_weight=1.0,
        overloaded=False,
        max_stamina=100,
        attack_range=attack_range,
        attack_recovery=2,
        attack_cooldown=6,
        physical_accuracy_multiplier=1.0,
        spell_accuracy_multiplier=1.0,
        weapon_mode="one_hand",
        weapon_type="longsword",
    )


def _fighter(user_pk, equipment=None, *, hp=10):
    return FighterSnapshot(
        user_pk=user_pk,
        name=str(user_pk),
        level=40,
        hp=hp,
        atk=8,
        defense=5,
        speed=5,
        luck=5,
        strategy="test",
        equipment=equipment,
    )


class BlackStarCatalogTests(unittest.TestCase):
    def test_twenty_classic_black_stars_have_clean_flavor_descriptions(self):
        entries = [
            DEFAULT_EQUIPMENT_CATALOG.get(catalog_id)
            for catalog_id in range(4001, 4021)
        ]

        self.assertEqual(len(entries), 20)
        self.assertEqual(
            {entry.fixed["item_level"] for entry in entries},
            {40},
        )
        for entry in entries:
            self.assertFalse(
                any(
                    word in entry.template.description
                    for word in ("适配", "改为", "未实现")
                )
            )

    def test_signature_effects_match_researched_catalog_mapping(self):
        ether = DEFAULT_EQUIPMENT_CATALOG.get(4001).template
        diabolos = DEFAULT_EQUIPMENT_CATALOG.get(4004).template
        railgun = DEFAULT_EQUIPMENT_CATALOG.get(4017).template
        boots = DEFAULT_EQUIPMENT_CATALOG.get(4020).template

        self.assertIn(
            {
                "type": "damage_lightning",
                "value": 4,
                "capacity": 0,
            },
            ether.inherent_affixes,
        )
        self.assertTrue(
            any(
                affix.get("ability_id") == "time_stop"
                and affix["value"] == 0.04
                for affix in diabolos.inherent_affixes
            )
        )
        self.assertEqual(railgun.material, "ether")
        self.assertTrue(
            any(
                affix.get("ability_id") == "roaring_wave"
                for affix in railgun.inherent_affixes
            )
        )
        self.assertEqual(boots.inherent_affixes, ())
        self.assertEqual(
            boots.source_effects,
            ("世界地图旅行速度+63%",),
        )


class EquipmentProcCombatTests(unittest.TestCase):
    def setUp(self):
        self.engine = SideviewCombatEngine()

    def _state(self, equipment, *, target_hp=100):
        actor_snapshot = _fighter(1, equipment)
        target_snapshot = _fighter(2)
        actor = FighterState(
            actor_snapshot,
            80,
            450,
            stamina=0,
            mana=0,
        )
        target = FighterState(target_snapshot, target_hp, 500)
        state = BattleState(5, actor, target, [], 123)
        return state, actor, target

    def test_time_stop_proc_uses_shared_status_runtime(self):
        proc = EquipmentProc(
            "black_star_diabolos",
            "trigger_ability",
            "enemy",
            1.0,
            "time_stop",
            200,
        )
        state, actor, target = self._state(_equipment(procs=(proc,)))

        self.engine.equipment_proc_resolver.resolve(
            state,
            actor,
            target,
            (20, False, False, 0, None, {"physical": 20}),
            _AlwaysTrigger(),
            self.engine._apply_damage,
        )

        # Equipment procs share the active ruleset's hard-control contract.
        status_rules = self.engine.ruleset.status
        self.assertEqual(
            target.statuses["stun"].remaining_ticks,
            status_rules.hard_control_duration_cap_ticks,
        )
        self.assertEqual(
            target.hard_control_immunity_until,
            state.tick
            + status_rules.hard_control_duration_cap_ticks
            + status_rules.post_control_immunity_ticks,
        )
        self.assertTrue(
            any(
                event.kind == "equipment_proc"
                and event.skill_id == "time_stop"
                for event in state.events
            )
        )

    def test_only_basic_attacks_and_physical_techniques_are_direct_hits(self):
        definitions = {
            "physical_skill": types.SimpleNamespace(
                ability_type="technique",
                effects=(types.SimpleNamespace(effect_type="physical_damage"),),
            ),
            "spell": types.SimpleNamespace(
                ability_type="spell",
                effects=(types.SimpleNamespace(effect_type="magic_damage"),),
            ),
        }
        actor = types.SimpleNamespace(
            snapshot=types.SimpleNamespace(
                skills=types.SimpleNamespace(active_definitions=definitions)
            )
        )

        self.assertTrue(
            self.engine._is_direct_weapon_hit(
                actor, (10, False, False, 0, None, {"physical": 10})
            )
        )
        self.assertTrue(
            self.engine._is_direct_weapon_hit(
                actor,
                (10, False, False, 0, "physical_skill", {"physical": 10}),
            )
        )
        self.assertFalse(
            self.engine._is_direct_weapon_hit(
                actor, (10, False, False, 0, "spell", {"fire": 10})
            )
        )

    def test_resource_steal_caps_and_execute_resolve_on_direct_hit(self):
        equipment = _equipment(
            effects={
                "stamina_steal": 0.50,
                "mana_steal": 0.50,
                "execute_chance": 1.0,
            }
        )
        state, actor, target = self._state(equipment, target_hp=10)
        actor.current_derived = types.SimpleNamespace(
            max_hp=100,
            max_mp=100,
            max_sp=100,
        )
        target.current_derived = types.SimpleNamespace(
            max_hp=100,
            max_mp=0,
            max_sp=100,
        )

        self.engine.equipment_proc_resolver.resolve(
            state,
            actor,
            target,
            (100, False, False, 0, None, {"physical": 100}),
            _AlwaysTrigger(),
            self.engine._apply_damage,
        )

        self.assertEqual((actor.stamina, actor.mana), (10, 10))
        self.assertEqual(target.current_hp, 0)
        self.assertTrue(any(event.kind == "execute" for event in state.events))

    def test_ragnarok_zone_can_damage_both_fighters(self):
        proc = EquipmentProc(
            "black_star_ragnarok",
            "trigger_ability",
            "enemy",
            1.0,
            "ragnarok",
        )
        state, actor, target = self._state(_equipment(procs=(proc,)))
        actor_hp = actor.current_hp
        target_hp = target.current_hp

        self.engine.equipment_proc_resolver.resolve(
            state,
            actor,
            target,
            (20, False, False, 0, None, {"physical": 20}),
            _AlwaysTrigger(),
            self.engine._apply_damage,
        )
        after_burst = target.current_hp
        self.engine.ability_runtime.tick(state, _AlwaysTrigger())

        self.assertLess(after_burst, target_hp)
        self.assertLess(actor.current_hp, actor_hp)
        self.assertLess(target.current_hp, after_burst)
        self.assertTrue(state.zones[0].affects_owner)

    def test_specific_equipment_status_resistance_is_applied(self):
        target_equipment = _equipment(
            effects={"status_resistance_paralysis": 0.75}
        )
        actor_snapshot = _fighter(1)
        target_snapshot = _fighter(2, target_equipment)
        actor = FighterState(actor_snapshot, 100, 450)
        target = FighterState(target_snapshot, 100, 500)
        state = BattleState(1, actor, target, [], 1)
        effect = types.SimpleNamespace(
            status_id="paralysis",
            params={},
            chance=1.0,
            duration_ticks=20,
            value=0.0,
        )

        applied = self.engine.ability_runtime.apply_status(
            state,
            target,
            effect,
            actor.snapshot.user_pk,
            random.Random(0),
        )

        self.assertFalse(applied)
        self.assertNotIn("paralysis", target.statuses)
