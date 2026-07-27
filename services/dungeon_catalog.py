import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .monster_catalog import MonsterCatalog
except ImportError:
    from services.monster_catalog import MonsterCatalog


DEFAULT_DUNGEON_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "dungeon_catalog.json"
)
_DUNGEON_ID_PATTERN = __import__("re").compile(r"^[a-z][a-z0-9_]{2,63}$")
_VALID_RANKS = {"normal", "elite", "boss"}


@dataclass(frozen=True)
class DungeonWave:
    template_id: str
    level: int
    rank: str = "normal"


@dataclass(frozen=True)
class DungeonRewardSpec:
    equipment_count: int
    equipment_level_min: int
    equipment_level_max: int
    catalog_id_min: int
    catalog_id_max: int
    chance: float = 0.0


@dataclass(frozen=True)
class DungeonDefinition:
    dungeon_id: str
    name: str
    description: str
    recommended_level: int
    exp_discount_rate: float
    waves: tuple[DungeonWave, ...]
    clear_rewards: DungeonRewardSpec
    partial_kill_rewards: DungeonRewardSpec


@dataclass(frozen=True)
class DungeonCatalogSnapshot:
    schema_version: int
    dungeons: tuple[DungeonDefinition, ...]
    by_id: dict[str, DungeonDefinition]
    by_name: dict[str, DungeonDefinition]


