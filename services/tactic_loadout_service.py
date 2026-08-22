"""Persistence boundary for v11 three-phase tactic plans.

The combat engine owns tactic *effects* while this service only owns the
player's saved opening/midgame/endgame choices.  ``*_in_db`` methods never
commit or roll back, so callers may compose them with a larger battle or
command transaction.  The convenience methods are the transaction-owning
facade for callers which do not already have a connection.
"""

from __future__ import annotations

from typing import Final

try:
    from .db import connect_db
    from .tactic_rules import (
        FAMILY_LABELS,
        LEGACY_STRATEGY_FAMILIES,
        PHASE_LABELS,
        CombatPhase,
        TacticFamily,
        TacticPlan,
    )
    from .user_service import utc_now_text
except ImportError:
    from services.db import connect_db
    from services.tactic_rules import (
        FAMILY_LABELS,
        LEGACY_STRATEGY_FAMILIES,
        PHASE_LABELS,
        CombatPhase,
        TacticFamily,
        TacticPlan,
    )
    from services.user_service import utc_now_text


_EMPTY_ACTIVE_SLOTS_JSON: Final = "[]"


def _resolve_family(value: TacticFamily | str) -> TacticFamily:
    """Resolve a v11 family label/value or one of the eighteen v10 names."""

    if isinstance(value, TacticFamily):
        return value
    text = str(value).strip()
    legacy_family = LEGACY_STRATEGY_FAMILIES.get(text)
    if legacy_family is not None:
        return legacy_family
    lowered = text.lower()
    for family, label in FAMILY_LABELS.items():
        if lowered in {family.value, family.name.lower(), label.lower()}:
            return family
    raise ValueError(f"未知战术：{value}")


def _validated_plan(
    opening: TacticFamily | str,
    midgame: TacticFamily | str,
    endgame: TacticFamily | str,
) -> TacticPlan:
    """Validate every input before a database statement can be executed."""

    return TacticPlan(
        opening=_resolve_family(opening),
        midgame=_resolve_family(midgame),
        endgame=_resolve_family(endgame),
    )


class TacticLoadoutService:
    """Load, migrate and update a player's saved three-phase tactic plan."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def get_plan_in_db(self, db, user_pk: int) -> TacticPlan | None:
        cursor = await db.execute(
            """
            SELECT opening_family, midgame_family, endgame_family
            FROM combat_loadouts
            WHERE user_pk = ?
            """,
            (int(user_pk),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return TacticPlan(
            opening=TacticFamily(row["opening_family"]),
            midgame=TacticFamily(row["midgame_family"]),
            endgame=TacticFamily(row["endgame_family"]),
        )

    async def get_plan(self, user_pk: int) -> TacticPlan | None:
        """Return the stored plan without implicitly creating one."""

        async with await connect_db(self.db_path) as db:
            return await self.get_plan_in_db(db, user_pk)

    async def load_or_migrate_in_db(
        self,
        db,
        user_pk: int,
        legacy_strategy: str,
    ) -> TacticPlan:
        """Load a plan, atomically migrating one legacy strategy if absent.

        The primary-key conflict is intentional: concurrent first loads are
        safe and the first committed plan wins.  Repeated calls never rewrite
        an existing plan or its legacy ``active_slots_json`` payload.
        Free-text legacy tactics use ``TacticPlan.from_legacy``'s neutral
        sustain fallback because old versions allowed custom tactics.
        """

        migrated = TacticPlan.from_legacy(legacy_strategy)
        now = utc_now_text()
        cursor = await db.execute(
            """
            INSERT INTO combat_loadouts (
                user_pk, opening_family, midgame_family, endgame_family,
                active_slots_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_pk) DO NOTHING
            """,
            (
                int(user_pk),
                migrated.opening.value,
                migrated.midgame.value,
                migrated.endgame.value,
                _EMPTY_ACTIVE_SLOTS_JSON,
                now,
            ),
        )
        await cursor.close()
        stored = await self.get_plan_in_db(db, user_pk)
        if stored is None:  # pragma: no cover - protected by INSERT/PK contract
            raise RuntimeError("战术方案迁移失败")
        return stored

    async def load_or_migrate(
        self,
        user_pk: int,
        legacy_strategy: str,
    ) -> TacticPlan:
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                plan = await self.load_or_migrate_in_db(
                    db,
                    user_pk,
                    legacy_strategy,
                )
                await db.commit()
                return plan
            except Exception:
                await db.rollback()
                raise

    async def set_plan_in_db(
        self,
        db,
        user_pk: int,
        opening: TacticFamily | str,
        midgame: TacticFamily | str,
        endgame: TacticFamily | str,
    ) -> TacticPlan:
        """Validate and upsert a plan without owning the caller's transaction.

        ``active_slots_json`` is omitted from the conflict update on purpose:
        v10/v11 active ability slot data remains byte-for-byte untouched.
        """

        plan = _validated_plan(opening, midgame, endgame)
        now = utc_now_text()
        cursor = await db.execute(
            """
            INSERT INTO combat_loadouts (
                user_pk, opening_family, midgame_family, endgame_family,
                active_slots_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_pk) DO UPDATE SET
                opening_family = excluded.opening_family,
                midgame_family = excluded.midgame_family,
                endgame_family = excluded.endgame_family,
                updated_at = excluded.updated_at
            WHERE combat_loadouts.opening_family <> excluded.opening_family
               OR combat_loadouts.midgame_family <> excluded.midgame_family
               OR combat_loadouts.endgame_family <> excluded.endgame_family
            """,
            (
                int(user_pk),
                plan.opening.value,
                plan.midgame.value,
                plan.endgame.value,
                _EMPTY_ACTIVE_SLOTS_JSON,
                now,
            ),
        )
        await cursor.close()
        return plan

    async def set_plan(
        self,
        user_pk: int,
        opening: TacticFamily | str,
        midgame: TacticFamily | str,
        endgame: TacticFamily | str,
    ) -> TacticPlan:
        # Validate before opening a write transaction.  The in-db method still
        # repeats this boundary for transaction-composing callers.
        plan = _validated_plan(opening, midgame, endgame)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN")
                await self.set_plan_in_db(
                    db,
                    user_pk,
                    plan.opening,
                    plan.midgame,
                    plan.endgame,
                )
                await db.commit()
                return plan
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def format_plan(plan: TacticPlan) -> str:
        """Format a compact, player-facing Chinese phase summary."""

        return "｜".join(
            (
                f"{PHASE_LABELS[CombatPhase.OPENING]}："
                f"{FAMILY_LABELS[plan.opening]}",
                f"{PHASE_LABELS[CombatPhase.MIDGAME]}："
                f"{FAMILY_LABELS[plan.midgame]}",
                f"{PHASE_LABELS[CombatPhase.ENDGAME]}："
                f"{FAMILY_LABELS[plan.endgame]}",
            )
        )


__all__ = ["TacticLoadoutService"]
