"""Validated data catalog for random Nefia generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    from ..models.dungeon import DungeonAffix, DungeonEnvironment, DungeonRiskChoice
    from .monster_catalog import MonsterCatalog
except ImportError:
    from models.dungeon import DungeonAffix, DungeonEnvironment, DungeonRiskChoice
    from services.monster_catalog import MonsterCatalog


DEFAULT_NEFIA_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "dungeon_nefia_catalog.json"
)
_COMBAT_ENVIRONMENTS = {
    "calm", "rain", "fog", "strong_wind", "close_quarters",
    "mana_tide", "ether_disturbance",
}
_TERRAINS = {"forest", "cave", "fortress", "tower"}


@dataclass(frozen=True)
class NefiaDefinition:
    dungeon_id: str
    node_count_min: int
    node_count_max: int
    monster_pool: tuple[str, ...]
    elite_pool: tuple[str, ...]
    boss_pool: tuple[str, ...]
    environment_pool: tuple[str, ...]
    terrain_pool: tuple[str, ...]
    spellbook_pool: tuple[str, ...]
    terrain_monster_pools: dict[str, dict[str, tuple[str, ...]]] = field(
        default_factory=dict
    )

    def monster_pool_for(self, terrain_id: str, rank: str) -> tuple[str, ...]:
        if rank == "boss":
            return self.boss_pool
        if rank not in {"normal", "elite"}:
            raise ValueError(f"未知怪物阶级：{rank}")
        fallback = self.elite_pool if rank == "elite" else self.monster_pool
        return self.terrain_monster_pools.get(terrain_id, {}).get(rank, fallback)


@dataclass(frozen=True)
class NefiaCatalogSnapshot:
    schema_version: int
    environments: dict[str, DungeonEnvironment]
    risks: dict[str, DungeonRiskChoice]
    risk_pairs: tuple[tuple[str, str], ...]
    affixes: dict[str, DungeonAffix]
    dungeons: dict[str, NefiaDefinition]


class DungeonNefiaCatalog:
    def __init__(
        self,
        path: str | Path | None = None,
        monster_catalog: MonsterCatalog | None = None,
    ) -> None:
        self.path = Path(path or DEFAULT_NEFIA_CATALOG_PATH)
        self.monster_catalog = monster_catalog or MonsterCatalog()
        self._snapshot = self._load()

    @property
    def snapshot(self) -> NefiaCatalogSnapshot:
        return self._snapshot

    def get(self, dungeon_id: str) -> NefiaDefinition:
        try:
            return self._snapshot.dungeons[dungeon_id]
        except KeyError as exc:
            raise KeyError(f"副本 {dungeon_id} 尚未配置随机奈菲亚") from exc

    @staticmethod
    def _unique_id(raw: object, label: str, seen: set[str]) -> str:
        value = str(raw or "").strip()
        if not value or value in seen:
            raise ValueError(f"{label} ID为空或重复：{value}")
        seen.add(value)
        return value

    @staticmethod
    def _tuple(raw: object, label: str) -> tuple[str, ...]:
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"{label}必须是非空数组")
        values = tuple(str(value).strip() for value in raw)
        if any(not value for value in values):
            raise ValueError(f"{label}不能包含空值")
        return values

    def _load(self) -> NefiaCatalogSnapshot:
        with self.path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        if raw.get("schema_version") != 1:
            raise ValueError("随机奈菲亚目录仅支持schema_version 1")

        environments: dict[str, DungeonEnvironment] = {}
        seen: set[str] = set()
        for index, item in enumerate(raw.get("environments", [])):
            env_id = self._unique_id(item.get("id"), f"environments[{index}]", seen)
            combat_id = str(item.get("combat_environment_id", ""))
            if combat_id not in _COMBAT_ENVIRONMENTS:
                raise ValueError(f"{env_id}引用未知战斗环境：{combat_id}")
            environments[env_id] = DungeonEnvironment(
                env_id,
                str(item.get("name", env_id)),
                str(item.get("description", "")),
                combat_id,
                float(item.get("threat_multiplier", 1.0)),
                float(item.get("reward_multiplier", 1.0)),
            )
        if not environments:
            raise ValueError("随机奈菲亚至少需要一个环境")

        risks: dict[str, DungeonRiskChoice] = {}
        seen = set()
        for index, item in enumerate(raw.get("risks", [])):
            risk_id = self._unique_id(item.get("id"), f"risks[{index}]", seen)
            risks[risk_id] = DungeonRiskChoice(
                risk_id,
                str(item.get("name", risk_id)),
                str(item.get("description", "")),
                int(item.get("monster_level_delta", 0)),
                float(item.get("reward_multiplier", 1.0)),
                float(item.get("entry_hp_cost_ratio", 0.0)),
                float(item.get("entry_mp_cost_ratio", 0.0)),
            )
        risk_pairs: list[tuple[str, str]] = []
        for index, pair in enumerate(raw.get("risk_pairs", [])):
            if not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]:
                raise ValueError(f"risk_pairs[{index}]必须包含两个不同选项")
            if any(value not in risks for value in pair):
                raise ValueError(f"risk_pairs[{index}]引用未知风险")
            risk_pairs.append((str(pair[0]), str(pair[1])))
        if not risk_pairs:
            raise ValueError("至少需要一组风险二选一")

        affixes: dict[str, DungeonAffix] = {}
        seen = set()
        for index, item in enumerate(raw.get("affixes", [])):
            affix_id = self._unique_id(item.get("id"), f"affixes[{index}]", seen)
            affixes[affix_id] = DungeonAffix(
                affix_id,
                str(item.get("name", affix_id)),
                str(item.get("description", "")),
                int(item.get("level_delta", 0)),
                float(item.get("aggression_delta", 0.0)),
                float(item.get("guard_delta", 0.0)),
                float(item.get("reward_multiplier", 1.0)),
            )
        if not affixes:
            raise ValueError("至少需要一个精英词缀")

        dungeons: dict[str, NefiaDefinition] = {}
        for dungeon_id, item in raw.get("dungeons", {}).items():
            minimum = int(item.get("node_count_min", 3))
            maximum = int(item.get("node_count_max", 5))
            if not 3 <= minimum <= maximum <= 5:
                raise ValueError(f"{dungeon_id}节点数必须在3到5之间")
            pools = {
                key: self._tuple(item.get(key), f"{dungeon_id}.{key}")
                for key in (
                    "monster_pool", "elite_pool", "boss_pool",
                    "environment_pool", "terrain_pool", "spellbook_pool",
                )
            }
            for pool_name in ("monster_pool", "elite_pool", "boss_pool"):
                for template_id in pools[pool_name]:
                    try:
                        self.monster_catalog.get(template_id)
                    except KeyError as exc:
                        raise ValueError(
                            f"{dungeon_id}.{pool_name}引用未知怪物：{template_id}"
                        ) from exc
            if any(value not in environments for value in pools["environment_pool"]):
                raise ValueError(f"{dungeon_id}.environment_pool引用未知环境")
            if any(value not in _TERRAINS for value in pools["terrain_pool"]):
                raise ValueError(f"{dungeon_id}.terrain_pool引用未知地形")
            terrain_monster_pools = self._terrain_monster_pools(
                dungeon_id,
                item.get("terrain_monster_pools", {}),
                pools["terrain_pool"],
            )
            dungeons[dungeon_id] = NefiaDefinition(
                dungeon_id,
                minimum,
                maximum,
                pools["monster_pool"],
                pools["elite_pool"],
                pools["boss_pool"],
                pools["environment_pool"],
                pools["terrain_pool"],
                pools["spellbook_pool"],
                terrain_monster_pools,
            )
        if not dungeons:
            raise ValueError("随机奈菲亚目录没有副本")
        return NefiaCatalogSnapshot(
            1, environments, risks, tuple(risk_pairs), affixes, dungeons
        )

    def _terrain_monster_pools(
        self,
        dungeon_id: str,
        raw: object,
        enabled_terrains: tuple[str, ...],
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        label = f"{dungeon_id}.terrain_monster_pools"
        if not isinstance(raw, dict):
            raise ValueError(f"{label}必须是对象")
        result: dict[str, dict[str, tuple[str, ...]]] = {}
        for raw_terrain_id, raw_rank_pools in raw.items():
            terrain_id = str(raw_terrain_id).strip()
            if terrain_id not in _TERRAINS:
                raise ValueError(f"{label}引用未知地形：{terrain_id}")
            if terrain_id not in enabled_terrains:
                raise ValueError(
                    f"{label}.{terrain_id}未在terrain_pool中启用"
                )
            if not isinstance(raw_rank_pools, dict) or not raw_rank_pools:
                raise ValueError(f"{label}.{terrain_id}必须是非空对象")
            unknown_ranks = set(raw_rank_pools) - {"normal", "elite"}
            if unknown_ranks:
                rank = sorted(str(value) for value in unknown_ranks)[0]
                raise ValueError(f"{label}.{terrain_id}引用未知阶级：{rank}")
            rank_pools: dict[str, tuple[str, ...]] = {}
            for rank, raw_pool in raw_rank_pools.items():
                pool_label = f"{label}.{terrain_id}.{rank}"
                pool = self._tuple(raw_pool, pool_label)
                for template_id in pool:
                    try:
                        self.monster_catalog.get(template_id)
                    except KeyError as exc:
                        raise ValueError(
                            f"{pool_label}引用未知怪物：{template_id}"
                        ) from exc
                rank_pools[str(rank)] = pool
            result[terrain_id] = rank_pools
        return result