class DungeonCatalog:
    """Validated, atomically reloadable dungeon data."""

    def __init__(
        self,
        path: str | Path | None = None,
        monster_catalog: MonsterCatalog | None = None,
    ):
        self.path = Path(path or DEFAULT_DUNGEON_CATALOG_PATH)
        self._monster_catalog = monster_catalog or MonsterCatalog()
        self._snapshot = self._load(self.path)

    @property
    def snapshot(self) -> DungeonCatalogSnapshot:
        return self._snapshot

    def get(self, dungeon_id: str) -> DungeonDefinition:
        try:
            return self._snapshot.by_id[dungeon_id]
        except KeyError as exc:
            raise KeyError(f"未知副本：{dungeon_id}") from exc

    def get_by_name(self, name: str) -> DungeonDefinition | None:
        return self._snapshot.by_name.get((name or "").strip())

    def list(self) -> tuple[DungeonDefinition, ...]:
        return self._snapshot.dungeons

    def reload(
        self,
        path: str | Path | None = None,
        monster_catalog: MonsterCatalog | None = None,
    ) -> DungeonCatalogSnapshot:
        candidate_path = Path(path or self.path)
        if monster_catalog is not None:
            self._monster_catalog = monster_catalog
        candidate = self._load(candidate_path, self._monster_catalog)
        self.path = candidate_path
        self._snapshot = candidate
        return candidate

    def _load(
        self,
        path: Path,
        monster_catalog: MonsterCatalog | None = None,
    ) -> DungeonCatalogSnapshot:
        catalog = monster_catalog or self._monster_catalog
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != 1:
            raise ValueError("副本目录仅支持schema_version 1")
        raw_dungeons = data.get("dungeons")
        if not isinstance(raw_dungeons, list) or not raw_dungeons:
            raise ValueError("dungeons必须是非空数组")
        dungeons: list[DungeonDefinition] = []
        by_id: dict[str, DungeonDefinition] = {}
        by_name: dict[str, DungeonDefinition] = {}
        for index, raw in enumerate(raw_dungeons):
            dungeon = self._dungeon(raw, index, catalog)
            if dungeon.dungeon_id in by_id:
                raise ValueError(f"重复副本ID：{dungeon.dungeon_id}")
            if dungeon.name in by_name:
                raise ValueError(f"重复副本名：{dungeon.name}")
            by_id[dungeon.dungeon_id] = dungeon
            by_name[dungeon.name] = dungeon
            dungeons.append(dungeon)
        return DungeonCatalogSnapshot(
            schema_version=1,
            dungeons=tuple(dungeons),
            by_id=by_id,
            by_name=by_name,
        )

    def _dungeon(self, raw, index: int, catalog: MonsterCatalog) -> DungeonDefinition:
        raw = raw if isinstance(raw, dict) else {}
        dungeon_id = str(raw.get("dungeon_id", ""))
        if not _DUNGEON_ID_PATTERN.match(dungeon_id):
            raise ValueError(f"dungeons[{index}]非法副本ID：{dungeon_id}")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError(f"dungeons[{index}]缺少副本名")
        description = str(raw.get("description", "")).strip()
        recommended_level = int(raw.get("recommended_level", 0))
        if not 1 <= recommended_level <= 280:
            raise ValueError(f"{dungeon_id}推荐等级必须在1到280之间")
        exp_discount_rate = float(raw.get("exp_discount_rate", 0.05))
        if not 0 < exp_discount_rate <= 1:
            raise ValueError(f"{dungeon_id}经验折扣率必须在0到1之间")
        waves = self._waves(raw.get("waves"), dungeon_id, catalog)
        clear_rewards = self._reward(
            raw.get("clear_rewards"), f"{dungeon_id}.clear_rewards"
        )
        partial_kill_rewards = self._reward(
            raw.get("partial_kill_rewards"), f"{dungeon_id}.partial_kill_rewards"
        )
        return DungeonDefinition(
            dungeon_id=dungeon_id,
            name=name,
            description=description,
            recommended_level=recommended_level,
            exp_discount_rate=exp_discount_rate,
            waves=waves,
            clear_rewards=clear_rewards,
            partial_kill_rewards=partial_kill_rewards,
        )

    def _waves(self, raw, dungeon_id: str, catalog: MonsterCatalog) -> tuple[DungeonWave, ...]:
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"{dungeon_id}波次必须是非空数组")
        waves: list[DungeonWave] = []
        for index, item in enumerate(raw):
            item = item if isinstance(item, dict) else {}
            template_id = str(item.get("template_id", ""))
            try:
                catalog.get(template_id)
            except KeyError:
                raise ValueError(
                    f"{dungeon_id}.waves[{index}]引用未知怪物模板：{template_id}"
                )
            level = int(item.get("level", 0))
            if not 1 <= level <= 280:
                raise ValueError(
                    f"{dungeon_id}.waves[{index}]等级必须在1到280之间"
                )
            rank = str(item.get("rank", "normal"))
            if rank not in _VALID_RANKS:
                raise ValueError(
                    f"{dungeon_id}.waves[{index}]阶级无效：{rank}"
                )
            waves.append(DungeonWave(template_id, level, rank))
        return tuple(waves)

    def _reward(self, raw, label: str) -> DungeonRewardSpec:
        raw = raw if isinstance(raw, dict) else {}
        equipment_count = int(raw.get("equipment_count", 0))
        if not 1 <= equipment_count <= 10:
            raise ValueError(f"{label}.equipment_count必须在1到10之间")
        equipment_level_min = int(raw.get("equipment_level_min", 0))
        equipment_level_max = int(raw.get("equipment_level_max", 0))
        if not 0 <= equipment_level_min <= 100:
            raise ValueError(f"{label}.equipment_level_min必须在0到100之间")
        if not 0 <= equipment_level_max <= 100:
            raise ValueError(f"{label}.equipment_level_max必须在0到100之间")
        if equipment_level_min > equipment_level_max:
            raise ValueError(f"{label}等级下限不能大于上限")
        catalog_id_min = int(raw.get("catalog_id_min", 0))
        catalog_id_max = int(raw.get("catalog_id_max", 0))
        if catalog_id_min <= 0 or catalog_id_max <= catalog_id_min:
            raise ValueError(f"{label}的catalog_id范围无效")
        chance = float(raw.get("chance", 0.0))
        if not 0 <= chance <= 1:
            raise ValueError(f"{label}.chance必须在0到1之间")
        return DungeonRewardSpec(
            equipment_count=equipment_count,
            equipment_level_min=equipment_level_min,
            equipment_level_max=equipment_level_max,
            catalog_id_min=catalog_id_min,
            catalog_id_max=catalog_id_max,
            chance=chance,
        )
