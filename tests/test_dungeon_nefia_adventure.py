import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from models.combat import (
    AIProfile,
    FighterContinuationState,
    FighterSnapshot,
    SimulationResult,
)
from services.dungeon_adventure_service import DungeonAdventureFacade
from services.ability_catalog import SPELL_DEFINITIONS
from services.dungeon_catalog import DungeonCatalog
from services.dungeon_nefia_catalog import (
    DEFAULT_NEFIA_CATALOG_PATH,
    DungeonNefiaCatalog,
)
from services.monster_build_service import MonsterBuildService
from services.monster_catalog import MonsterCatalog
from services.dungeon_snapshot_codec import (
    _continuation_from_dict,
    dump_adventure,
    load_adventure,
)
from models.monster import MonsterSpawnSpec


class DungeonNefiaAdventureTests(unittest.TestCase):
    def setUp(self):
        self.monsters = MonsterCatalog()
        self.monster_builds = MonsterBuildService(self.monsters)
        self.dungeons = DungeonCatalog(monster_catalog=self.monsters)
        self.nefia = DungeonNefiaCatalog(monster_catalog=self.monsters)
        self.facade = DungeonAdventureFacade(
            self.monster_builds,
            self.dungeons,
            nefia_catalog=self.nefia,
        )

    def _write_nefia_catalog(self, payload: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "dungeon_nefia_catalog.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _default_nefia_payload() -> dict:
        return json.loads(
            DEFAULT_NEFIA_CATALOG_PATH.read_text(encoding="utf-8")
        )

    @staticmethod
    def _layout(adventure):
        return tuple(
            tuple(
                (
                    route.option_id,
                    route.monster_template_id,
                    route.environment.environment_id,
                    route.terrain_id,
                    route.discovery.discovery_type,
                    tuple(affix.affix_id for affix in route.affixes),
                )
                for route in floor.routes
            )
            for floor in adventure.floors
        )

    @staticmethod
    def _overpowered_player():
        return FighterSnapshot(
            user_pk=77,
            name="Hero",
            level=1,
            hp=10000,
            atk=5000,
            defense=5000,
            speed=500,
            luck=150,
            strategy="balanced",
        )

    def test_catalog_has_valid_random_rules(self):
        snapshot = self.nefia.snapshot
        self.assertGreaterEqual(len(snapshot.environments), 7)
        self.assertGreaterEqual(len(snapshot.risk_pairs), 4)
        self.assertGreaterEqual(len(snapshot.dungeons), 5)
        self.assertEqual(
            set(snapshot.dungeons),
            set(self.dungeons.snapshot.by_id),
            "每个随机奈菲亚主题都必须有可开局的基础副本定义",
        )
        self.assertIn("verdant_wetland", snapshot.dungeons)
        self.assertEqual(
            set(snapshot.dungeons["ember_outpost"].terrain_pool),
            {"fortress", "tower", "cave"},
        )

        node_ranges = {
            (definition.node_count_min, definition.node_count_max)
            for definition in snapshot.dungeons.values()
        }
        ecology_signatures = {
            (
                frozenset(definition.monster_pool),
                frozenset(definition.elite_pool),
                frozenset(definition.boss_pool),
            )
            for definition in snapshot.dungeons.values()
        }
        environment_signatures = {
            frozenset(definition.environment_pool)
            for definition in snapshot.dungeons.values()
        }
        reward_focuses = {
            frozenset(definition.spellbook_pool)
            for definition in snapshot.dungeons.values()
        }
        self.assertEqual(len(node_ranges), len(snapshot.dungeons))
        self.assertEqual(len(ecology_signatures), len(snapshot.dungeons))
        self.assertEqual(len(environment_signatures), len(snapshot.dungeons))
        self.assertEqual(len(reward_focuses), len(snapshot.dungeons))

        for definition in snapshot.dungeons.values():
            self.assertNotEqual(
                set(definition.monster_pool), set(definition.elite_pool)
            )
            self.assertNotEqual(
                set(definition.elite_pool), set(definition.boss_pool)
            )
            self.assertEqual(
                set(definition.terrain_pool),
                set(definition.terrain_monster_pools),
            )
            for rank_pools in definition.terrain_monster_pools.values():
                self.assertEqual(set(rank_pools), {"normal", "elite"})
            for template_id in (
                *definition.monster_pool,
                *definition.elite_pool,
                *definition.boss_pool,
            ):
                self.monsters.get(template_id)
            self.assertTrue(set(definition.spellbook_pool) <= set(SPELL_DEFINITIONS))

    def test_continuation_immunity_round_trips_and_old_snapshots_default_to_zero(self):
        run = self.facade.start_daily(
            owner_key="codec-owner",
            group_key="codec-group",
            dungeon_id="verdant_wetland",
            player_level=20,
            cycle_key="2026-12-01",
        )
        run = replace(
            run,
            continuation_state=FighterContinuationState(
                hard_control_immunity_ticks=3
            ),
        )

        restored = load_adventure(dump_adventure(run))

        self.assertEqual(
            restored.continuation_state.hard_control_immunity_ticks, 3
        )
        self.assertEqual(
            _continuation_from_dict({}).hard_control_immunity_ticks, 0
        )

    def test_non_boss_monsters_follow_each_terrain_ecology(self):
        for dungeon_id, definition in self.nefia.snapshot.dungeons.items():
            seen_terrains = set()
            for sample in range(80):
                run = self.facade.start_daily(
                    owner_key=f"ecology-owner-{dungeon_id}-{sample}",
                    group_key=f"ecology-group-{dungeon_id}",
                    dungeon_id=dungeon_id,
                    player_level=20,
                    cycle_key=f"ecology-cycle-{sample}",
                )
                for floor in run.floors:
                    for route in floor.routes:
                        with self.subTest(
                            dungeon=dungeon_id,
                            terrain=route.terrain_id,
                            rank=route.monster_rank,
                        ):
                            if route.monster_rank == "boss":
                                self.assertIn(
                                    route.monster_template_id,
                                    definition.boss_pool,
                                )
                                continue
                            seen_terrains.add(route.terrain_id)
                            self.assertIn(
                                route.monster_template_id,
                                definition.terrain_monster_pools[
                                    route.terrain_id
                                ][route.monster_rank],
                            )
                if seen_terrains == set(definition.terrain_monster_pools):
                    break
            self.assertEqual(
                seen_terrains,
                set(definition.terrain_monster_pools),
            )

    def test_missing_terrain_ecology_falls_back_to_dungeon_pools(self):
        payload = self._default_nefia_payload()
        del payload["dungeons"]["verdant_wetland"]["terrain_monster_pools"]
        catalog = DungeonNefiaCatalog(
            self._write_nefia_catalog(payload),
            monster_catalog=self.monsters,
        )
        definition = catalog.get("verdant_wetland")

        self.assertEqual(definition.terrain_monster_pools, {})
        self.assertEqual(
            definition.monster_pool_for("forest", "normal"),
            definition.monster_pool,
        )
        self.assertEqual(
            definition.monster_pool_for("cave", "elite"),
            definition.elite_pool,
        )

        payload = self._default_nefia_payload()
        del payload["dungeons"]["verdant_wetland"][
            "terrain_monster_pools"
        ]["forest"]["elite"]
        catalog = DungeonNefiaCatalog(
            self._write_nefia_catalog(payload),
            monster_catalog=self.monsters,
        )
        definition = catalog.get("verdant_wetland")
        self.assertEqual(
            definition.monster_pool_for("forest", "elite"),
            definition.elite_pool,
        )

    def test_terrain_ecology_rejects_invalid_configuration(self):
        def add_unknown_terrain(pools):
            pools["swamp"] = {"normal": ["monster_001"]}

        def empty_pool(pools):
            pools["forest"]["normal"] = []

        def add_unknown_monster(pools):
            pools["forest"]["normal"] = ["monster_missing"]

        cases = (
            (add_unknown_terrain, "未知地形"),
            (empty_pool, "必须是非空数组"),
            (add_unknown_monster, "引用未知怪物"),
        )
        for mutation, message in cases:
            with self.subTest(message=message):
                payload = self._default_nefia_payload()
                mutation(
                    payload["dungeons"]["verdant_wetland"][
                        "terrain_monster_pools"
                    ]
                )
                with self.assertRaisesRegex(ValueError, message):
                    DungeonNefiaCatalog(
                        self._write_nefia_catalog(payload),
                        monster_catalog=self.monsters,
                    )

    def test_daily_map_is_group_shared_but_personal_run_is_not(self):
        for dungeon_id, definition in self.nefia.snapshot.dungeons.items():
            with self.subTest(dungeon=dungeon_id):
                first = self.facade.start_daily(
                    owner_key=f"u1-{dungeon_id}",
                    group_key="g1",
                    dungeon_id=dungeon_id,
                    player_level=5,
                    cycle_key="2026-08-11",
                )
                second = self.facade.start_daily(
                    owner_key=f"u2-{dungeon_id}",
                    group_key="g1",
                    dungeon_id=dungeon_id,
                    player_level=30,
                    cycle_key="2026-08-11",
                )
                self.assertEqual(self._layout(first), self._layout(second))
                self.assertNotEqual(first.adventure_id, second.adventure_id)
                self.assertNotEqual(first.seed, second.seed)
                self.assertTrue(
                    definition.node_count_min
                    <= len(first.floors)
                    <= definition.node_count_max
                )

    def test_same_cycle_cannot_reroll_seed_or_difficulty(self):
        first = self.facade.start_daily(
            owner_key="u1",
            group_key="g1",
            dungeon_id="verdant_wetland",
            player_level=5,
            cycle_key="2026-08-11",
            difficulty=1,
        )
        retry = self.facade.start_daily(
            owner_key="u1",
            group_key="g1",
            dungeon_id="verdant_wetland",
            player_level=99,
            cycle_key="2026-08-11",
            difficulty=5,
        )
        self.assertEqual(first, retry)
        self.assertEqual(retry.difficulty, 1)

    def test_process_restart_reconstructs_same_map_seed_and_reward_namespace(self):
        args = dict(
            owner_key="stable-owner",
            group_key="stable-group",
            dungeon_id="verdant_wetland",
            player_level=8,
            cycle_key="2026-08-19",
            difficulty=2,
        )
        first = self.facade.start_daily(**args)
        restarted = DungeonAdventureFacade(
            self.monster_builds,
            self.dungeons,
            nefia_catalog=self.nefia,
        ).start_daily(**args)
        self.assertEqual(first.adventure_id, restarted.adventure_id)
        self.assertEqual(first.seed, restarted.seed)
        self.assertEqual(first.settlement_key, restarted.settlement_key)
        self.assertEqual(first.floors, restarted.floors)

    def test_higher_difficulty_raises_enemy_level_and_reward_multiplier(self):
        easy = self.facade.start_daily(
            owner_key="easy",
            group_key="same-map",
            dungeon_id="ember_outpost",
            player_level=15,
            cycle_key="2026-08-20",
            difficulty=1,
        )
        hard = self.facade.start_daily(
            owner_key="hard",
            group_key="same-map",
            dungeon_id="ember_outpost",
            player_level=15,
            cycle_key="2026-08-20",
            difficulty=5,
        )
        easy_route = easy.floors[0].routes[0]
        hard_route = hard.floors[0].routes[0]
        self.assertEqual(easy_route.monster_template_id, hard_route.monster_template_id)
        self.assertGreater(hard_route.monster_level, easy_route.monster_level)
        self.assertGreater(
            hard_route.base_reward_multiplier, easy_route.base_reward_multiplier
        )

    def test_routes_have_decisions_elites_and_final_boss(self):
        run = self.facade.start_daily(
            owner_key="u1",
            group_key="g1",
            dungeon_id="ember_outpost",
            player_level=15,
            cycle_key="2026-08-11",
            difficulty=3,
        )
        for floor in run.floors:
            self.assertEqual(len(floor.routes), 2)
            self.assertNotEqual(
                floor.routes[0].option_id, floor.routes[1].option_id
            )
            self.assertTrue(all(len(route.risk_choices) == 2 for route in floor.routes))
        for floor in run.floors[:-1]:
            self.assertEqual(
                sum(self.facade.requires_combat(route) for route in floor.routes),
                1,
            )
        self.assertTrue(
            all(route.node_kind == "boss" for route in run.floors[-1].routes)
        )
        self.assertTrue(
            all(len(route.affixes) == 2 for route in run.floors[-1].routes)
        )

    def test_hidden_rooms_accept_skill_or_traversal_magic(self):
        hidden = None
        for day in range(1, 50):
            run = self.facade.start_daily(
                owner_key=f"u{day}",
                group_key="hidden-search",
                dungeon_id="ember_outpost",
                player_level=15,
                cycle_key=f"2026-09-{day:02d}",
            )
            hidden = next(
                (
                    route
                    for floor in run.floors
                    for route in floor.routes
                    if route.discovery.discovery_type == "hidden_room"
                ),
                None,
            )
            if hidden:
                break
        self.assertIsNotNone(hidden)
        self.assertFalse(self.facade.can_access_discovery(hidden))
        self.assertTrue(
            self.facade.can_access_discovery(hidden, capabilities=("teleport",))
        )
        self.assertTrue(
            self.facade.can_access_discovery(
                hidden,
                capabilities=("detect_invisible",),
            )
        )
        self.assertTrue(
            self.facade.can_access_discovery(
                hidden,
                exploration_skills={
                    hidden.discovery.skill_id: hidden.discovery.skill_threshold
                },
            )
        )

    def test_route_risk_and_retreat_are_explicit_state_transitions(self):
        run = self.facade.start_daily(
            owner_key="u1",
            group_key="g1",
            dungeon_id="verdant_wetland",
            player_level=5,
            cycle_key="2026-08-12",
        )
        route = run.current_floor.routes[0]
        run = self.facade.choose_route(run.adventure_id, route.option_id)
        self.assertEqual(run.phase, "risk_choice")
        run = self.facade.choose_risk(
            run.adventure_id, route.risk_choices[0].risk_id
        )
        self.assertEqual(run.phase, "combat_ready")
        result = self.facade.retreat(run.adventure_id)
        self.assertEqual(result.adventure.phase, "retreated")
        self.assertTrue(result.settlement_ready)
        self.assertEqual(
            {intent.reward_type for intent in result.newly_earned_intents},
            {"experience", "salvage"},
        )
        with self.assertRaises(ValueError):
            self.facade.retreat(run.adventure_id)

    def test_actual_sideview_combat_uses_route_environment(self):
        run = self.facade.start_daily(
            owner_key="external-platform-id",
            group_key="g-combat",
            dungeon_id="verdant_wetland",
            player_level=1,
            cycle_key="2026-08-13",
        )
        route = next(
            item for item in run.current_floor.routes
            if self.facade.requires_combat(item)
        )
        self.facade.choose_route(run.adventure_id, route.option_id)
        self.facade.choose_risk(run.adventure_id, route.risk_choices[0].risk_id)
        result = self.facade.fight(
            run.adventure_id,
            self._overpowered_player(),
            AIProfile(),
        )
        self.assertEqual(
            result.simulation.environment_id,
            route.environment.combat_environment_id,
        )
        self.assertTrue(result.adventure.encounters)
        self.assertTrue(result.newly_earned_intents)
        with self.assertRaises(ValueError):
            self.facade.fight(
                run.adventure_id,
                self._overpowered_player(),
                AIProfile(),
            )

    def test_event_route_advances_without_calling_combat_engine(self):
        run = self.facade.start_daily(
            owner_key="event-owner",
            group_key="event-group",
            dungeon_id="verdant_wetland",
            player_level=12,
            cycle_key="2026-08-24",
        )
        route = next(
            item for item in run.current_floor.routes
            if not self.facade.requires_combat(item)
        )
        risk = route.risk_choices[0]

        class FailingCombatEngine:
            @staticmethod
            def simulate(*args, **kwargs):
                raise AssertionError("event node must not invoke combat")

        self.facade.combat_engine = FailingCombatEngine()
        self.facade.choose_route(run.adventure_id, route.option_id)
        self.facade.choose_risk(run.adventure_id, risk.risk_id)
        result = self.facade.fight(
            run.adventure_id,
            self._overpowered_player(),
            AIProfile(),
        )

        self.assertIsNone(result.simulation)
        self.assertTrue(result.narrative)
        self.assertEqual(result.adventure.completed_floors, 1)
        self.assertIsNone(result.adventure.encounters[-1].simulation)
        self.assertEqual(
            {intent.reward_type for intent in result.newly_earned_intents}
            & {"experience", "salvage"},
            {"experience", "salvage"},
        )

    def test_event_story_variant_is_personal_stable_and_persisted(self):
        args = dict(
            owner_key="event-story-owner",
            group_key="event-story-group",
            dungeon_id="verdant_wetland",
            player_level=12,
            cycle_key="2026-12-01",
        )
        first = self.facade.start_daily(**args)
        route = next(
            item for item in first.current_floor.routes
            if not self.facade.requires_combat(item)
        )
        risk = route.risk_choices[0]
        accessible = self.facade.can_access_discovery(route)
        granted = self.facade.event_access_granted(
            route,
            risk,
            discovery_accessible=accessible,
        )
        variant = self.facade._event_story_variant(
            first,
            route,
            risk,
            discovery_accessible=accessible,
            access_granted=granted,
        )
        restarted = DungeonAdventureFacade(
            self.monster_builds,
            self.dungeons,
            nefia_catalog=self.nefia,
        ).start_daily(**args)
        replayed = self.facade._event_story_variant(
            restarted,
            route,
            risk,
            discovery_accessible=accessible,
            access_granted=granted,
        )
        self.assertEqual(variant, replayed)

        self.facade.choose_route(first.adventure_id, route.option_id)
        self.facade.choose_risk(first.adventure_id, risk.risk_id)
        result = self.facade.fight(
            first.adventure_id,
            self._overpowered_player(),
            AIProfile(),
        )
        self.assertIn(variant.narrative, result.narrative)
        for intent in result.newly_earned_intents:
            self.assertEqual(
                intent.metadata.get("story_variant"),
                variant.variant_id,
            )
        self.assertEqual(
            result.adventure.encounters[-1].narrative,
            result.narrative,
        )

    def test_event_story_effects_are_bounded_and_risk_consistent(self):
        seen_cautious = set()
        seen_risky = set()
        for day in range(1, 121):
            adventure = self.facade.start_daily(
                owner_key=f"story-bounds-{day}",
                group_key=f"story-bounds-group-{day}",
                dungeon_id="ember_outpost",
                player_level=20,
                cycle_key=f"2027-01-{day:03d}",
            )
            route = next(
                item for floor in adventure.floors[:-1] for item in floor.routes
                if item.node_kind in {"camp", "remains", "hidden_room", "treasure"}
            )
            accessible = self.facade.can_access_discovery(route)
            for risk in route.risk_choices:
                granted = self.facade.event_access_granted(
                    route,
                    risk,
                    discovery_accessible=accessible,
                )
                variant = self.facade._event_story_variant(
                    adventure,
                    route,
                    risk,
                    discovery_accessible=accessible,
                    access_granted=granted,
                )
                self.assertGreaterEqual(variant.hp_delta, -0.04)
                self.assertLessEqual(variant.hp_delta, 0.04)
                self.assertGreaterEqual(variant.mana_delta, -0.02)
                self.assertLessEqual(variant.mana_delta, 0.04)
                self.assertGreaterEqual(variant.stamina_delta, 0.0)
                self.assertLessEqual(variant.stamina_delta, 0.04)
                self.assertIn(variant.salvage_bonus, {0, 1, 2})
                target = (
                    seen_cautious
                    if risk.risk_id in {
                        "rest_at_camp",
                        "bury_remains",
                        "inspect_the_seal",
                        "open_carefully",
                    }
                    else seen_risky
                )
                target.add(variant.variant_id)
                if target is seen_cautious:
                    self.assertGreaterEqual(variant.hp_delta, 0.0)
                    self.assertGreaterEqual(variant.mana_delta, 0.0)
        self.assertTrue(seen_cautious)
        self.assertTrue(seen_risky)

    def test_hidden_room_ability_reduces_real_entry_cost(self):
        hidden = None
        for day in range(1, 60):
            run = self.facade.start_daily(
                owner_key=f"hidden-cost-{day}",
                group_key="hidden-cost-group",
                dungeon_id="ember_outpost",
                player_level=20,
                cycle_key=f"2026-10-{day:02d}",
            )
            hidden = next(
                (
                    route
                    for floor in run.floors[:-1]
                    for route in floor.routes
                    if route.node_kind == "hidden_room"
                ),
                None,
            )
            if hidden is not None:
                break
        self.assertIsNotNone(hidden)
        force = next(
            risk for risk in hidden.risk_choices
            if risk.risk_id == "force_the_passage"
        )
        raw_hp, raw_mp, raw_mitigated = self.facade.effective_entry_cost(
            hidden, force, discovery_accessible=False
        )
        solved_hp, solved_mp, solved_mitigated = self.facade.effective_entry_cost(
            hidden, force, discovery_accessible=True
        )
        self.assertFalse(raw_mitigated)
        self.assertTrue(solved_mitigated)
        self.assertLess(solved_hp, raw_hp)
        self.assertLess(solved_mp, raw_mp)

    def test_elite_affixes_change_real_monster_stats(self):
        base = self.monster_builds.build(
            MonsterSpawnSpec("monster_001", 20, "elite", -991)
        ).snapshot
        ironclad = self.facade._affixed_snapshot(
            base, (self.nefia.snapshot.affixes["ironclad"],)
        )
        swift = self.facade._affixed_snapshot(
            base, (self.nefia.snapshot.affixes["swift"],)
        )
        ferocious = self.facade._affixed_snapshot(
            base, (self.nefia.snapshot.affixes["ferocious"],)
        )
        self.assertGreater(ironclad.derived.defense, base.derived.defense)
        self.assertGreater(
            ironclad.derived.physical_reduction,
            base.derived.physical_reduction,
        )
        self.assertGreater(
            ironclad.derived.magical_reduction,
            base.derived.magical_reduction,
        )
        self.assertGreater(swift.derived.action_speed, base.derived.action_speed)
        self.assertGreater(swift.derived.evasion, base.derived.evasion)
        self.assertGreater(ferocious.derived.attack_power, base.derived.attack_power)

    def test_defeat_also_has_non_rerollable_consolation(self):
        run = self.facade.start_daily(
            owner_key="weak",
            group_key="g-defeat",
            dungeon_id="ember_outpost",
            player_level=1,
            cycle_key="2026-08-21",
            difficulty=5,
        )
        route = run.current_floor.routes[0]
        self.facade.choose_route(run.adventure_id, route.option_id)
        self.facade.choose_risk(run.adventure_id, route.risk_choices[1].risk_id)

        class LosingCombatEngine:
            @staticmethod
            def simulate(
                attacker,
                defender,
                _attacker_profile,
                _defender_profile,
                random_seed,
                attacker_initial_state=None,
                _defender_initial_state=None,
                *,
                environment_id="calm",
            ):
                initial = attacker_initial_state or FighterContinuationState()
                return SimulationResult(
                    attacker=attacker,
                    defender=defender,
                    winner_pk=defender.user_pk,
                    loser_pk=attacker.user_pk,
                    duration_ticks=12,
                    finish_reason="hp_depleted",
                    attacker_remaining_hp=0,
                    defender_remaining_hp=max(1, defender.max_hp // 2),
                    attacker_damage_dealt=1,
                    defender_damage_dealt=max(1, attacker.max_hp),
                    events=(),
                    random_seed=random_seed,
                    attacker_final_state=replace(initial, defeated=True),
                    defender_final_state=FighterContinuationState(),
                    ruleset_id="sideview-v11",
                    environment_id=environment_id,
                )

        self.facade.combat_engine = LosingCombatEngine()
        weak = FighterSnapshot(1, "weak", 1, 1, 0, 0, 0, 0, "balanced")
        result = self.facade.fight(run.adventure_id, weak, AIProfile())
        self.assertEqual(result.adventure.phase, "defeated")
        consolation = [
            intent
            for intent in result.newly_earned_intents
            if intent.metadata.get("consolation")
        ]
        self.assertEqual(
            {intent.reward_type for intent in consolation},
            {"experience", "salvage"},
        )
        resumed = self.facade.start_daily(
            owner_key="weak",
            group_key="g-defeat",
            dungeon_id="ember_outpost",
            player_level=99,
            cycle_key="2026-08-21",
            difficulty=1,
        )
        self.assertEqual(resumed.phase, "defeated")
        self.assertEqual(resumed.reward_intents, result.adventure.reward_intents)

    def test_clear_has_guaranteed_boss_equipment_and_spellbook(self):
        run = self.facade.start_daily(
            owner_key="u-clear",
            group_key="g-clear",
            dungeon_id="verdant_wetland",
            player_level=1,
            cycle_key="2026-08-14",
        )
        while not run.terminal:
            route = run.current_floor.routes[0]
            run = self.facade.choose_route(run.adventure_id, route.option_id)
            run = self.facade.choose_risk(
                run.adventure_id, route.risk_choices[0].risk_id
            )
            result = self.facade.fight(
                run.adventure_id,
                self._overpowered_player(),
                AIProfile(),
                capabilities=("teleport", "fire_wall"),
                exploration_skills={
                    "reading": 50,
                    "natural_knowledge": 50,
                    "concealment": 50,
                    "weightlifting": 50,
                },
            )
            run = result.adventure
        self.assertEqual(run.phase, "cleared")
        boss_rewards = [
            intent
            for intent in run.reward_intents
            if intent.metadata.get("guaranteed_boss_reward")
        ]
        self.assertEqual(
            {intent.reward_type for intent in boss_rewards},
            {"equipment", "spellbook"},
        )
        boss_equipment = next(
            intent for intent in boss_rewards if intent.reward_type == "equipment"
        )
        self.assertEqual(boss_equipment.quantity, 1)
        self.assertEqual(len({i.source_key for i in run.reward_intents}), len(run.reward_intents))

    def test_rare_equipment_find_bonus_is_lightweight_and_capped(self):
        self.assertEqual(
            self.facade._rare_equipment_find_bonuses(0.15),
            (0.0375, 0.06),
        )
        self.assertEqual(
            self.facade._rare_equipment_find_bonuses(9.0),
            (0.075, 0.12),
        )
        self.assertEqual(
            self.facade._rare_equipment_find_bonuses(-1.0),
            (0.0, 0.0),
        )

    def test_pve_stealth_is_stable_bounded_and_harder_on_bosses(self):
        run = self.facade.start_daily(
            owner_key="stealth-proof",
            group_key="stealth-group",
            dungeon_id="verdant_wetland",
            player_level=30,
            cycle_key="2026-11-01",
        )
        normal = next(
            route
            for floor in run.floors[:-1]
            for route in floor.routes
            if self.facade.requires_combat(route)
            and route.monster_rank != "boss"
        )
        boss = run.floors[-1].routes[0]

        self.assertEqual(
            self.facade._pve_stealth_opening_ticks(run, normal, 0.0),
            0,
        )
        first = self.facade._pve_stealth_opening_ticks(run, normal, 9.0)
        replay = self.facade._pve_stealth_opening_ticks(run, normal, 0.50)
        boss_result = self.facade._pve_stealth_opening_ticks(run, boss, 0.50)
        self.assertEqual(first, replay)
        self.assertIn(first, (0, 2))
        self.assertIn(boss_result, (0, 2))

        # Across many stable daily seeds, the rank modifier must make bosses
        # materially harder to catch unaware than ordinary encounters.
        normal_hits = boss_hits = 0
        samples = 300
        for index in range(samples):
            candidate = replace(run, seed=index + 1)
            normal_hits += bool(
                self.facade._pve_stealth_opening_ticks(
                    candidate, normal, 0.50
                )
            )
            boss_hits += bool(
                self.facade._pve_stealth_opening_ticks(
                    candidate, boss, 0.50
                )
            )
        self.assertGreater(normal_hits, boss_hits)


if __name__ == "__main__":
    unittest.main()
