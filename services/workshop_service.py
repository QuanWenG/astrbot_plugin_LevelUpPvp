"""Transactional equipment salvage and directed-affix rework workflows."""

from __future__ import annotations

import json
import hashlib
import math
import random
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, replace

try:
    from ..models.equipment import SLOT_LABELS, EquipmentItem, EquipmentTemplate
    from ..models.workshop import (
        BulkSalvagePreview,
        BulkSalvageResult,
        DominatedSalvageItem,
        ReworkCost,
        ReworkDecision,
        ReworkPreview,
        SalvageResult,
        WorkshopWallet,
    )
    from .db import connect_db
    from .equipment_affixes import (
        effective_inherent_affixes,
        skill_level_affix_cap,
    )
    from .equipment_catalog import QUALITY_LABELS, QUALITY_MULTIPLIERS
    from .equipment_service import EquipmentService
    from .material_catalog import actual_weight, material_for
except ImportError:
    from models.equipment import SLOT_LABELS, EquipmentItem, EquipmentTemplate
    from models.workshop import (
        BulkSalvagePreview,
        BulkSalvageResult,
        DominatedSalvageItem,
        ReworkCost,
        ReworkDecision,
        ReworkPreview,
        SalvageResult,
        WorkshopWallet,
    )
    from services.db import connect_db
    from services.equipment_affixes import (
        effective_inherent_affixes,
        skill_level_affix_cap,
    )
    from services.equipment_catalog import QUALITY_LABELS, QUALITY_MULTIPLIERS
    from services.equipment_service import EquipmentService
    from services.material_catalog import actual_weight, material_for


WORKSHOP_RULESET_ID = "equipment-workshop-v11"
STANDARD_REWORK_MODE = "standard"
SEASON_REWORK_MODE = "season_imprint"
SEASON_REWORK_TOKEN_COST = 20

DIRECTION_LABELS = {
    "strength": "力量",
    "dexterity": "灵巧",
    "shooting": "射击",
    "arcane": "奥术",
    "defense": "防御",
    "fortune": "奇运",
}

_DIRECTION_ALIASES = {
    **{key: key for key in DIRECTION_LABELS},
    **{label: key for key, label in DIRECTION_LABELS.items()},
    "力": "strength",
    "敏捷": "dexterity",
    "远程": "shooting",
    "魔法": "arcane",
    "生存": "defense",
    "幸运": "fortune",
    "运气": "fortune",
}

_QUALITY_TIER = {
    "common": 1,
    "excellent": 2,
    "rare": 3,
    "epic": 4,
    "mythic": 5,
}
_SALVAGE_BASE = {
    "common": 5,
    "excellent": 9,
    "rare": 16,
    "epic": 28,
    "mythic": 44,
}

# Every cleanup policy is preview-only until its exact snapshot token is
# supplied.  ``dominated`` is conservative enough to include excellent/rare
# drops, but never epic/mythic, stars, bespoke effects or protected items.
_BULK_SALVAGE_POLICIES = {
    "common": "未穿戴普通装备",
    "excellent": "未穿戴普通与优秀装备",
    "dominated": "同槽同方向被完全支配的装备",
}
_BULK_SALVAGE_POLICY_ALIASES = {
    "common": "common",
    "普通": "common",
    "excellent": "excellent",
    "优秀": "excellent",
    "优秀及以下": "excellent",
    "dominated": "dominated",
    "支配": "dominated",
    "重复": "dominated",
    "安全": "dominated",
}
_BULK_SALVAGE_POLICY_COMMANDS = {
    "common": "普通",
    "excellent": "优秀",
    "dominated": "支配",
}
_REWORK_BASE = {
    "excellent": 18,
    "rare": 32,
    "epic": 52,
    "mythic": 80,
}
_QUALITY_AFFIX_SLOTS = {
    "excellent": 1,
    "rare": 2,
    "epic": 3,
    "mythic": 4,
}

_MELEE_SKILLS = {"longsword", "axe", "spear", "tactics", "unarmed"}
_DEXTERITY_SKILLS = {"shortsword", "unarmed"}
_SHOOTING_SKILLS = {"bow", "crossbow", "firearm", "throwing"}
_DAMAGE_AFFIXES = {
    "damage_magic",
    "damage_fire",
    "damage_cold",
    "damage_lightning",
    "damage_shadow",
    "damage_nature",
    "damage_mind",
    "damage_hell",
}

_DIRECTION_ORDER = tuple(DIRECTION_LABELS)
_WEAPON_DIRECTIONS = {
    "longsword": "strength",
    "axe": "strength",
    "spear": "strength",
    "blunt": "strength",
    "scythe": "strength",
    "shortsword": "dexterity",
    "staff": "arcane",
    "bow": "shooting",
    "crossbow": "shooting",
    "firearm": "shooting",
    "throwing": "shooting",
}
_PROTECTED_BULK_QUALITIES = {"epic", "mythic", "legendary"}


@dataclass(frozen=True)
class _DispositionProfile:
    item: EquipmentItem
    slot_key: str
    direction: str
    features: dict[str, float]
    weight: float
    free_capacity: int


