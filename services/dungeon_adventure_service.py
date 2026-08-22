"""Interactive random-Nefia application service.

This module owns dungeon generation and lifecycle only.  Combat is delegated to
``SideviewCombatEngine`` and loot is emitted as deterministic reward intents for
an external transaction/settlement service.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, replace
from typing import Protocol

try:
    from ..models.combat import AIProfile, FighterContinuationState, FighterSnapshot
    from ..models.dungeon import (
        DungeonActionResult,
        DungeonAdventure,
        DungeonAffix,
        DungeonDiscovery,
        DungeonEncounterRecord,
        DungeonFloor,
        DungeonRewardIntent,
        DungeonRiskChoice,
        DungeonRouteOption,
    )
    from .combat_engine import SideviewCombatEngine
    from .combat_random import KeyedEntropy
    from .dungeon_catalog import DungeonCatalog, DungeonDefinition
    from .dungeon_nefia_catalog import DungeonNefiaCatalog, NefiaDefinition
    from .monster_build_service import MonsterBuildService
    from ..models.monster import MonsterSpawnSpec
except ImportError:
    from models.combat import AIProfile, FighterContinuationState, FighterSnapshot
    from models.dungeon import (
        DungeonActionResult,
        DungeonAdventure,
        DungeonAffix,
        DungeonDiscovery,
        DungeonEncounterRecord,
        DungeonFloor,
        DungeonRewardIntent,
        DungeonRiskChoice,
        DungeonRouteOption,
    )
    from services.combat_engine import SideviewCombatEngine
    from services.combat_random import KeyedEntropy
    from services.dungeon_catalog import DungeonCatalog, DungeonDefinition
    from services.dungeon_nefia_catalog import DungeonNefiaCatalog, NefiaDefinition
    from services.monster_build_service import MonsterBuildService
    from models.monster import MonsterSpawnSpec


TERRAIN_NAMES = {
    "forest": "森林",
    "cave": "洞窟",
    "fortress": "要塞",
    "tower": "高塔",
}

COMBAT_NODE_KINDS = frozenset({"normal", "elite", "boss"})

EVENT_KIND_BY_DISCOVERY = {
    "camp": "camp",
    "remains": "remains",
    "gathering_point": "gathering",
    "material_cache": "gathering",
    "gem_cache": "gathering",
    "hidden_room": "hidden_room",
    "ordinary_chest": "treasure",
    "mystery_chest": "treasure",
}

DETECT_INVISIBLE_CAPABILITY = "detect_invisible"
RARE_EQUIPMENT_CHANCE_BONUS_CAP = 0.08
RARE_EQUIPMENT_QUALITY_BONUS_CAP = 0.12
PVE_STEALTH_CAP = 0.50
PVE_STEALTH_OPENING_TICKS = 2


@dataclass(frozen=True)
class _EventStoryVariant:
    variant_id: str
    narrative: str
    hp_delta: float = 0.0
    mana_delta: float = 0.0
    stamina_delta: float = 0.0
    salvage_bonus: int = 0


class DungeonAdventureRepository(Protocol):
    def find_cycle(
        self, owner_key: str, dungeon_id: str, cycle_key: str
    ) -> DungeonAdventure | None: ...

    def get(self, adventure_id: str) -> DungeonAdventure: ...

    def add(self, adventure: DungeonAdventure) -> DungeonAdventure: ...

    def save(
        self, adventure: DungeonAdventure, expected_version: int
    ) -> DungeonAdventure: ...


class InMemoryDungeonAdventureRepository:
    """Thread-safe reference repository.

    Production can replace this with a DB adapter without changing generation
    or combat.  The unique cycle index is the anti-reroll invariant.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, DungeonAdventure] = {}
        self._cycles: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    def find_cycle(
        self, owner_key: str, dungeon_id: str, cycle_key: str
    ) -> DungeonAdventure | None:
        with self._lock:
            adventure_id = self._cycles.get((owner_key, dungeon_id, cycle_key))
            return self._by_id.get(adventure_id) if adventure_id else None

    def get(self, adventure_id: str) -> DungeonAdventure:
        with self._lock:
            try:
                return self._by_id[adventure_id]
            except KeyError as exc:
                raise KeyError(f"未知奈菲亚探险：{adventure_id}") from exc

    def add(self, adventure: DungeonAdventure) -> DungeonAdventure:
        key = (adventure.owner_key, adventure.dungeon_id, adventure.cycle_key)
        with self._lock:
            existing_id = self._cycles.get(key)
            if existing_id:
                return self._by_id[existing_id]
            self._cycles[key] = adventure.adventure_id
            self._by_id[adventure.adventure_id] = adventure
            return adventure

    def save(
        self, adventure: DungeonAdventure, expected_version: int
    ) -> DungeonAdventure:
        with self._lock:
            current = self.get(adventure.adventure_id)
            if current.version != expected_version:
                raise RuntimeError("奈菲亚状态已被其他操作更新，请重新查看路线")
            if adventure.version != expected_version + 1:
                raise ValueError("奈菲亚版本必须严格递增")
            self._by_id[adventure.adventure_id] = adventure
            return adventure


class DungeonAdventureFacade:
    """Stable high-level API for handlers and future persistence adapters."""

    RULESET_ID = "dungeon-nefia-v1"

    def __init__(
        self,
        monster_build_service: MonsterBuildService,
        dungeon_catalog: DungeonCatalog,
        *,
        combat_engine: SideviewCombatEngine | None = None,
        nefia_catalog: DungeonNefiaCatalog | None = None,
        repository: DungeonAdventureRepository | None = None,
    ) -> None:
        self.monster_build_service = monster_build_service
        self.dungeon_catalog = dungeon_catalog
        self.combat_engine = combat_engine or SideviewCombatEngine()
        self.nefia_catalog = nefia_catalog or DungeonNefiaCatalog(
            monster_catalog=monster_build_service.catalog
        )
        self.repository = repository or InMemoryDungeonAdventureRepository()

    def start_daily(
        self,
        *,
        owner_key: str,
        group_key: str,
        dungeon_id: str,
        player_level: int,
        cycle_key: str,
        difficulty: int = 1,
        capabilities: tuple[str, ...] = (),
        exploration_skills: dict[str, int] | None = None,
    ) -> DungeonAdventure:
        """Start or idempotently resume today's personal run on a shared map.

        ``cycle_key`` must be supplied by the operation layer (normally its
        04:00-boundary date).  The same owner/dungeon/cycle can never generate a
        second seed, even after defeat or retreat.
        """

        owner_key = str(owner_key).strip()
        group_key = str(group_key).strip()
        cycle_key = str(cycle_key).strip()
        if not owner_key or not group_key or not cycle_key:
            raise ValueError("owner_key、group_key与cycle_key不能为空")
        if not 1 <= int(player_level) <= 280:
            raise ValueError("玩家等级必须在1到280之间")
        if not 1 <= int(difficulty) <= 5:
            raise ValueError("奈菲亚难度必须在1到5之间")
        existing = self.repository.find_cycle(owner_key, dungeon_id, cycle_key)
        if existing:
            return existing

        definition = self.dungeon_catalog.get(dungeon_id)
        nefia = self.nefia_catalog.get(dungeon_id)
        map_seed = self._seed("map", group_key, dungeon_id, cycle_key)
        personal_seed = self._seed("run", owner_key, map_seed)
        adventure_id = self._token("nefia", owner_key, dungeon_id, cycle_key)
        floors = self._generate_floors(
            definition, nefia, map_seed, int(player_level), int(difficulty)
        )
        # Route topology remains group-shared.  Discovery access is evaluated
        # from the current build at inspection/fight time, while reward source
        # keys remain tied only to this immutable daily expedition.
        adventure = DungeonAdventure(
            adventure_id=adventure_id,
            # Never include mutable build/capability state here.  A process
            # restart or a newly learned spell must not mint a second set of
            # reward keys for the same daily expedition.
            settlement_key=f"dungeon:{adventure_id}",
            owner_key=owner_key,
            group_key=group_key,
            dungeon_id=dungeon_id,
            cycle_key=cycle_key,
            seed=personal_seed,
            player_level=int(player_level),
            difficulty=int(difficulty),
            floors=floors,
        )
        return self.repository.add(adventure)

    def get(self, adventure_id: str) -> DungeonAdventure:
        return self.repository.get(adventure_id)

    @staticmethod
    def can_access_discovery(
        route: DungeonRouteOption,
        *,
        capabilities: tuple[str, ...] = (),
        exploration_skills: dict[str, int] | None = None,
    ) -> bool:
        discovery = route.discovery
        if discovery is None:
            return True
        known = set(capabilities)
        skills = exploration_skills or {}
        ability_ok = not discovery.unlock_any or bool(
            known.intersection(discovery.unlock_any)
        )
        skill_ok = (
            not discovery.skill_id
            or int(skills.get(discovery.skill_id, 0)) >= discovery.skill_threshold
        )
        # Hidden rooms accept any listed traversal magic OR the related skill;
        # gathering nodes require their skill and ignore ability shortcuts.
        if discovery.discovery_type == "hidden_room":
            return (
                DETECT_INVISIBLE_CAPABILITY in known
                or ability_ok
                or skill_ok
            )
        return ability_ok and skill_ok

    @staticmethod
    def requires_combat(route: DungeonRouteOption) -> bool:
        return route.node_kind in COMBAT_NODE_KINDS

    def choose_route(
        self, adventure_id: str, option_id: str
    ) -> DungeonAdventure:
        adventure = self.get(adventure_id)
        self._require_phase(adventure, "route_choice")
        floor = adventure.current_floor
        if floor is None:
            raise RuntimeError("奈菲亚没有可选择的当前节点")
        route = next(
            (item for item in floor.routes if item.option_id == option_id), None
        )
        if route is None:
            raise ValueError("该路线不属于当前层")
        updated = replace(
            adventure,
            phase="risk_choice",
            selected_route_id=route.option_id,
            selected_risk_id=None,
            version=adventure.version + 1,
        )
        return self.repository.save(updated, adventure.version)

    def choose_risk(
        self, adventure_id: str, risk_id: str
    ) -> DungeonAdventure:
        adventure = self.get(adventure_id)
        self._require_phase(adventure, "risk_choice")
        route = self._selected_route(adventure)
        if risk_id not in {item.risk_id for item in route.risk_choices}:
            raise ValueError("该风险选项不属于当前路线")
        updated = replace(
            adventure,
            phase="combat_ready",
            selected_risk_id=risk_id,
            version=adventure.version + 1,
        )
        return self.repository.save(updated, adventure.version)

    def fight(
        self,
        adventure_id: str,
        player_snapshot: FighterSnapshot,
        player_profile: AIProfile,
        *,
        capabilities: tuple[str, ...] = (),
        exploration_skills: dict[str, int] | None = None,
        rare_equipment_find_bonus: float = 0.0,
        pve_stealth: float = 0.0,
    ) -> DungeonActionResult:
        adventure = self.get(adventure_id)
        self._require_phase(adventure, "combat_ready")
        route = self._selected_route(adventure)
        risk = self._selected_risk(adventure, route)
        if not self.requires_combat(route):
            return self._resolve_event(
                adventure,
                route,
                risk,
                capabilities=capabilities,
                exploration_skills=exploration_skills or {},
                rare_equipment_find_bonus=rare_equipment_find_bonus,
            )
        floor_index = adventure.floor_index
        level = self.effective_monster_level(route, risk)
        monster = self.monster_build_service.build(
            MonsterSpawnSpec(
                route.monster_template_id,
                level,
                route.monster_rank,
                -(1_000_000 + (adventure.seed % 800_000) + floor_index * 2),
            )
        )
        monster_snapshot = self._affixed_snapshot(monster.snapshot, route.affixes)
        monster_profile = self._affixed_profile(monster.ai_profile, route.affixes)
        entry_hp_cost, entry_mp_cost, _ = self.effective_entry_cost(
            route,
            risk,
            discovery_accessible=self.can_access_discovery(
                route,
                capabilities=capabilities,
                exploration_skills=exploration_skills or {},
            ),
        )
        initial = self._entry_state(
            adventure.continuation_state,
            entry_hp_cost,
            entry_mp_cost,
        )
        battle_seed = self._seed(
            "battle", adventure.seed, floor_index, route.option_id
        )
        stealth_opening_ticks = self._pve_stealth_opening_ticks(
            adventure,
            route,
            pve_stealth,
        )
        simulation = self.combat_engine.simulate(
            player_snapshot,
            monster_snapshot,
            player_profile,
            monster_profile,
            battle_seed,
            initial,
            (
                FighterContinuationState(
                    recovery_ticks=stealth_opening_ticks,
                )
                if stealth_opening_ticks else None
            ),
            environment_id=route.environment.combat_environment_id,
        )
        narrative = ""
        if stealth_opening_ticks:
            narrative = (
                f"你借{route.terrain_name}地形隐去行踪，"
                f"抢到先手；敌人开场迟疑{stealth_opening_ticks}拍。"
            )
        won = simulation.winner_pk == player_snapshot.user_pk
        record = DungeonEncounterRecord(
            floor_index,
            route.option_id,
            risk.risk_id,
            route.monster_template_id,
            route.monster_rank,
            simulation.environment_id,
            tuple(item.affix_id for item in route.affixes),
            won,
            simulation,
            narrative,
        )
        earned = self._encounter_rewards(
            adventure,
            route,
            risk,
            won,
            capabilities=capabilities,
            exploration_skills=exploration_skills or {},
            rare_equipment_find_bonus=rare_equipment_find_bonus,
        )
        encounters = adventure.encounters + (record,)
        rewards = adventure.reward_intents + earned
        equipment_misses, spellbook_misses = self._loot_pity_after(
            adventure,
            route,
            earned,
            successful=won,
        )
        if not won:
            consolation = self._consolation_rewards(
                adventure, "defeated", len([r for r in encounters if r.won])
            )
            earned += consolation
            rewards += consolation
            phase = "defeated"
            next_floor = floor_index
        elif floor_index + 1 >= len(adventure.floors):
            phase = "cleared"
            next_floor = floor_index + 1
        else:
            phase = "route_choice"
            next_floor = floor_index + 1
        updated = replace(
            adventure,
            phase=phase,
            floor_index=next_floor,
            selected_route_id=None,
            selected_risk_id=None,
            continuation_state=(
                simulation.attacker_final_state if won else None
            ),
            encounters=encounters,
            reward_intents=rewards,
            equipment_misses=equipment_misses,
            spellbook_misses=spellbook_misses,
            version=adventure.version + 1,
        )
        saved = self.repository.save(updated, adventure.version)
        return DungeonActionResult(saved, simulation, earned, narrative)

    @staticmethod
    def _pve_stealth_opening_ticks(
        adventure: DungeonAdventure,
        route: DungeonRouteOption,
        pve_stealth: float,
    ) -> int:
        """Resolve a small, stable PvE initiative surprise from Concealment."""

        raw = max(0.0, min(PVE_STEALTH_CAP, float(pve_stealth)))
        if raw <= 0:
            return 0
        terrain_factor = {
            "forest": 1.15,
            "cave": 1.00,
            "fortress": 0.80,
            "tower": 0.90,
        }.get(route.terrain_id, 1.0)
        rank_factor = {
            "normal": 1.00,
            "elite": 0.75,
            "boss": 0.50,
        }.get(route.monster_rank, 1.0)
        chance = min(PVE_STEALTH_CAP, raw * terrain_factor * rank_factor)
        roll = KeyedEntropy(
            DungeonAdventureFacade.RULESET_ID,
            adventure.seed,
        ).random(
            stream="encounter.pve_stealth",
            tick=adventure.floor_index,
            actor=route.option_id,
        )
        return PVE_STEALTH_OPENING_TICKS if roll < chance else 0

    def _resolve_event(
        self,
        adventure: DungeonAdventure,
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        capabilities: tuple[str, ...],
        exploration_skills: dict[str, int],
        rare_equipment_find_bonus: float,
    ) -> DungeonActionResult:
        """Resolve an exploration node without invoking the combat engine."""

        discovery_accessible = self.can_access_discovery(
            route,
            capabilities=capabilities,
            exploration_skills=exploration_skills,
        )
        access_granted = self.event_access_granted(
            route, risk, discovery_accessible=discovery_accessible
        )
        hp_cost, mp_cost, _ = self.effective_entry_cost(
            route,
            risk,
            discovery_accessible=discovery_accessible,
        )
        story_variant = self._event_story_variant(
            adventure,
            route,
            risk,
            discovery_accessible=discovery_accessible,
            access_granted=access_granted,
        )
        state = self._entry_state(adventure.continuation_state, hp_cost, mp_cost)
        state = self._event_recovery_state(
            state,
            route,
            risk,
            story_variant=story_variant,
        )
        narrative = self._event_narrative(
            route,
            risk,
            discovery_accessible=discovery_accessible,
            access_granted=access_granted,
            story_variant=story_variant,
        )
        earned = self._event_rewards(
            adventure,
            route,
            risk,
            discovery_accessible=discovery_accessible,
            access_granted=access_granted,
            exploration_skills=exploration_skills,
            rare_equipment_find_bonus=rare_equipment_find_bonus,
            story_variant=story_variant,
        )
        equipment_misses, spellbook_misses = self._loot_pity_after(
            adventure,
            route,
            earned,
            successful=True,
        )
        floor_index = adventure.floor_index
        record = DungeonEncounterRecord(
            floor_index,
            route.option_id,
            risk.risk_id,
            route.monster_template_id,
            route.monster_rank,
            route.environment.combat_environment_id,
            (),
            True,
            None,
            narrative,
        )
        if floor_index + 1 >= len(adventure.floors):
            phase = "cleared"
            next_floor = floor_index + 1
        else:
            phase = "route_choice"
            next_floor = floor_index + 1
        updated = replace(
            adventure,
            phase=phase,
            floor_index=next_floor,
            selected_route_id=None,
            selected_risk_id=None,
            continuation_state=state,
            encounters=adventure.encounters + (record,),
            reward_intents=adventure.reward_intents + earned,
            equipment_misses=equipment_misses,
            spellbook_misses=spellbook_misses,
            version=adventure.version + 1,
        )
        saved = self.repository.save(updated, adventure.version)
        return DungeonActionResult(saved, None, earned, narrative)

    @staticmethod
    def effective_monster_level(
        route: DungeonRouteOption, risk: DungeonRiskChoice
    ) -> int:
        base = (
            route.monster_level
            + risk.monster_level_delta
            + sum(item.level_delta for item in route.affixes)
        )
        return max(
            1,
            min(280, round(base * route.environment.threat_multiplier)),
        )

    @staticmethod
    def effective_entry_cost(
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        discovery_accessible: bool,
    ) -> tuple[float, float, bool]:
        factor = 1.0
        if (
            discovery_accessible
            and route.node_kind not in COMBAT_NODE_KINDS
            and risk.risk_id != "blood_contract"
        ):
            factor = 0.35
        hp_cost = max(0.0, float(risk.entry_hp_cost_ratio) * factor)
        mp_cost = max(0.0, float(risk.entry_mp_cost_ratio) * factor)
        mitigated = factor < 1.0 and (
            risk.entry_hp_cost_ratio > 0 or risk.entry_mp_cost_ratio > 0
        )
        return hp_cost, mp_cost, mitigated

    @staticmethod
    def event_access_granted(
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        discovery_accessible: bool,
    ) -> bool:
        if discovery_accessible:
            return True
        return route.node_kind in {"hidden_room", "treasure"} and risk.risk_id in {
            "force_the_passage",
            "spring_the_trap",
        }

    @classmethod
    def effective_reward_multiplier(
        cls,
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        discovery_accessible: bool,
        access_granted: bool | None = None,
        exploration_skills: dict[str, int] | None = None,
    ) -> float:
        discovery = route.discovery
        usable = discovery_accessible if access_granted is None else access_granted
        discovery_multiplier = (
            discovery.reward_multiplier if discovery and usable else 1.0
        )
        skills = exploration_skills or {}
        if usable and discovery and discovery.skill_id:
            surplus = max(
                0,
                int(skills.get(discovery.skill_id, 0)) - discovery.skill_threshold,
            )
            discovery_multiplier *= 1.0 + min(0.35, surplus * 0.01)
        multiplier = (
            route.base_reward_multiplier
            * route.environment.reward_multiplier
            * risk.reward_multiplier
            * discovery_multiplier
        )
        for affix in route.affixes:
            multiplier *= affix.reward_multiplier
        if access_granted and not discovery_accessible:
            multiplier *= 0.88
        return multiplier

    @staticmethod
    def _event_story_variant(
        adventure: DungeonAdventure,
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        discovery_accessible: bool,
        access_granted: bool,
    ) -> _EventStoryVariant:
        """Choose a small personal story without perturbing loot RNG streams."""

        cautious = risk.risk_id in {
            "rest_at_camp",
            "bury_remains",
            "gather_carefully",
            "inspect_the_seal",
            "open_carefully",
        }
        if route.node_kind == "camp" and cautious:
            variants = (
                _EventStoryVariant(
                    "warm_stew",
                    "余烬下还温着一小锅炖汤，你的脚步也轻快了一些。",
                    hp_delta=0.02,
                    stamina_delta=0.03,
                ),
                _EventStoryVariant(
                    "traveler_note",
                    "石缝里压着前人画下的路线，你因此少走了一段弯路。",
                    mana_delta=0.03,
                ),
                _EventStoryVariant(
                    "spare_supplies",
                    "熄灭的火堆旁藏着一份完好的备用物资。",
                    salvage_bonus=1,
                ),
            )
        elif route.node_kind == "camp":
            variants = (
                _EventStoryVariant(
                    "hidden_ration",
                    "床板夹层里还塞着两包没受潮的口粮。",
                    salvage_bonus=2,
                ),
                _EventStoryVariant(
                    "returning_patrol",
                    "离开时巡逻者突然折返，你带着擦伤钻进了暗处。",
                    hp_delta=-0.03,
                ),
                _EventStoryVariant(
                    "scout_map",
                    "一张潦草的守卫换岗图让你提前避开了下一轮脚步声。",
                    stamina_delta=0.03,
                    salvage_bonus=1,
                ),
            )
        elif route.node_kind == "remains" and cautious:
            variants = (
                _EventStoryVariant(
                    "quiet_blessing",
                    "最后一缕执念散去时，留下了令人安心的微光。",
                    mana_delta=0.04,
                ),
                _EventStoryVariant(
                    "weathered_token",
                    "泥土中露出一枚无主的旧徽记，你把它收作旅途纪念。",
                    salvage_bonus=1,
                ),
                _EventStoryVariant(
                    "shared_memory",
                    "一段陌生却温和的记忆掠过脑海，让疲惫稍稍消退。",
                    stamina_delta=0.04,
                ),
            )
        elif route.node_kind == "remains":
            variants = (
                _EventStoryVariant(
                    "false_bottom",
                    "破旧行囊还有一层夹底，里面的零件保存得出奇完好。",
                    salvage_bonus=2,
                ),
                _EventStoryVariant(
                    "curse_residue",
                    "遗物上的诅咒残响顺着指尖钻入血肉。",
                    hp_delta=-0.04,
                    mana_delta=-0.02,
                ),
                _EventStoryVariant(
                    "faded_prayer",
                    "褪色祷文仍残留一点力量，抵消了部分阴冷气息。",
                    mana_delta=0.03,
                    salvage_bonus=1,
                ),
            )
        elif route.node_kind == "gathering" and discovery_accessible:
            variants = (
                _EventStoryVariant(
                    "rare_cluster",
                    "脉络深处还结着一簇少见的伴生材料。",
                    salvage_bonus=2,
                ),
                _EventStoryVariant(
                    "restorative_herb",
                    "你认出几片能直接敷伤的药叶，顺手处理了旧伤。",
                    hp_delta=0.04,
                ),
                _EventStoryVariant(
                    "clear_spring",
                    "材料下渗出一股清泉，让一路积累的疲惫淡了些。",
                    stamina_delta=0.04,
                    salvage_bonus=1,
                ),
            )
        elif route.node_kind == "gathering":
            variants = (
                _EventStoryVariant(
                    "loose_fragments",
                    "碎石间还有几块容易辨认的材料，至少没有白跑。",
                    salvage_bonus=1,
                ),
                _EventStoryVariant(
                    "promising_trace",
                    "一丝异色纹理指向更深处，你把位置记在了地图上。",
                    mana_delta=0.02,
                ),
                _EventStoryVariant(
                    "edible_shoots",
                    "附近长着几株普通但能充饥的嫩芽。",
                    stamina_delta=0.03,
                ),
            )
        elif route.node_kind in {"hidden_room", "treasure"} and (
            discovery_accessible or (access_granted and cautious)
        ):
            variants = (
                _EventStoryVariant(
                    "hidden_compartment",
                    "机关背后还有一道不起眼的夹层。",
                    salvage_bonus=2,
                ),
                _EventStoryVariant(
                    "old_provisions",
                    "密封罐里的应急药剂竟然仍能使用。",
                    hp_delta=0.03,
                ),
                _EventStoryVariant(
                    "arcane_clue",
                    "墙上的残缺公式与你已知的魔法互相印证。",
                    mana_delta=0.04,
                    salvage_bonus=1,
                ),
            )
        elif route.node_kind in {"hidden_room", "treasure"} and access_granted:
            variants = (
                _EventStoryVariant(
                    "sealed_cache",
                    "被你撞松的砖石后掉出一小袋封存材料。",
                    salvage_bonus=2,
                ),
                _EventStoryVariant(
                    "ward_backlash",
                    "破裂的防护术最后闪了一次，灼伤了你的手臂。",
                    hp_delta=-0.03,
                    mana_delta=-0.02,
                ),
                _EventStoryVariant(
                    "dusty_tonic",
                    "尘封角落的一支苦涩药剂勉强还能入口。",
                    hp_delta=0.02,
                    salvage_bonus=1,
                ),
            )
        else:
            variants = (
                _EventStoryVariant(
                    "chalk_mark",
                    "门框背面留着前人的粉笔记号，印证了你的判断。",
                    mana_delta=0.02,
                ),
                _EventStoryVariant(
                    "loose_token",
                    "入口碎屑中混着一枚不起眼的旧零件。",
                    salvage_bonus=1,
                ),
                _EventStoryVariant(
                    "distant_whisper",
                    "封印后传来转瞬即逝的低语，秘密仍留待下次破解。",
                    stamina_delta=0.02,
                ),
            )
        return KeyedEntropy(
            DungeonAdventureFacade.RULESET_ID,
            adventure.seed,
        ).choice(
            variants,
            stream="event.story_variant",
            tick=adventure.floor_index,
            actor=f"{route.option_id}:{risk.risk_id}",
        )

    @staticmethod
    def _event_recovery_state(
        state: FighterContinuationState,
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        story_variant: _EventStoryVariant | None = None,
    ) -> FighterContinuationState:
        hp_gain = mp_gain = stamina_gain = 0.0
        if route.node_kind == "camp":
            if risk.risk_id == "rest_at_camp":
                hp_gain, mp_gain, stamina_gain = 0.22, 0.20, 0.30
            else:
                hp_gain, mp_gain, stamina_gain = 0.08, 0.08, 0.12
        elif route.node_kind == "remains" and risk.risk_id == "bury_remains":
            hp_gain, mp_gain, stamina_gain = 0.05, 0.10, 0.08
        elif route.node_kind == "gathering":
            stamina_gain = 0.04
        variant = story_variant or _EventStoryVariant("ordinary", "")
        return replace(
            state,
            hp_ratio=max(
                0.01,
                min(1.0, state.hp_ratio + hp_gain + variant.hp_delta),
            ),
            mana_ratio=max(
                0.0,
                min(1.0, state.mana_ratio + mp_gain + variant.mana_delta),
            ),
            stamina_ratio=max(
                0.0,
                min(
                    1.0,
                    state.stamina_ratio + stamina_gain + variant.stamina_delta,
                ),
            ),
            defeated=False,
        )

    @staticmethod
    def _event_narrative(
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        discovery_accessible: bool,
        access_granted: bool,
        story_variant: _EventStoryVariant | None = None,
    ) -> str:
        discovery_name = route.discovery.name if route.discovery else route.name
        base: str
        if route.node_kind == "camp":
            if risk.risk_id == "rest_at_camp":
                base = f"你在{discovery_name}压低火光休整，伤势、魔力与体力都得到恢复。"
            else:
                base = f"你快速翻找{discovery_name}，带走补给后在守卫回来前离开。"
        elif route.node_kind == "remains":
            if risk.risk_id == "bury_remains":
                base = f"你安葬了{discovery_name}，短暂的宁静让魔力重新流动。"
            else:
                base = f"你从{discovery_name}取走遗物，也承受了残留诅咒的擦伤。"
        elif route.node_kind == "gathering":
            if discovery_accessible:
                base = f"相关探索技能派上用场，你从{discovery_name}辨出了真正有用的材料。"
            else:
                base = f"你看不懂{discovery_name}的完整脉络，只拾取了边缘处的零散材料。"
        elif route.node_kind in {"hidden_room", "treasure"}:
            if discovery_accessible:
                base = f"已有能力破解了{discovery_name}，机关代价被大幅抵消。"
            elif access_granted:
                base = f"你付出真实代价强行打开{discovery_name}，总算没有空手而归。"
            else:
                base = f"你记下{discovery_name}的封印结构，只带走入口处能安全取得的东西。"
        else:
            base = f"你调查了{discovery_name}，平安通过这一层。"
        if story_variant and story_variant.narrative:
            return f"{base}{story_variant.narrative}"
        return base

    def _event_rewards(
        self,
        adventure: DungeonAdventure,
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        *,
        discovery_accessible: bool,
        access_granted: bool,
        exploration_skills: dict[str, int],
        rare_equipment_find_bonus: float = 0.0,
        story_variant: _EventStoryVariant | None = None,
    ) -> tuple[DungeonRewardIntent, ...]:
        definition = self.dungeon_catalog.get(adventure.dungeon_id)
        nefia = self.nefia_catalog.get(adventure.dungeon_id)
        floor = adventure.floor_index
        multiplier = self.effective_reward_multiplier(
            route,
            risk,
            discovery_accessible=discovery_accessible,
            access_granted=access_granted,
            exploration_skills=exploration_skills,
        )
        entropy = KeyedEntropy(self.RULESET_ID, adventure.seed)
        prefix = f"{adventure.settlement_key}:floor:{floor + 1}"
        variant = story_variant or _EventStoryVariant("ordinary", "")
        story_metadata = {
            "story_variant": variant.variant_id,
            "story_hp_delta": variant.hp_delta,
            "story_mana_delta": variant.mana_delta,
            "story_stamina_delta": variant.stamina_delta,
            "story_salvage_bonus": variant.salvage_bonus,
        }
        intents: list[DungeonRewardIntent] = [
            DungeonRewardIntent(
                f"{prefix}:exp",
                "experience",
                max(
                    2,
                    round(
                        (6 + adventure.player_level * 0.60 + floor * 1.5)
                        * multiplier
                    ),
                ),
                self._seed("loot", adventure.owner_key, prefix, "event-exp"),
                metadata={
                    "floor": floor + 1,
                    "node_kind": route.node_kind,
                    **story_metadata,
                },
            )
        ]
        salvage_base = {
            "camp": 1 if risk.risk_id == "rest_at_camp" else 3,
            "remains": 1 if risk.risk_id == "bury_remains" else 3,
            "gathering": 4 if access_granted else 1,
            "hidden_room": 3 if access_granted else 1,
            "treasure": 2 if access_granted else 1,
        }.get(route.node_kind, 1)
        intents.append(
            DungeonRewardIntent(
                f"{prefix}:salvage",
                "salvage",
                max(1, round(salvage_base * multiplier) + variant.salvage_bonus),
                self._seed("loot", adventure.owner_key, prefix, "event-salvage"),
                metadata={
                    "floor": floor + 1,
                    "node_kind": route.node_kind,
                    "ability_solution": discovery_accessible,
                    **story_metadata,
                },
            )
        )
        equipment_roll = entropy.random(
            stream="event.equipment", tick=floor, actor=route.option_id
        )
        equipment_chance = {
            "remains": 0.42 if risk.risk_id == "loot_remains" else 0.12,
            "gathering": 0.18 if access_granted else 0.04,
            "hidden_room": 1.0 if access_granted else 0.08,
            "treasure": 0.68 if access_granted else 0.12,
            "camp": 0.12 if risk.risk_id == "search_camp" else 0.03,
        }.get(route.node_kind, 0.05)
        find_chance_bonus, find_quality_bonus = self._rare_equipment_find_bonuses(
            rare_equipment_find_bonus
        )
        equipment_chance = min(1.0, equipment_chance + find_chance_bonus)
        equipment_pity = adventure.equipment_misses >= 2
        if equipment_pity or equipment_roll < equipment_chance:
            spec = definition.clear_rewards
            intents.append(
                DungeonRewardIntent(
                    f"{prefix}:equipment",
                    "equipment",
                    1,
                    self._seed("loot", adventure.owner_key, prefix, "event-equipment"),
                    max(1, route.monster_level - 4),
                    min(100, route.monster_level + 1),
                    spec.catalog_id_min,
                    spec.catalog_id_max,
                    quality_bonus=(
                        max(0.0, multiplier - 1.0) + find_quality_bonus
                    ),
                    metadata={
                        "node_kind": route.node_kind,
                        "pity_guaranteed": equipment_pity,
                        "rare_find_bonus": find_quality_bonus,
                        **story_metadata,
                    },
                )
            )
        book_roll = entropy.random(
            stream="event.spellbook", tick=floor, actor=route.option_id
        )
        book_chance = {
            "hidden_room": 0.52 if access_granted else 0.04,
            "treasure": 0.28 if access_granted else 0.03,
            "remains": 0.14 if risk.risk_id == "bury_remains" else 0.08,
            "camp": 0.07,
            "gathering": 0.06,
        }.get(route.node_kind, 0.03)
        spellbook_pity = adventure.spellbook_misses >= 3
        if spellbook_pity or book_roll < book_chance:
            intents.append(
                DungeonRewardIntent(
                    f"{prefix}:spellbook",
                    "spellbook",
                    1,
                    self._seed("loot", adventure.owner_key, prefix, "event-spellbook"),
                    spell_pool=nefia.spellbook_pool,
                    metadata={
                        "node_kind": route.node_kind,
                        "pity_guaranteed": spellbook_pity,
                        **story_metadata,
                    },
                )
            )
        return tuple(intents)

    def retreat(self, adventure_id: str) -> DungeonActionResult:
        adventure = self.get(adventure_id)
        if adventure.terminal:
            raise ValueError("本次奈菲亚已经结束")
        consolation = self._consolation_rewards(
            adventure, "retreated", adventure.completed_floors
        )
        updated = replace(
            adventure,
            phase="retreated",
            selected_route_id=None,
            selected_risk_id=None,
            continuation_state=None,
            reward_intents=adventure.reward_intents + consolation,
            version=adventure.version + 1,
        )
        saved = self.repository.save(updated, adventure.version)
        return DungeonActionResult(saved, None, consolation)

    def _generate_floors(
        self,
        definition: DungeonDefinition,
        nefia: NefiaDefinition,
        map_seed: int,
        player_level: int,
        difficulty: int,
    ) -> tuple[DungeonFloor, ...]:
        entropy = KeyedEntropy(self.RULESET_ID, map_seed)
        count = entropy.randint(
            nefia.node_count_min,
            nefia.node_count_max,
            stream="map.node_count",
        )
        floors: list[DungeonFloor] = []
        for floor_index in range(count):
            routes = tuple(
                self._route(
                    definition,
                    nefia,
                    entropy,
                    floor_index,
                    side,
                    count,
                    player_level,
                    difficulty,
                )
                for side in range(2)
            )
            floors.append(DungeonFloor(floor_index, routes))
        return tuple(floors)

    def _route(
        self,
        definition: DungeonDefinition,
        nefia: NefiaDefinition,
        entropy: KeyedEntropy,
        floor_index: int,
        side: int,
        floor_count: int,
        player_level: int,
        difficulty: int,
    ) -> DungeonRouteOption:
        is_boss = floor_index == floor_count - 1
        terrain_id = entropy.choice(
            nefia.terrain_pool,
            stream="map.terrain",
            tick=floor_index,
            subindex=side,
        )
        event_side = entropy.randint(
            0, 1, stream="map.event_side", tick=floor_index
        )
        is_event = not is_boss and side == event_side
        elite_roll = entropy.random(
            stream="map.elite", tick=floor_index, subindex=side
        )
        is_elite = not is_boss and not is_event and (
            floor_index >= 1 and (elite_roll < 0.36 or floor_index == floor_count - 2)
        )
        rank = "boss" if is_boss else "elite" if is_elite else "normal"
        kind = rank
        pool = nefia.monster_pool_for(terrain_id, rank)
        monster_stream = (
            "map.monster.boss"
            if is_boss
            else f"map.monster.{terrain_id}.{rank}"
        )
        template_id = entropy.choice(
            pool, stream=monster_stream, tick=floor_index, subindex=side
        )
        environment_id = entropy.choice(
            nefia.environment_pool,
            stream="map.environment",
            tick=floor_index,
            subindex=side,
        )
        environment = self.nefia_catalog.snapshot.environments[environment_id]
        risk_pair = entropy.choice(
            self.nefia_catalog.snapshot.risk_pairs,
            stream="map.risk_pair",
            tick=floor_index,
            subindex=side,
        )
        risks = tuple(
            self.nefia_catalog.snapshot.risks[risk_id]
            for risk_id in risk_pair
        )
        affix_count = 2 if is_boss else 1 if is_elite else 0
        affix_values = tuple(self.nefia_catalog.snapshot.affixes.values())
        first = entropy.choice(
            affix_values, stream="map.affix", tick=floor_index, subindex=side * 2
        )
        second_pool = tuple(item for item in affix_values if item != first)
        affixes = ()
        if affix_count:
            affixes = (first,)
        if affix_count == 2:
            affixes += (
                entropy.choice(
                    second_pool,
                    stream="map.affix",
                    tick=floor_index,
                    subindex=side * 2 + 1,
                ),
            )
        level_delta = floor_index + (difficulty - 1) * 2
        target_level = max(1, min(280, player_level + level_delta))
        discovery = self._discovery(entropy, floor_index, side, terrain_id, is_boss)
        if is_event:
            kind = EVENT_KIND_BY_DISCOVERY[discovery.discovery_type]
        contextual_pair = {
            "camp": ("rest_at_camp", "search_camp"),
            "remains": ("bury_remains", "loot_remains"),
            "gathering_point": ("gather_carefully", "strip_the_vein"),
            "material_cache": ("gather_carefully", "strip_the_vein"),
            "gem_cache": ("gather_carefully", "strip_the_vein"),
            "hidden_room": ("inspect_the_seal", "force_the_passage"),
            "ordinary_chest": ("open_carefully", "spring_the_trap"),
            "mystery_chest": ("open_carefully", "spring_the_trap"),
        }.get(discovery.discovery_type) if is_event else None
        if contextual_pair is not None:
            risks = tuple(
                self.nefia_catalog.snapshot.risks[risk_id]
                for risk_id in contextual_pair
            )
        route_names = (
            "苔痕岔路", "坍塌近道", "守卫回廊", "失落阶梯",
            "兽迹小径", "封印长桥", "裂隙甬道", "古井暗门",
        )
        name = route_names[
            entropy.randint(
                0,
                len(route_names) - 1,
                stream="map.route_name",
                tick=floor_index,
                subindex=side,
            )
        ]
        if is_boss:
            name = "奈菲亚之主的门扉" if side == 0 else "封印王座的侧门"
        elif is_event:
            name = discovery.name
        return DungeonRouteOption(
            option_id=f"f{floor_index + 1}{'a' if side == 0 else 'b'}",
            name=name,
            description=(
                f"{environment.name}中有{discovery.name}，选择会改变资源与收获。"
                if is_event
                else f"通往{environment.name}，胜利后可探索{discovery.name}。"
            ),
            node_kind=kind,
            monster_template_id=template_id,
            monster_level=target_level,
            monster_rank=rank,
            environment=environment,
            affixes=affixes,
            risk_choices=risks,  # type: ignore[arg-type]
            terrain_id=terrain_id,
            terrain_name=TERRAIN_NAMES[terrain_id],
            discovery=discovery,
            base_reward_multiplier=(
                1.65 if is_boss else 1.28 if is_elite else 0.88 if is_event else 1.0
            ) * (1 + 0.12 * (difficulty - 1)),
        )

    @staticmethod
    def _discovery(
        entropy: KeyedEntropy,
        floor_index: int,
        side: int,
        terrain_id: str,
        is_boss: bool,
    ) -> DungeonDiscovery:
        if is_boss:
            return DungeonDiscovery(
                f"f{floor_index + 1}-boss-hoard-{side}",
                "mystery_chest",
                "首领秘藏",
                "击败奈菲亚之主后必定开启的终点宝藏。",
                1.45,
            )
        variants = {
            "forest": (
                ("gathering_point", "药草采集点", "natural_knowledge", 12, (), 1.16),
                ("material_cache", "林地材料堆", None, 0, (), 1.08),
                ("remains", "无名遗骸", "concealment", 10, (), 1.14),
            ),
            "cave": (
                ("gem_cache", "矿脉宝箱", "weightlifting", 12, (), 1.18),
                ("hidden_room", "岩壁后的隐藏房", "weightlifting", 20, ("teleport", "blink", "fire_wall", "fire_ray"), 1.30),
                ("ordinary_chest", "旧木宝箱", None, 0, (), 1.05),
            ),
            "fortress": (
                ("camp", "废弃营地", "concealment", 10, (), 1.14),
                ("remains", "守卫遗骸", None, 0, (), 1.12),
                ("material_cache", "军需材料箱", None, 0, (), 1.09),
            ),
            "tower": (
                ("mystery_chest", "魔法机关箱", "reading", 15, (), 1.24),
                ("hidden_room", "折叠空间密室", "reading", 22, ("teleport", "blink"), 1.34),
                ("ordinary_chest", "学徒储物箱", None, 0, (), 1.06),
            ),
        }
        choice = entropy.choice(
            variants[terrain_id],
            stream="map.discovery",
            tick=floor_index,
            subindex=side,
        )
        kind, name, skill_id, threshold, unlock_any, multiplier = choice
        return DungeonDiscovery(
            f"f{floor_index + 1}-{kind}-{side}",
            kind,  # type: ignore[arg-type]
            name,
            "对应技能或魔法可能揭示额外收益。",
            multiplier,
            skill_id,
            threshold,
            unlock_any,
        )

    def _encounter_rewards(
        self,
        adventure: DungeonAdventure,
        route: DungeonRouteOption,
        risk: DungeonRiskChoice,
        won: bool,
        *,
        capabilities: tuple[str, ...],
        exploration_skills: dict[str, int],
        rare_equipment_find_bonus: float = 0.0,
    ) -> tuple[DungeonRewardIntent, ...]:
        if not won:
            return ()
        definition = self.dungeon_catalog.get(adventure.dungeon_id)
        nefia = self.nefia_catalog.get(adventure.dungeon_id)
        floor = adventure.floor_index
        unlocked = self.can_access_discovery(
            route,
            capabilities=capabilities,
            exploration_skills=exploration_skills,
        )
        multiplier = self.effective_reward_multiplier(
            route,
            risk,
            discovery_accessible=unlocked,
            exploration_skills=exploration_skills,
        )
        entropy = KeyedEntropy(self.RULESET_ID, adventure.seed)
        prefix = f"{adventure.settlement_key}:floor:{floor + 1}"
        intents: list[DungeonRewardIntent] = [
            DungeonRewardIntent(
                f"{prefix}:exp",
                "experience",
                max(2, round((10 + route.monster_level * 1.8) * multiplier)),
                self._seed("loot", adventure.owner_key, prefix, "exp"),
                metadata={"floor": floor + 1, "node_kind": route.node_kind},
            )
        ]
        is_boss = route.node_kind == "boss"
        is_elite = route.node_kind == "elite"
        equipment_roll = entropy.random(
            stream="loot.equipment", tick=floor, actor=route.option_id
        )
        find_chance_bonus, find_quality_bonus = self._rare_equipment_find_bonuses(
            rare_equipment_find_bonus
        )
        equipment_pity = not is_boss and adventure.equipment_misses >= 2
        equipment_chance = (
            min(0.72, 0.48 + 0.10 * multiplier)
            if is_elite
            else min(0.63, 0.16 * multiplier + find_chance_bonus)
        )
        if (
            is_boss
            or equipment_pity
            or equipment_roll < equipment_chance
        ):
            spec = definition.clear_rewards
            intents.append(
                DungeonRewardIntent(
                    f"{prefix}:equipment",
                    "equipment",
                    1,
                    self._seed("loot", adventure.owner_key, prefix, "equipment"),
                    max(1, route.monster_level - 3),
                    min(100, route.monster_level + 2),
                    spec.catalog_id_min,
                    spec.catalog_id_max,
                    quality_bonus=(
                        max(0.0, multiplier - 1.0) + find_quality_bonus
                    ),
                    metadata={
                        "guaranteed_boss_reward": is_boss,
                        "elite_drop_chance": (
                            round(equipment_chance, 4) if is_elite else None
                        ),
                        "pity_guaranteed": equipment_pity,
                        "rare_find_bonus": find_quality_bonus,
                    },
                )
            )
        book_roll = entropy.random(
            stream="loot.spellbook", tick=floor, actor=route.option_id
        )
        book_affix = any(item.affix_id == "spell_hoarder" for item in route.affixes)
        discovery_book = bool(
            unlocked
            and route.discovery
            and route.discovery.discovery_type in {"mystery_chest", "hidden_room"}
        )
        spellbook_pity = not is_boss and adventure.spellbook_misses >= 3
        if (
            is_boss
            or book_affix
            or discovery_book
            or spellbook_pity
            or book_roll < min(0.42, 0.09 * multiplier)
        ):
            intents.append(
                DungeonRewardIntent(
                    f"{prefix}:spellbook",
                    "spellbook",
                    1,
                    self._seed("loot", adventure.owner_key, prefix, "spellbook"),
                    spell_pool=nefia.spellbook_pool,
                    metadata={
                        "guaranteed_boss_reward": is_boss,
                        "pity_guaranteed": spellbook_pity,
                    },
                )
            )
        if unlocked and route.discovery and route.discovery.discovery_type in {
            "material_cache", "gem_cache", "gathering_point", "camp", "remains"
        }:
            intents.append(
                DungeonRewardIntent(
                    f"{prefix}:salvage",
                    "salvage",
                    max(1, round(3 * multiplier)),
                    self._seed("loot", adventure.owner_key, prefix, "salvage"),
                    metadata={"discovery": route.discovery.discovery_type},
                )
            )
        return tuple(intents)

    @staticmethod
    def _rare_equipment_find_bonuses(
        rare_equipment_find_bonus: float,
    ) -> tuple[float, float]:
        """Convert item discovery rate into bounded Nefia-only loot boosts."""

        raw = max(0.0, min(0.30, float(rare_equipment_find_bonus)))
        return (
            min(RARE_EQUIPMENT_CHANCE_BONUS_CAP, raw * 0.25),
            min(RARE_EQUIPMENT_QUALITY_BONUS_CAP, raw * 0.40),
        )

    @staticmethod
    def _loot_pity_after(
        adventure: DungeonAdventure,
        route: DungeonRouteOption,
        earned: tuple[DungeonRewardIntent, ...],
        *,
        successful: bool,
    ) -> tuple[int, int]:
        if not successful:
            return adventure.equipment_misses, adventure.spellbook_misses
        if route.node_kind == "boss":
            return 0, 0
        reward_types = {intent.reward_type for intent in earned}
        equipment_misses = (
            0
            if "equipment" in reward_types
            else adventure.equipment_misses + 1
        )
        spellbook_misses = (
            0
            if "spellbook" in reward_types
            else adventure.spellbook_misses + 1
        )
        return equipment_misses, spellbook_misses

    def _consolation_rewards(
        self, adventure: DungeonAdventure, reason: str, completed: int
    ) -> tuple[DungeonRewardIntent, ...]:
        prefix = f"{adventure.settlement_key}:terminal:{reason}"
        return (
            DungeonRewardIntent(
                f"{prefix}:exp",
                "experience",
                max(2, 2 + completed * max(2, adventure.player_level // 4)),
                self._seed("loot", adventure.owner_key, prefix, "exp"),
                metadata={"consolation": True, "reason": reason},
            ),
            DungeonRewardIntent(
                f"{prefix}:salvage",
                "salvage",
                max(1, completed + 1),
                self._seed("loot", adventure.owner_key, prefix, "salvage"),
                metadata={"consolation": True, "reason": reason},
            ),
        )

    @staticmethod
    def _affixed_snapshot(
        snapshot: FighterSnapshot,
        affixes: tuple[DungeonAffix, ...],
    ) -> FighterSnapshot:
        derived = snapshot.derived
        if derived is None or not affixes:
            return snapshot
        affix_ids = {item.affix_id for item in affixes}
        defense = float(derived.defense)
        physical_reduction = float(derived.physical_reduction)
        magical_reduction = float(derived.magical_reduction)
        action_speed = float(derived.action_speed)
        evasion = float(derived.evasion)
        attack_power = float(derived.attack_power)
        if "ironclad" in affix_ids:
            defense *= 1.18
            physical_reduction = min(0.55, physical_reduction + 0.08)
            magical_reduction = min(0.55, magical_reduction + 0.08)
        if "swift" in affix_ids:
            action_speed = min(180.0, action_speed * 1.18)
            evasion *= 1.08
        if "ferocious" in affix_ids:
            attack_power *= 1.10
        return replace(
            snapshot,
            derived=replace(
                derived,
                defense=defense,
                physical_reduction=physical_reduction,
                magical_reduction=magical_reduction,
                action_speed=action_speed,
                evasion=evasion,
                attack_power=attack_power,
            ),
        )

    @staticmethod
    def _affixed_profile(
        profile: AIProfile, affixes: tuple[DungeonAffix, ...]
    ) -> AIProfile:
        aggression = max(
            0.05,
            min(0.98, profile.aggression + sum(a.aggression_delta for a in affixes)),
        )
        guard = max(
            0.0,
            min(0.90, profile.guard_tendency + sum(a.guard_delta for a in affixes)),
        )
        return replace(profile, aggression=aggression, guard_tendency=guard)

    @staticmethod
    def _entry_state(
        continuation: FighterContinuationState | None,
        hp_cost_ratio: float,
        mp_cost_ratio: float,
    ) -> FighterContinuationState:
        state = continuation or FighterContinuationState()
        return replace(
            state,
            hp_ratio=max(0.10, state.hp_ratio - hp_cost_ratio),
            mana_ratio=max(0.0, state.mana_ratio - mp_cost_ratio),
            defeated=False,
        )

    @staticmethod
    def _selected_route(adventure: DungeonAdventure) -> DungeonRouteOption:
        floor = adventure.current_floor
        if floor is None or not adventure.selected_route_id:
            raise RuntimeError("当前没有已选择路线")
        route = next(
            (r for r in floor.routes if r.option_id == adventure.selected_route_id),
            None,
        )
        if route is None:
            raise RuntimeError("奈菲亚路线状态损坏")
        return route

    @staticmethod
    def _selected_risk(
        adventure: DungeonAdventure, route: DungeonRouteOption
    ) -> DungeonRiskChoice:
        risk = next(
            (r for r in route.risk_choices if r.risk_id == adventure.selected_risk_id),
            None,
        )
        if risk is None:
            raise RuntimeError("当前没有已选择风险")
        return risk

    @staticmethod
    def _require_phase(adventure: DungeonAdventure, phase: str) -> None:
        if adventure.phase != phase:
            raise ValueError(f"当前奈菲亚状态为 {adventure.phase}，不能执行该操作")

    @staticmethod
    def _seed(*parts: object) -> int:
        payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
        return int.from_bytes(
            hashlib.blake2b(payload, digest_size=8, person=b"nefia-v1").digest(),
            "big",
        ) & ((1 << 63) - 1)

    @staticmethod
    def _token(*parts: object) -> str:
        payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
        return hashlib.blake2b(
            payload, digest_size=10, person=b"nefia-id-v1"
        ).hexdigest()