def _profile_fingerprint(profile: _DispositionProfile) -> str:
    item = profile.item
    payload = json.dumps(
        {
            "id": int(item.id),
            "slot": profile.slot_key,
            "direction": profile.direction,
            "quality": item.quality,
            "level": int(item.item_level),
            "enhancement": int(item.enhancement_level),
            "capacity": int(item.enchant_capacity),
            "free_capacity": int(profile.free_capacity),
            "weight": profile.weight,
            "features": profile.features,
            "locked": bool(item.is_locked),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _bulk_item_fingerprint(item: EquipmentItem) -> str:
    """Bind confirmation to every persisted field of one selected item."""

    payload = json.dumps(
        item.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=12).hexdigest()


def normalize_rework_direction(direction: str) -> str:
    """Return the stable direction id accepted by persistence and scoring."""
    normalized = _DIRECTION_ALIASES.get(str(direction).strip().lower())
    if not normalized:
        choices = "、".join(DIRECTION_LABELS.values())
        raise ValueError(f"未知重铸方向，可选：{choices}")
    return normalized


def salvage_scrap_value(quality: str, item_level: int) -> int:
    """Calculate deterministic salvage value from quality and item level."""
    if quality not in _SALVAGE_BASE:
        raise ValueError("该品质不能分解")
    level = max(1, int(item_level))
    return _SALVAGE_BASE[quality] + math.ceil(level / 5) * _QUALITY_TIER[quality]


def rework_cost(quality: str, item_level: int) -> ReworkCost:
    """Calculate the transparent quality base and level surcharge."""
    if quality not in _REWORK_BASE:
        raise ValueError("只有优秀及以上的普通或白星装备可以定向重铸")
    tier = _QUALITY_TIER[quality]
    return ReworkCost(
        quality_base=_REWORK_BASE[quality],
        level_surcharge=math.ceil(max(1, int(item_level)) / 10) * (tier + 1),
    )


def affix_matches_direction(affix: dict, direction: str) -> bool:
    """Tell whether one affix contributes to the selected build direction."""
    direction = normalize_rework_direction(direction)
    kind = str(affix.get("type", ""))
    stat = str(affix.get("stat", ""))
    skill = str(affix.get("skill_id", ""))
    if direction == "strength":
        return (
            (kind == "stat_flat" and stat == "strength")
            or (kind == "skill_level" and skill in _MELEE_SKILLS)
            or kind in {"melee_followup", "armor_penetration"}
        )
    if direction == "dexterity":
        return (
            (kind == "stat_flat" and stat == "dexterity")
            or (kind == "skill_level" and skill in _DEXTERITY_SKILLS)
            or kind in {"evasion", "critical_rate", "melee_followup"}
        )
    if direction == "shooting":
        return (
            (kind == "stat_flat" and stat in {"dexterity", "perception"})
            or (kind == "skill_level" and skill in _SHOOTING_SKILLS)
            or kind in {"ranged_followup", "accuracy"}
        )
    if direction == "arcane":
        return (
            (kind == "stat_flat" and stat in {"magic", "willpower"})
            or kind in _DAMAGE_AFFIXES
            or kind == "spell_power"
        )
    if direction == "defense":
        return (
            (kind == "stat_flat" and stat in {"constitution", "willpower"})
            or kind.startswith("resistance_")
            or kind
            in {
                "element_resistance",
                "block_rate",
                "knockback_resistance",
                "evasion",
                "status_resistance",
            }
        )
    return (
        (kind == "advanced_stat" and stat == "luck")
        or kind in {"critical_rate", "life_steal", "accuracy", "evasion"}
    )


def direction_match_score(affixes: tuple[dict, ...], direction: str) -> int:
    """Score a candidate by the share of affix capacity matching its target."""
    if not affixes:
        return 0
    total = sum(max(1, int(affix.get("capacity", 1))) for affix in affixes)
    matched = sum(
        max(1, int(affix.get("capacity", 1)))
        for affix in affixes
        if affix_matches_direction(affix, direction)
    )
    return round(100 * matched / max(1, total))


def _slot_key(item: EquipmentItem) -> str:
    slot = (
        "finger"
        if item.equip_slot in {"left_finger", "right_finger"}
        else item.equip_slot
    )
    return f"{slot}:{item.item_type}:{item.hand_mode}"


def _affix_key(affix: dict) -> str:
    identity = {
        key: value
        for key, value in affix.items()
        if key not in {"value", "capacity"}
    }
    return "affix:" + json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _affix_directions(affix: dict) -> tuple[str, ...]:
    kind = str(affix.get("type", ""))
    matched = {
        direction
        for direction in _DIRECTION_ORDER
        if affix_matches_direction(affix, direction)
    }
    if kind.startswith("status_resistance") or kind in {
        "status_immunity",
        "max_hp",
        "max_stamina",
    }:
        matched.add("defense")
    if kind in {"execute_chance", "life_steal", "stamina_steal"}:
        matched.add("fortune")
    if kind == "mana_steal":
        matched.add("arcane")
    return tuple(direction for direction in _DIRECTION_ORDER if direction in matched)


def _feature_directions(
    item: EquipmentItem,
    feature_key: str,
) -> tuple[str, ...]:
    if feature_key.startswith("affix:"):
        raw = json.loads(feature_key.removeprefix("affix:"))
        return _affix_directions(raw)
    if feature_key.startswith("primary:"):
        stat = feature_key.split(":", 1)[1]
        return {
            "strength": ("strength",),
            "constitution": ("defense",),
            "dexterity": ("dexterity",),
            "perception": ("shooting",),
            "magic": ("arcane",),
            "willpower": ("arcane", "defense"),
        }.get(stat, ())
    if feature_key.startswith("advanced:"):
        stat = feature_key.split(":", 1)[1]
        return {
            "luck": ("fortune",),
            "speed": ("dexterity", "fortune"),
            "life_growth": ("defense",),
            "mana_growth": ("arcane",),
        }.get(stat, ())
    if feature_key.startswith("skill:"):
        skill_id = feature_key.split(":", 1)[1]
        pseudo_affix = {"type": "skill_level", "skill_id": skill_id}
        return _affix_directions(pseudo_affix)
    if feature_key in {"base:weapon_power", "base:accuracy"}:
        direction = _WEAPON_DIRECTIONS.get(item.weapon_type)
        if direction:
            return (direction,)
        return ("shooting",) if feature_key.endswith("accuracy") else ()
    if feature_key in {
        "base:armor_power",
        "effect:max_hp",
        "effect:block_rate",
        "effect:knockback_resistance",
    } or feature_key.startswith("resistance:"):
        return ("defense",)
    if feature_key == "base:evasion":
        return ("dexterity", "defense")
    if feature_key == "effect:action_speed":
        return ("dexterity", "fortune")
    return ()


def _equipment_features(
    item: EquipmentItem,
    user_level: int,
    *,
    allow_opaque: bool,
) -> dict[str, float] | None:
    """Resolve a conservative, current-level Pareto vector for one item."""

    material = material_for(item.material)
    level_factor = max(
        0.50,
        1.0 - max(0, int(item.item_level) - int(user_level)) * 0.03,
    )
    quality_factor = QUALITY_MULTIPLIERS.get(item.quality, 1.0)
    factor = quality_factor * level_factor
    values: defaultdict[str, float] = defaultdict(float)

    for effect in material.effects:
        key = f"{effect.effect_type}:{effect.target}"
        values[key] += float(effect.value)
    for stat, raw in item.base_stats.items():
        raw_value = float(raw)
        if raw_value < 0:
            return None
        key = f"base:{stat}"
        value = raw_value * factor
        if stat in {"atk", "weapon_power"} and item.item_type == "weapon":
            key = "base:weapon_power"
            value = (
                raw_value * material.attack_factor * quality_factor
                + item.enhancement_level
                + item.item_level // 10
            ) * level_factor
        elif stat in {"defense", "armor_power"}:
            key = "base:armor_power"
            value = (
                raw_value * material.defense_factor * quality_factor
                + item.enhancement_level * 2
                + item.item_level / 12.0
            ) * level_factor
        elif stat == "accuracy":
            value = raw_value * material.accuracy_factor * factor
        elif stat == "evasion":
            value = raw_value * material.evasion_factor * factor
        elif stat in {
            "strength",
            "constitution",
            "dexterity",
            "perception",
            "magic",
            "willpower",
        }:
            key = f"primary:{stat}"
        elif stat == "max_hp":
            key = "effect:max_hp"
        elif stat == "action_speed":
            key = "effect:action_speed"
        values[key] += value

    affixes = (
        effective_inherent_affixes(
            item.inherent_affixes,
            int(user_level),
            int(item.item_level),
        )
        + tuple(item.random_affixes)
        + tuple(item.fusion_affixes)
    )
    for affix in affixes:
        kind = str(affix.get("type", ""))
        if kind == "trigger_ability":
            if allow_opaque:
                continue
            return None
        try:
            value = float(affix.get("value", 0))
        except (TypeError, ValueError):
            if allow_opaque:
                continue
            return None
        if value < 0:
            return None
        directions = _affix_directions(affix)
        if not directions:
            if allow_opaque:
                continue
            return None
        values[_affix_key(affix)] += value
    return {
        key: round(value, 8)
        for key, value in values.items()
        if value > 0
    }


def _disposition_profile(
    item: EquipmentItem,
    user_level: int,
    *,
    allow_opaque: bool,
) -> _DispositionProfile | None:
    features = _equipment_features(
        item,
        int(user_level),
        allow_opaque=allow_opaque,
    )
    if features is None:
        return None
    if item.item_type == "weapon":
        direction = _WEAPON_DIRECTIONS.get(item.weapon_type, "")
    elif item.item_type in {"armor", "shield"}:
        direction = "defense"
    else:
        direction_scores: defaultdict[str, float] = defaultdict(float)
        for feature_key, value in features.items():
            for candidate in _feature_directions(item, feature_key):
                direction_scores[candidate] += abs(float(value))
        direction = max(
            _DIRECTION_ORDER,
            key=lambda candidate: (
                direction_scores[candidate],
                -_DIRECTION_ORDER.index(candidate),
            ),
        ) if any(direction_scores.values()) else ""
    if not direction:
        return None
    return _DispositionProfile(
        item=item,
        slot_key=_slot_key(item),
        direction=direction,
        features=features,
        weight=round(actual_weight(item.weight, item.material), 8),
        free_capacity=max(0, item.enchant_capacity - item.used_capacity),
    )


def _profile_dominates(
    keeper: _DispositionProfile,
    candidate: _DispositionProfile,
) -> bool:
    if keeper.slot_key != candidate.slot_key:
        return False
    if keeper.direction != candidate.direction:
        return False
    left, right = keeper.item, candidate.item
    if _QUALITY_TIER.get(left.quality, 0) < _QUALITY_TIER.get(right.quality, 0):
        return False
    if int(left.item_level) < int(right.item_level):
        return False
    if int(left.enhancement_level) < int(right.enhancement_level):
        return False
    if int(left.enchant_capacity) < int(right.enchant_capacity):
        return False
    if keeper.free_capacity < candidate.free_capacity:
        return False
    if keeper.weight > candidate.weight + 1e-8:
        return False
    if any(
        keeper.features.get(key, 0.0) + 1e-8 < value
        for key, value in candidate.features.items()
    ):
        return False
    return any((
        (
            _QUALITY_TIER.get(left.quality, 0)
            > _QUALITY_TIER.get(right.quality, 0)
        ),
        int(left.item_level) > int(right.item_level),
        int(left.enhancement_level) > int(right.enhancement_level),
        int(left.enchant_capacity) > int(right.enchant_capacity),
        keeper.free_capacity > candidate.free_capacity,
        keeper.weight + 1e-8 < candidate.weight,
        any(
            keeper.features.get(key, 0.0) > value + 1e-8
            for key, value in candidate.features.items()
        ),
        any(key not in candidate.features for key in keeper.features),
    ))


def _can_be_dominated_cleanup_target(item: EquipmentItem) -> bool:
    return (
        item.item_level > 0
        and item.quality not in _PROTECTED_BULK_QUALITIES
        and item.quality in _SALVAGE_BASE
        and item.star_type == "none"
        and item.blessing_state == "normal"
        and not item.is_locked
        and not item.source_effects
        and not item.fusion_affixes
        and not any(
            str(affix.get("type", "")) == "trigger_ability"
            for affix in item.inherent_affixes + item.random_affixes
        )
    )


def _can_be_quality_cleanup_target(
    item: EquipmentItem,
    allowed_qualities: frozenset[str],
) -> bool:
    """Protect invested or unusual gear from threshold-based cleanup."""

    return (
        item.item_level > 0
        and item.quality in allowed_qualities
        and item.star_type == "none"
        and item.blessing_state == "normal"
        and item.enhancement_level == 0
        and not item.is_locked
        and not item.source_effects
        and not item.fusion_affixes
        and not any(
            str(affix.get("type", "")) == "trigger_ability"
            for affix in item.inherent_affixes + item.random_affixes
        )
    )


class WorkshopService:
    """Thin workflow facade over pure workshop rules and equipment storage."""

    def __init__(
        self,
        db_path: str,
        equipment_service: EquipmentService | None = None,
        seed_source=None,
        ruleset_id: str = WORKSHOP_RULESET_ID,
    ) -> None:
        self.db_path = db_path
        self.equipment = equipment_service or EquipmentService(db_path)
        self._seed_source = seed_source or (lambda: secrets.randbits(63))
        self.ruleset_id = str(ruleset_id)

    async def wallet(self, user_pk: int) -> WorkshopWallet:
        async with await connect_db(self.db_path) as db:
            wallet = await self._wallet_in_db(db, int(user_pk), create=True)
            await db.commit()
            return wallet

    async def salvage(self, user_pk: int, equipment_id: int) -> SalvageResult:
        """Delete one eligible inventory item and atomically credit scrap."""
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                item = await self.equipment.get_owned_item_in_db(
                    db, int(user_pk), int(equipment_id)
                )
                await self._ensure_not_equipped_in_db(db, item)
                self._validate_salvage_item(item)
                rework_state = await self._rework_state_in_db(db, int(item.id))
                if rework_state and rework_state["status"] == "pending":
                    raise ValueError("该装备有待决定的重铸预览，请先接受或放弃")
                gained = salvage_scrap_value(item.quality, item.item_level)
                now = int(time.time())
                await db.execute(
                    "INSERT INTO workshop_wallet "
                    "(user_pk, scrap_balance, lifetime_earned, lifetime_spent, updated_at_ts) "
                    "VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(user_pk) DO UPDATE SET "
                    "scrap_balance = scrap_balance + excluded.scrap_balance, "
                    "lifetime_earned = lifetime_earned + excluded.lifetime_earned, "
                    "updated_at_ts = excluded.updated_at_ts",
                    (int(user_pk), gained, gained, now),
                )
                await db.execute(
                    "DELETE FROM equipment_items WHERE id = ? AND owner_pk = ?",
                    (item.id, int(user_pk)),
                )
                wallet = await self._wallet_in_db(db, int(user_pk))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return SalvageResult(
            equipment_id=int(item.id),
            equipment_name=item.name,
            quality=item.quality,
            item_level=item.item_level,
            scrap_gained=gained,
            balance_after=wallet.scrap_balance,
        )

    async def preview_bulk_salvage(
        self,
        user_pk: int,
        quality: str = "common",
    ) -> BulkSalvagePreview:
        """Preview safe bulk cleanup without changing inventory.

        The returned token binds the exact policy result so a later drop,
        protection change or equipment change cannot silently expand what the
        confirmation will destroy.
        """

        policy_id = self._normalize_bulk_salvage_policy(quality)
        async with await connect_db(self.db_path) as db:
            items, dominated = await self._bulk_salvage_plan_in_db(
                db, int(user_pk), policy_id
            )
        if not items:
            raise ValueError(self._empty_bulk_salvage_message(policy_id))
        return self._bulk_salvage_preview(
            int(user_pk), policy_id, items, dominated
        )

    async def bulk_salvage(
        self,
        user_pk: int,
        quality: str,
        confirmation_token: str,
    ) -> BulkSalvageResult:
        """Atomically salvage the exact conservative set shown in a preview."""

        policy_id = self._normalize_bulk_salvage_policy(quality)
        supplied_token = str(confirmation_token or "").strip().lower()
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                items, dominated = await self._bulk_salvage_plan_in_db(
                    db, int(user_pk), policy_id
                )
                if not items:
                    raise ValueError(self._empty_bulk_salvage_message(policy_id))
                preview = self._bulk_salvage_preview(
                    int(user_pk), policy_id, items, dominated
                )
                if supplied_token != preview.confirmation_token:
                    raise ValueError(
                        "背包内容已变化或确认码错误，请重新使用 "
                        "/工坊 整理 "
                        f"{_BULK_SALVAGE_POLICY_COMMANDS[policy_id]}"
                    )
                gained = preview.scrap_total
                now = int(time.time())
                await db.execute(
                    "INSERT INTO workshop_wallet "
                    "(user_pk, scrap_balance, lifetime_earned, lifetime_spent, updated_at_ts) "
                    "VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(user_pk) DO UPDATE SET "
                    "scrap_balance = scrap_balance + excluded.scrap_balance, "
                    "lifetime_earned = lifetime_earned + excluded.lifetime_earned, "
                    "updated_at_ts = excluded.updated_at_ts",
                    (int(user_pk), gained, gained, now),
                )
                ids = tuple(int(item.id) for item in items)
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    "DELETE FROM equipment_items WHERE owner_pk = ? "
                    f"AND id IN ({placeholders})",
                    (int(user_pk), *ids),
                )
                cursor = await db.execute(
                    "SELECT COUNT(*) AS count FROM equipment_items "
                    f"WHERE owner_pk = ? AND id IN ({placeholders})",
                    (int(user_pk), *ids),
                )
                remaining = int((await cursor.fetchone())["count"])
                await cursor.close()
                if remaining:
                    raise RuntimeError("批量分解目标发生变化，请重试")
                wallet = await self._wallet_in_db(db, int(user_pk))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return BulkSalvageResult(
            user_pk=int(user_pk),
            quality=policy_id,
            item_count=len(ids),
            equipment_ids=ids,
            scrap_gained=gained,
            balance_after=wallet.scrap_balance,
        )

    async def preview_rework(
        self,
        user_pk: int,
        equipment_id: int,
        direction: str,
        seed: int | None = None,
    ) -> ReworkPreview:
        """Spend scrap once and persist a candidate awaiting accept/reject."""

        return await self._preview_rework(
            user_pk=user_pk,
            equipment_id=equipment_id,
            direction=direction,
            seed=seed,
            mode=STANDARD_REWORK_MODE,
        )

    async def preview_season_rework(
        self,
        user_pk: int,
        equipment_id: int,
        direction: str,
        seed: int | None = None,
    ) -> ReworkPreview:
        """Spend normal scrap plus season tokens for one guaranteed direction."""

        return await self._preview_rework(
            user_pk=user_pk,
            equipment_id=equipment_id,
            direction=direction,
            seed=seed,
            mode=SEASON_REWORK_MODE,
        )

    async def _preview_rework(
        self,
        *,
        user_pk: int,
        equipment_id: int,
        direction: str,
        seed: int | None,
        mode: str,
    ) -> ReworkPreview:
        """Create and fully pay one immutable pending preview transaction."""

        if mode not in {STANDARD_REWORK_MODE, SEASON_REWORK_MODE}:
            raise ValueError("未知工坊重铸模式")
        direction_id = normalize_rework_direction(direction)
        season_imprint = mode == SEASON_REWORK_MODE
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                item = await self.equipment.get_owned_item_in_db(
                    db, int(user_pk), int(equipment_id)
                )
                await self._ensure_not_equipped_in_db(db, item)
                self._validate_rework_item(item)
                state = await self._rework_state_in_db(db, int(item.id))
                if state and state["status"] == "pending":
                    raise ValueError("该装备已有待决定的重铸预览，请先接受或放弃")

                miss_before = self._prior_miss_streak(state, direction_id)
                prior_direction, prior_streak = self._prior_standard_pity(state)
                pity_active = not season_imprint and miss_before >= 4
                cost = replace(
                    rework_cost(item.quality, item.item_level),
                    season_tokens=(
                        SEASON_REWORK_TOKEN_COST if season_imprint else 0
                    ),
                )
                wallet = await self._wallet_in_db(db, int(user_pk), create=True)
                if wallet.scrap_balance < cost.total:
                    raise ValueError(
                        f"碎片不足：需要{cost.total}，当前{wallet.scrap_balance}"
                    )
                if wallet.season_tokens < cost.season_tokens:
                    raise ValueError(
                        "赛季币不足："
                        f"需要{cost.season_tokens}，当前{wallet.season_tokens}"
                    )
                rng = random.Random(
                    int(seed) if seed is not None else int(self._seed_source())
                )
                candidate = self._roll_candidate_affixes(
                    item,
                    direction_id,
                    rng,
                    force_target=season_imprint or pity_active,
                )
                score = direction_match_score(candidate, direction_id)
                if season_imprint and score <= 0:
                    raise RuntimeError("赛季刻印未生成目标方向词条")
                miss_after = (
                    miss_before
                    if season_imprint
                    else (0 if score > 0 else miss_before + 1)
                )
                standard_direction_after = (
                    prior_direction if season_imprint else direction_id
                )
                standard_streak_after = (
                    prior_streak if season_imprint else miss_after
                )
                now = int(time.time())
                cursor = await db.execute(
                    "UPDATE workshop_wallet SET scrap_balance = scrap_balance - ?, "
                    "season_tokens = season_tokens - ?, "
                    "lifetime_spent = lifetime_spent + ?, updated_at_ts = ? "
                    "WHERE user_pk = ? AND scrap_balance >= ? "
                    "AND season_tokens >= ?",
                    (
                        cost.total,
                        cost.season_tokens,
                        cost.total,
                        now,
                        int(user_pk),
                        cost.total,
                        cost.season_tokens,
                    ),
                )
                await cursor.close()
                debited_wallet = await self._wallet_in_db(db, int(user_pk))
                if (
                    debited_wallet.scrap_balance
                    != wallet.scrap_balance - cost.total
                    or debited_wallet.season_tokens
                    != wallet.season_tokens - cost.season_tokens
                ):
                    raise RuntimeError("工坊扣款状态不一致，重铸已取消")

                original_snapshot = {
                    "item": item.to_dict(),
                    "direction": direction_id,
                    "miss_streak_before": miss_before,
                    "mode": mode,
                    "scrap_balance_before": wallet.scrap_balance,
                    "season_tokens_before": wallet.season_tokens,
                }
                candidate_snapshot = {
                    "candidate_affixes": list(candidate),
                    "candidate_used_capacity": item.used_capacity,
                    "direction": direction_id,
                    "match_score": score,
                    "miss_streak_after": miss_after,
                    "pity_guaranteed": pity_active,
                    "target_guaranteed": season_imprint or pity_active,
                    "cost": cost.total,
                    "season_tokens_cost": cost.season_tokens,
                    "mode": mode,
                    "guarantee_source": (
                        "season_imprint"
                        if season_imprint
                        else ("pity" if pity_active else "none")
                    ),
                    "standard_miss_direction": standard_direction_after,
                    "standard_miss_streak_after": standard_streak_after,
                    "scrap_balance_after": wallet.scrap_balance - cost.total,
                    "season_tokens_after": (
                        wallet.season_tokens - cost.season_tokens
                    ),
                }
                await db.execute(
                    "INSERT INTO equipment_rework_state "
                    "(equipment_id, ruleset_id, status, original_snapshot_json, "
                    "reworked_snapshot_json, updated_at_ts) "
                    "VALUES (?, ?, 'pending', ?, ?, ?) "
                    "ON CONFLICT(equipment_id, ruleset_id) DO UPDATE SET "
                    "status = 'pending', "
                    "original_snapshot_json = excluded.original_snapshot_json, "
                    "reworked_snapshot_json = excluded.reworked_snapshot_json, "
                    "updated_at_ts = excluded.updated_at_ts",
                    (
                        item.id,
                        self.ruleset_id,
                        self._dump(original_snapshot),
                        self._dump(candidate_snapshot),
                        now,
                    ),
                )
                wallet_after = await self._wallet_in_db(db, int(user_pk))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return ReworkPreview(
            equipment_id=int(item.id),
            equipment_name=item.name,
            direction=direction_id,
            direction_label=DIRECTION_LABELS[direction_id],
            cost=cost,
            balance_after=wallet_after.scrap_balance,
            candidate_affixes=candidate,
            match_score=score,
            miss_streak_before=miss_before,
            miss_streak_after=miss_after,
            pity_guaranteed=pity_active,
            mode=mode,
            target_guaranteed=season_imprint or pity_active,
            season_tokens_after=wallet_after.season_tokens,
        )

    async def decide_rework(
        self,
        user_pk: int,
        equipment_id: int,
        accept: bool,
    ) -> ReworkDecision:
        """Atomically accept or reject the already-paid pending candidate."""
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                item = await self.equipment.get_owned_item_in_db(
                    db, int(user_pk), int(equipment_id)
                )
                await self._ensure_not_equipped_in_db(db, item)
                self._validate_rework_item(item)
                state = await self._rework_state_in_db(db, int(item.id))
                if not state or state["status"] != "pending":
                    raise ValueError("该装备没有待决定的重铸预览")
                original = self._load(state["original_snapshot_json"])
                candidate = self._load(state["reworked_snapshot_json"])
                original_affixes = tuple(
                    original.get("item", {}).get("random_affixes", ())
                )
                if tuple(item.random_affixes) != original_affixes:
                    raise ValueError("装备词条已发生变化，不能应用旧预览")

                candidate_affixes = tuple(candidate.get("candidate_affixes", ()))
                candidate_used = int(
                    candidate.get("candidate_used_capacity", item.used_capacity)
                )
                decided_item = item
                if bool(accept):
                    decided_item = replace(
                        item,
                        random_affixes=candidate_affixes,
                        used_capacity=candidate_used,
                    )
                    await db.execute(
                        "UPDATE equipment_items SET random_affixes_json = ?, "
                        "used_capacity = ? WHERE id = ? AND owner_pk = ?",
                        (
                            self._dump(list(candidate_affixes)),
                            candidate_used,
                            item.id,
                            int(user_pk),
                        ),
                    )
                status = "accepted" if bool(accept) else "rejected"
                decided_at = int(time.time())
                candidate["decision"] = status
                candidate["decided_at_ts"] = decided_at
                candidate["decided_random_affixes"] = list(
                    decided_item.random_affixes
                )
                await db.execute(
                    "UPDATE equipment_rework_state SET status = ?, "
                    "reworked_snapshot_json = ?, updated_at_ts = ? "
                    "WHERE equipment_id = ? AND ruleset_id = ? AND status = 'pending'",
                    (
                        status,
                        self._dump(candidate),
                        decided_at,
                        item.id,
                        self.ruleset_id,
                    ),
                )
                wallet = await self._wallet_in_db(db, int(user_pk), create=True)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return ReworkDecision(
            equipment_id=int(item.id),
            accepted=bool(accept),
            direction=str(candidate.get("direction", "")),
            scrap_spent=int(candidate.get("cost", 0)),
            balance=wallet.scrap_balance,
            match_score=int(candidate.get("match_score", 0)),
            miss_streak=int(candidate.get("miss_streak_after", 0)),
            item=decided_item,
            mode=str(candidate.get("mode", STANDARD_REWORK_MODE)),
            season_tokens_spent=int(candidate.get("season_tokens_cost", 0)),
            season_tokens_balance=wallet.season_tokens,
        )

    async def accept_rework(
        self, user_pk: int, equipment_id: int
    ) -> ReworkDecision:
        return await self.decide_rework(user_pk, equipment_id, True)

    async def reject_rework(
        self, user_pk: int, equipment_id: int
    ) -> ReworkDecision:
        return await self.decide_rework(user_pk, equipment_id, False)

    async def _wallet_in_db(
        self,
        db,
        user_pk: int,
        create: bool = False,
    ) -> WorkshopWallet:
        if create:
            await db.execute(
                "INSERT OR IGNORE INTO workshop_wallet "
                "(user_pk, scrap_balance, lifetime_earned, lifetime_spent, updated_at_ts) "
                "VALUES (?, 0, 0, 0, ?)",
                (int(user_pk), int(time.time())),
            )
        cursor = await db.execute(
            "SELECT user_pk, scrap_balance, lifetime_earned, lifetime_spent, "
            "season_tokens "
            "FROM workshop_wallet WHERE user_pk = ?",
            (int(user_pk),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return WorkshopWallet(int(user_pk), 0, 0, 0)
        return WorkshopWallet(
            int(row["user_pk"]),
            int(row["scrap_balance"]),
            int(row["lifetime_earned"]),
            int(row["lifetime_spent"]),
            int(row["season_tokens"]),
        )

    @staticmethod
    def _normalize_bulk_salvage_policy(quality: str) -> str:
        raw = str(quality or "").strip().lower()
        policy_id = _BULK_SALVAGE_POLICY_ALIASES.get(raw)
        if not policy_id:
            raise ValueError("批量整理只支持普通、优秀或支配策略")
        return policy_id

    @staticmethod
    def _empty_bulk_salvage_message(policy_id: str) -> str:
        if policy_id == "dominated":
            return "没有符合安全规则的同槽同方向被支配装备"
        if policy_id == "excellent":
            return "没有符合安全规则的未穿戴普通或优秀装备"
        return "没有可批量分解的未穿戴普通装备"

    async def _bulk_salvage_plan_in_db(
        self,
        db,
        user_pk: int,
        policy_id: str,
    ) -> tuple[tuple[EquipmentItem, ...], tuple[DominatedSalvageItem, ...]]:
        items = await self.equipment.list_items_in_db(db, int(user_pk))
        cursor = await db.execute(
            "SELECT equipment_id FROM equipment_loadout WHERE user_pk = ?",
            (int(user_pk),),
        )
        equipped_ids = {
            int(row["equipment_id"]) for row in await cursor.fetchall()
        }
        await cursor.close()
        cursor = await db.execute(
            "SELECT equipment_id FROM equipment_rework_state "
            "WHERE status = 'pending'"
        )
        pending_ids = {
            int(row["equipment_id"]) for row in await cursor.fetchall()
        }
        await cursor.close()
        excluded_ids = equipped_ids | pending_ids
        if policy_id in {"common", "excellent"}:
            allowed_qualities = (
                frozenset({"common", "excellent"})
                if policy_id == "excellent"
                else frozenset({"common"})
            )
            selected = tuple(sorted(
                (
                    item for item in items
                    if _can_be_quality_cleanup_target(
                        item,
                        allowed_qualities,
                    )
                    and int(item.id) not in excluded_ids
                ),
                key=lambda item: int(item.id),
            ))
            return selected, ()

        cursor = await db.execute(
            "SELECT level FROM users WHERE id = ?",
            (int(user_pk),),
        )
        user_row = await cursor.fetchone()
        await cursor.close()
        user_level = int(user_row["level"]) if user_row else 1
        keeper_profiles = tuple(
            profile
            for item in items
            if (
                profile := _disposition_profile(
                    item,
                    user_level,
                    allow_opaque=True,
                )
            ) is not None
        )
        matches: list[
            tuple[EquipmentItem, _DispositionProfile, _DispositionProfile]
        ] = []
        reasons: list[DominatedSalvageItem] = []
        for item in items:
            if (
                int(item.id) in excluded_ids
                or not _can_be_dominated_cleanup_target(item)
            ):
                continue
            candidate = _disposition_profile(
                item,
                user_level,
                allow_opaque=False,
            )
            if candidate is None:
                continue
            dominators = tuple(
                profile
                for profile in keeper_profiles
                if int(profile.item.id) != int(item.id)
                and _profile_dominates(profile, candidate)
            )
            if not dominators:
                continue
            keeper = max(
                dominators,
                key=lambda profile: (
                    _QUALITY_TIER.get(profile.item.quality, 0),
                    int(profile.item.item_level),
                    int(profile.item.enchant_capacity),
                    sum(profile.features.values()),
                    -profile.weight,
                    -int(profile.item.id),
                ),
            )
            matches.append((item, candidate, keeper))
        matches.sort(key=lambda value: int(value[0].id))
        for item, candidate, keeper in matches:
            direction_label = DIRECTION_LABELS[keeper.direction]
            slot = (
                "指环"
                if item.equip_slot in {"left_finger", "right_finger"}
                else SLOT_LABELS.get(item.equip_slot, item.equip_slot)
            )
            reasons.append(
                DominatedSalvageItem(
                    equipment_id=int(item.id),
                    equipment_name=item.name,
                    quality=item.quality,
                    item_level=item.item_level,
                    slot_label=slot,
                    direction_labels=(direction_label,),
                    keeper_id=int(keeper.item.id),
                    keeper_name=keeper.item.name,
                    keeper_quality=keeper.item.quality,
                    keeper_level=keeper.item.item_level,
                    candidate_fingerprint=_profile_fingerprint(candidate),
                    keeper_fingerprint=_profile_fingerprint(keeper),
                )
            )
        return tuple(item for item, _candidate, _keeper in matches), tuple(reasons)

    @staticmethod
    def _bulk_salvage_preview(
        user_pk: int,
        policy_id: str,
        items: tuple[EquipmentItem, ...],
        dominated_items: tuple[DominatedSalvageItem, ...] = (),
    ) -> BulkSalvagePreview:
        item_rows = tuple(
            (int(item.id), str(item.name), int(item.item_level))
            for item in items
        )
        payload = "|".join(
            [str(int(user_pk)), policy_id]
            + [
                f"{item_id}:{_bulk_item_fingerprint(item)}"
                for item, (item_id, _name, _level) in zip(items, item_rows)
            ]
            + [
                "keeper:"
                f"{item.equipment_id}:{item.candidate_fingerprint}:"
                f"{item.keeper_id}:{item.keeper_fingerprint}"
                for item in dominated_items
            ]
        ).encode("utf-8")
        token = hashlib.blake2b(payload, digest_size=5).hexdigest()
        return BulkSalvagePreview(
            user_pk=int(user_pk),
            quality=policy_id,
            quality_label=_BULK_SALVAGE_POLICIES[policy_id],
            items=item_rows,
            scrap_total=sum(
                salvage_scrap_value(item.quality, item.item_level)
                for item in items
            ),
            confirmation_token=token,
            policy_id=policy_id,
            dominated_items=dominated_items,
        )

    async def _rework_state_in_db(self, db, equipment_id: int):
        cursor = await db.execute(
            "SELECT status, original_snapshot_json, reworked_snapshot_json "
            "FROM equipment_rework_state WHERE equipment_id = ? AND ruleset_id = ?",
            (int(equipment_id), self.ruleset_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _ensure_not_equipped_in_db(
        self, db, item: EquipmentItem
    ) -> None:
        cursor = await db.execute(
            "SELECT 1 FROM equipment_loadout WHERE equipment_id = ? LIMIT 1",
            (item.id,),
        )
        equipped = await cursor.fetchone()
        await cursor.close()
        if equipped:
            raise ValueError("装备中的物品不能分解或重铸，请先卸下")

    @staticmethod
    def _validate_salvage_item(item: EquipmentItem) -> None:
        if item.is_locked:
            raise ValueError("该装备已收藏锁定，请先在工坊取消收藏")
        if item.item_level <= 0:
            raise ValueError("新手装备不能分解")
        if item.star_type == "black_star" or item.quality == "legendary":
            raise ValueError("黑星装备不能分解")
        if item.quality not in _SALVAGE_BASE:
            raise ValueError("该装备不能分解")

    @staticmethod
    def _validate_rework_item(item: EquipmentItem) -> None:
        if item.star_type not in {"none", "white_star"}:
            raise ValueError("黑星装备不能重铸")
        if item.quality not in _REWORK_BASE:
            raise ValueError("只有优秀及以上的普通或白星装备可以定向重铸")
        slot_limit = _QUALITY_AFFIX_SLOTS[item.quality]
        if not item.random_affixes:
            raise ValueError("该装备没有可重铸的随机词条")
        if len(item.random_affixes) > slot_limit:
            raise ValueError("装备随机词条数量超过原品质上限")
        random_capacity = sum(
            max(0, int(affix.get("capacity", 0)))
            for affix in item.random_affixes
        )
        fusion_capacity = sum(
            max(0, int(affix.get("capacity", 0)))
            for affix in item.fusion_affixes
        )
        if random_capacity + fusion_capacity > item.enchant_capacity:
            raise ValueError("装备词条容量异常，不能重铸")

    @classmethod
    def _prior_miss_streak(cls, state, direction: str) -> int:
        previous_direction, previous_streak = cls._prior_standard_pity(state)
        if previous_direction != direction:
            return 0
        return previous_streak

    @staticmethod
    def _prior_standard_pity(state) -> tuple[str, int]:
        """Read ordinary rework pity without letting an imprint consume it."""

        if not state:
            return "", 0
        try:
            previous = json.loads(state["reworked_snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return "", 0
        mode = str(previous.get("mode", STANDARD_REWORK_MODE))
        fallback_direction = (
            str(previous.get("direction", ""))
            if mode == STANDARD_REWORK_MODE
            else ""
        )
        fallback_streak = (
            previous.get("miss_streak_after", 0)
            if mode == STANDARD_REWORK_MODE
            else 0
        )
        direction = str(
            previous.get("standard_miss_direction", fallback_direction)
        )
        try:
            streak = int(
                previous.get("standard_miss_streak_after", fallback_streak)
            )
        except (TypeError, ValueError):
            streak = 0
        return direction, max(0, min(4, streak))

    def _roll_candidate_affixes(
        self,
        item: EquipmentItem,
        direction: str,
        rng: random.Random,
        force_target: bool,
    ) -> tuple[dict, ...]:
        entry = self.equipment.catalog.snapshot.by_template_id.get(item.template_id)
        if entry:
            template = replace(entry.template, material=item.material)
        else:
            template = EquipmentTemplate(
                template_id=item.template_id,
                name=item.name,
                item_type=item.item_type,
                equip_slot=item.equip_slot,
                hand_mode=item.hand_mode,
                weapon_type=item.weapon_type,
                armor_type=item.armor_type,
                material=item.material,
                weight=item.weight,
                base_stats=dict(item.base_stats),
                inherent_affixes=tuple(item.inherent_affixes),
                weight_range_exception=True,
                description=item.description,
                source_effects=tuple(item.source_effects),
            )
        rolled = self.equipment.factory.generate(
            item.owner_pk,
            template,
            item.item_level,
            item.quality,
            rng.getrandbits(63),
        ).random_affixes
        affixes: list[dict] = []
        for index, original in enumerate(item.random_affixes):
            affix = dict(rolled[index])
            if rng.random() < 0.30:
                affix = self._target_affix(direction, item.item_level, rng)
            affix["capacity"] = max(0, int(original.get("capacity", 0)))
            affixes.append(affix)
        candidate = tuple(affixes)
        if force_target and direction_match_score(candidate, direction) == 0:
            target_index = rng.randrange(len(affixes))
            capacity = affixes[target_index]["capacity"]
            affixes[target_index] = self._target_affix(
                direction, item.item_level, rng
            )
            affixes[target_index]["capacity"] = capacity
            candidate = tuple(affixes)
        return candidate

    @staticmethod
    def _target_affix(
        direction: str,
        item_level: int,
        rng: random.Random,
    ) -> dict:
        pools = {
            "strength": (
                ("stat_flat", "strength"),
                ("skill_level", "longsword"),
                ("skill_level", "axe"),
                ("skill_level", "spear"),
                ("melee_followup", ""),
                ("armor_penetration", ""),
            ),
            "dexterity": (
                ("stat_flat", "dexterity"),
                ("skill_level", "shortsword"),
                ("evasion", ""),
                ("critical_rate", ""),
                ("melee_followup", ""),
            ),
            "shooting": (
                ("stat_flat", "perception"),
                ("skill_level", "bow"),
                ("skill_level", "firearm"),
                ("skill_level", "throwing"),
                ("ranged_followup", ""),
                ("accuracy", ""),
            ),
            "arcane": (
                ("stat_flat", "magic"),
                ("stat_flat", "willpower"),
                ("spell_power", ""),
                ("damage_magic", ""),
                ("damage_fire", ""),
                ("damage_lightning", ""),
            ),
            "defense": (
                ("stat_flat", "constitution"),
                ("stat_flat", "willpower"),
                ("block_rate", ""),
                ("knockback_resistance", ""),
                ("evasion", ""),
                ("resistance_magic", ""),
            ),
            "fortune": (
                ("advanced_stat", "luck"),
                ("critical_rate", ""),
                ("life_steal", ""),
                ("accuracy", ""),
                ("evasion", ""),
            ),
        }
        kind, subject = rng.choice(pools[direction])
        affix: dict[str, object] = {"type": kind, "capacity": 1}
        if kind in {"stat_flat", "advanced_stat"}:
            affix.update(stat=subject, value=rng.randint(1, 3))
        elif kind == "skill_level":
            affix.update(
                skill_id=subject,
                value=rng.randint(1, skill_level_affix_cap(item_level)),
            )
        elif kind.startswith("resistance_"):
            affix["value"] = rng.randint(10, 50)
        elif kind in {"accuracy", "evasion"} or kind.startswith("damage_"):
            affix["value"] = rng.randint(1, 5)
        elif kind in {"armor_penetration", "life_steal"}:
            affix["value"] = round(rng.uniform(0.02, 0.08), 3)
        else:
            affix["value"] = round(rng.uniform(0.02, 0.10), 3)
        return affix

    @staticmethod
    def _dump(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> dict:
        try:
            loaded = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("重铸预览数据损坏") from exc
        if not isinstance(loaded, dict):
            raise ValueError("重铸预览数据损坏")
        return loaded


__all__ = [
    "DIRECTION_LABELS",
    "SEASON_REWORK_MODE",
    "SEASON_REWORK_TOKEN_COST",
    "STANDARD_REWORK_MODE",
    "WORKSHOP_RULESET_ID",
    "WorkshopService",
    "affix_matches_direction",
    "direction_match_score",
    "normalize_rework_direction",
    "rework_cost",
    "salvage_scrap_value",
]
