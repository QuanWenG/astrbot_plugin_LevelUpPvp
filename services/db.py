import json
import os
import sqlite3

try:
    from .progression_rules import (
        LEGACY_RULESET_ID,
        attribute_exp_required,
        clamp_potential,
        clamp_skill_potential,
        legacy_attribute_exp_required,
        legacy_skill_exp_required,
        legacy_spell_exp_required,
        migrate_exp_preserving_progress,
        migrate_level_exp_preserving_progress,
        migrate_v10_skill_exp_preserving_progress,
        skill_exp_required,
        spell_exp_required,
        v10_skill_exp_required,
    )
except ImportError:
    from services.progression_rules import (
        LEGACY_RULESET_ID,
        attribute_exp_required,
        clamp_potential,
        clamp_skill_potential,
        legacy_attribute_exp_required,
        legacy_skill_exp_required,
        legacy_spell_exp_required,
        migrate_exp_preserving_progress,
        migrate_level_exp_preserving_progress,
        migrate_v10_skill_exp_preserving_progress,
        skill_exp_required,
        spell_exp_required,
        v10_skill_exp_required,
    )

try:
    import aiosqlite
except ImportError:
    aiosqlite = None


PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION = "2026-07-primary-attributes-v2"
PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX = ".pre-primary-attributes-v2.bak"
ELONA_BALANCE_MIGRATION = "2026-07-elona-balance-v1"
ELONA_BALANCE_BACKUP_SUFFIX = ".pre-elona-balance-v1.bak"
CLASSIC_BLACK_STAR_LEVEL_MIGRATION = "2026-07-classic-black-stars-level-40-v1"
CLASSIC_BLACK_STAR_EFFECTS_MIGRATION = "2026-07-classic-black-stars-effects-v2"
ELONA_PROGRESSION_MIGRATION = "elona-progression-v2"
ELONA_PROGRESSION_BACKUP_SUFFIX = ".pre-elona-progression-v2.bak"
V11_PROGRESSION_MIGRATION = "2026-08-qq-daily-budget-v11"
V11_PROGRESSION_BACKUP_SUFFIX = ".pre-qq-daily-budget-v11.bak"
CLASSIC_BLACK_STAR_TEMPLATE_IDS = (
    "black_star_ether_dagger",
    "black_star_lucky_dagger",
    "black_star_claymore",
    "black_star_diabolos",
    "black_star_zantetsu",
    "black_star_ragnarok",
    "black_star_rankis",
    "black_star_holy_lance",
    "black_star_axe_of_destruction",
    "black_star_void_scythe",
    "black_star_kumiromi_scythe",
    "black_star_hammer_of_earth",
    "black_star_elemental_staff",
    "black_star_bow_of_vindale",
    "black_star_wind_bow",
    "black_star_winchester_premium",
    "black_star_rail_gun",
    "black_star_sage_helm",
    "black_star_aurora_ring_black_star",
    "black_star_seven_league_boots_black_star",
)


class _AsyncSQLiteCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()

    async def close(self) -> None:
        self._cursor.close()


class _AsyncSQLiteConnection:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.close()

    async def execute(self, sql: str, parameters=()):
        return _AsyncSQLiteCursor(self._conn.execute(sql, parameters))

    async def executescript(self, sql_script: str) -> None:
        self._conn.executescript(sql_script)

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()


class _AioSQLiteConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        await self._conn.close()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


async def connect_db(db_path: str):
    if aiosqlite is None:
        db = _AsyncSQLiteConnection(db_path)
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = MEMORY")
        return db

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = MEMORY")
    return _AioSQLiteConnectionWrapper(db)


async def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    async with await connect_db(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 1,
                exp INTEGER NOT NULL DEFAULT 0,
                total_exp INTEGER NOT NULL DEFAULT 0,
                stat_points INTEGER NOT NULL DEFAULT 0,
                skill_points INTEGER NOT NULL DEFAULT 0,
                level_up_count INTEGER NOT NULL DEFAULT 0,
                hp INTEGER NOT NULL DEFAULT 1,
                atk INTEGER NOT NULL DEFAULT 1,
                defense INTEGER NOT NULL DEFAULT 1,
                speed INTEGER NOT NULL DEFAULT 1,
                luck INTEGER NOT NULL DEFAULT 1,
                willpower INTEGER NOT NULL DEFAULT 1,
                life_growth INTEGER NOT NULL DEFAULT 100,
                mana_growth INTEGER NOT NULL DEFAULT 100,
                advanced_speed INTEGER NOT NULL DEFAULT 100,
                advanced_luck INTEGER NOT NULL DEFAULT 100,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                checkin_date TEXT NOT NULL,
                streak_days INTEGER NOT NULL,
                exp_gain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_pk, checkin_date),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS battles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL DEFAULT '',
                attacker_pk INTEGER NOT NULL,
                defender_pk INTEGER NOT NULL,
                winner_pk INTEGER NOT NULL,
                loser_pk INTEGER NOT NULL,
                attacker_win_rate REAL NOT NULL,
                roll_value REAL NOT NULL,
                strategy TEXT NOT NULL DEFAULT '',
                winner_exp_gain INTEGER NOT NULL,
                loser_exp_gain INTEGER NOT NULL DEFAULT 0,
                loser_exp_loss INTEGER NOT NULL,
                analysis TEXT NOT NULL DEFAULT '',
                battle_log TEXT NOT NULL DEFAULT '[]',
                llm_raw_result TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'local',
                is_counterattack INTEGER NOT NULL DEFAULT 0,
                countered_battle_id INTEGER,
                battle_mode TEXT NOT NULL DEFAULT 'probability',
                engine_version TEXT NOT NULL DEFAULT 'legacy-v1',
                random_seed INTEGER,
                duration_ticks INTEGER NOT NULL DEFAULT 0,
                finish_reason TEXT NOT NULL DEFAULT '',
                simulation_json TEXT NOT NULL DEFAULT '{}',
                ruleset_id TEXT NOT NULL DEFAULT 'legacy-v1',
                environment_id TEXT NOT NULL DEFAULT '',
                attacker_rating_before REAL,
                attacker_rating_after REAL,
                defender_rating_before REAL,
                defender_rating_after REAL,
                rated INTEGER NOT NULL DEFAULT 0 CHECK(rated IN (0, 1)),
                reward_reason TEXT NOT NULL DEFAULT '',
                attacker_tactic_plan_json TEXT NOT NULL DEFAULT '{}',
                defender_tactic_plan_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL,
                FOREIGN KEY(attacker_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(defender_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(winner_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(loser_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(countered_battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS combat_states (
                user_pk INTEGER PRIMARY KEY,
                hp_ratio REAL NOT NULL DEFAULT 1,
                mana_ratio REAL NOT NULL DEFAULT 1,
                stamina_ratio REAL NOT NULL DEFAULT 1,
                hp_regen_buffer REAL NOT NULL DEFAULT 0,
                mp_regen_buffer REAL NOT NULL DEFAULT 0,
                sp_regen_buffer REAL NOT NULL DEFAULT 0,
                recovery_turn_phase INTEGER NOT NULL DEFAULT 0,
                statuses_json TEXT NOT NULL DEFAULT '[]',
                skill_cooldowns_json TEXT NOT NULL DEFAULT '{}',
                attack_cooldown INTEGER NOT NULL DEFAULT 0,
                recovery_ticks INTEGER NOT NULL DEFAULT 0,
                hitstun_ticks INTEGER NOT NULL DEFAULT 0,
                counter_cooldown INTEGER NOT NULL DEFAULT 0,
                hard_control_immunity_ticks INTEGER NOT NULL DEFAULT 0,
                stance_id TEXT,
                frozen_mana_ratio REAL NOT NULL DEFAULT 0,
                frozen_mana_capacity_ratio REAL NOT NULL DEFAULT 0,
                lethal_survival_used INTEGER NOT NULL DEFAULT 0,
                defeated INTEGER NOT NULL DEFAULT 0,
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS level_up_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL,
                auto_growth_json TEXT NOT NULL,
                stat_points_gain INTEGER NOT NULL,
                skill_points_gain INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS level_freezes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                frozen_level INTEGER NOT NULL,
                from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL,
                frozen_stats_json TEXT NOT NULL,
                frozen_stat_points INTEGER NOT NULL DEFAULT 0,
                frozen_skill_points INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'frozen',
                created_at TEXT NOT NULL,
                released_at TEXT,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS stat_point_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                stat_name TEXT NOT NULL,
                points_spent INTEGER NOT NULL,
                rolls_json TEXT NOT NULL,
                total_gain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS nickname_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS equipment_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner_pk INTEGER NOT NULL,
                template_id TEXT NOT NULL, name TEXT NOT NULL, item_type TEXT NOT NULL,
                equip_slot TEXT NOT NULL, hand_mode TEXT NOT NULL DEFAULT 'none',
                weapon_type TEXT NOT NULL DEFAULT '', armor_type TEXT NOT NULL DEFAULT '',
                item_level INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'common',
                star_type TEXT NOT NULL DEFAULT 'none', material TEXT NOT NULL DEFAULT 'iron',
                blessing_state TEXT NOT NULL DEFAULT 'normal', enhancement_level INTEGER NOT NULL DEFAULT 0,
                weight REAL NOT NULL DEFAULT 0, enchant_capacity INTEGER NOT NULL DEFAULT 0,
                used_capacity INTEGER NOT NULL DEFAULT 0, base_stats_json TEXT NOT NULL DEFAULT '{}',
                inherent_affixes_json TEXT NOT NULL DEFAULT '[]', random_affixes_json TEXT NOT NULL DEFAULT '[]',
                fusion_affixes_json TEXT NOT NULL DEFAULT '[]', bound INTEGER NOT NULL DEFAULT 1,
                description TEXT NOT NULL DEFAULT '',
                source_effects_json TEXT NOT NULL DEFAULT '[]',
                is_locked INTEGER NOT NULL DEFAULT 0 CHECK(is_locked IN (0, 1)),
                created_at TEXT NOT NULL, FOREIGN KEY(owner_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS equipment_loadout (
                user_pk INTEGER NOT NULL, slot TEXT NOT NULL, equipment_id INTEGER NOT NULL,
                PRIMARY KEY(user_pk, slot), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(equipment_id) REFERENCES equipment_items(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_skills (
                user_pk INTEGER NOT NULL, skill_id TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1,
                exp INTEGER NOT NULL DEFAULT 0, potential INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY(user_pk, skill_id), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS active_skill_slots (
                user_pk INTEGER NOT NULL, slot INTEGER NOT NULL, skill_id TEXT NOT NULL,
                PRIMARY KEY(user_pk, slot), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS skill_growth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_pk INTEGER NOT NULL, battle_id INTEGER,
                skill_id TEXT NOT NULL, exp_gain INTEGER NOT NULL, from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL, potential_before INTEGER NOT NULL, potential_after INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                rules_version TEXT NOT NULL DEFAULT 'elona-scaled-v2',
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS feature_grants (
                user_pk INTEGER NOT NULL, grant_key TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(user_pk, grant_key), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spellbook_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_pk INTEGER NOT NULL, spell_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'internal',
                random_seed INTEGER NOT NULL, bound INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spell_research_balances (
                user_pk INTEGER PRIMARY KEY,
                pages INTEGER NOT NULL DEFAULT 0 CHECK(pages >= 0),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spell_research_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                spell_id TEXT NOT NULL DEFAULT '',
                school_id TEXT NOT NULL DEFAULT '',
                delta INTEGER NOT NULL,
                balance_after INTEGER NOT NULL CHECK(balance_after >= 0),
                reason TEXT NOT NULL,
                operation_key TEXT NOT NULL UNIQUE,
                source_book_id INTEGER,
                source_seed INTEGER,
                result_book_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_spells (
                user_pk INTEGER NOT NULL, spell_id TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
                potential INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY(user_pk, spell_id),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spell_read_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL, spell_id TEXT NOT NULL,
                book_item_id INTEGER NOT NULL, success INTEGER NOT NULL,
                success_chance REAL NOT NULL, random_seed INTEGER NOT NULL,
                potential_before INTEGER NOT NULL DEFAULT 0,
                potential_after INTEGER NOT NULL DEFAULT 0,
                reading_difficulty INTEGER NOT NULL DEFAULT 0,
                reading_power REAL NOT NULL DEFAULT 0,
                reading_attribute TEXT NOT NULL DEFAULT '',
                activity_day_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spell_growth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL, battle_id INTEGER,
                spell_id TEXT NOT NULL, exp_gain INTEGER NOT NULL,
                from_level INTEGER NOT NULL, to_level INTEGER NOT NULL,
                potential_before INTEGER NOT NULL,
                potential_after INTEGER NOT NULL, created_at TEXT NOT NULL,
                rules_version TEXT NOT NULL DEFAULT 'elona-scaled-v2',
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS advanced_attribute_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL, attribute_id TEXT NOT NULL,
                amount INTEGER NOT NULL, value_before INTEGER NOT NULL,
                value_after INTEGER NOT NULL, source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_attribute_progress (
                user_pk INTEGER NOT NULL, attribute_id TEXT NOT NULL,
                exp INTEGER NOT NULL DEFAULT 0, potential INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY(user_pk, attribute_id),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS attribute_growth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_pk INTEGER NOT NULL,
                battle_id INTEGER, attribute_id TEXT NOT NULL, exp_gain INTEGER NOT NULL,
                from_value INTEGER NOT NULL, to_value INTEGER NOT NULL,
                potential_before INTEGER NOT NULL, potential_after INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                rules_version TEXT NOT NULL DEFAULT 'elona-scaled-v2',
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS external_activity_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                source TEXT NOT NULL,
                reward_key TEXT NOT NULL,
                component TEXT NOT NULL CHECK(component IN ('attempt', 'correct')),
                level_exp_gain INTEGER NOT NULL DEFAULT 0,
                perception_exp_gain INTEGER NOT NULL DEFAULT 0,
                magic_exp_gain INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(user_pk, source, reward_key, component),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS dungeon_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                dungeon_id TEXT NOT NULL,
                cleared INTEGER NOT NULL DEFAULT 0,
                monsters_killed INTEGER NOT NULL DEFAULT 0,
                total_monsters INTEGER NOT NULL DEFAULT 0,
                exp_gain INTEGER NOT NULL DEFAULT 0,
                rewards_json TEXT NOT NULL DEFAULT '[]',
                strategy TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dungeon_adventures (
                adventure_id TEXT PRIMARY KEY,
                owner_pk INTEGER NOT NULL,
                owner_key TEXT NOT NULL,
                group_key TEXT NOT NULL,
                dungeon_id TEXT NOT NULL,
                cycle_key TEXT NOT NULL,
                phase TEXT NOT NULL,
                floor_index INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                snapshot_json TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL,
                updated_at_ts INTEGER NOT NULL,
                UNIQUE(owner_key, dungeon_id, cycle_key),
                FOREIGN KEY(owner_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS combat_loadouts (
                user_pk INTEGER PRIMARY KEY,
                opening_family TEXT NOT NULL DEFAULT 'sustain',
                midgame_family TEXT NOT NULL DEFAULT 'sustain',
                endgame_family TEXT NOT NULL DEFAULT 'sustain',
                active_slots_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS auto_pilot_state (
                user_pk INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                origin_umo TEXT NOT NULL DEFAULT '',
                origin_group_id TEXT NOT NULL DEFAULT '',
                started_at_ts INTEGER NOT NULL DEFAULT 0,
                last_tick_ts INTEGER NOT NULL DEFAULT 0,
                next_tick_ts INTEGER NOT NULL DEFAULT 0,
                cursor_json TEXT NOT NULL DEFAULT '{}',
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_auto_pilot_due
                ON auto_pilot_state(enabled, next_tick_ts, user_pk);

            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL DEFAULT '',
                season_key TEXT NOT NULL,
                ruleset_id TEXT NOT NULL,
                start_at_ts INTEGER NOT NULL,
                end_at_ts INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(group_id, season_key)
            );

            CREATE TABLE IF NOT EXISTS season_users (
                season_id INTEGER NOT NULL,
                user_pk INTEGER NOT NULL,
                rating REAL NOT NULL DEFAULT 1000,
                games INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                provisional_games INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(season_id, user_pk),
                FOREIGN KEY(season_id) REFERENCES seasons(id) ON DELETE CASCADE,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reward_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reward_key TEXT NOT NULL UNIQUE,
                user_pk INTEGER NOT NULL,
                battle_id INTEGER,
                source TEXT NOT NULL,
                exp_gain INTEGER NOT NULL DEFAULT 0,
                currency_gain INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                created_at_ts INTEGER NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS operation_progress (
                user_pk INTEGER NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                period_kind TEXT NOT NULL,
                period_key TEXT NOT NULL,
                operation_key TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                target INTEGER NOT NULL DEFAULT 1,
                completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)),
                claimed INTEGER NOT NULL DEFAULT 0 CHECK(claimed IN (0, 1)),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(
                    user_pk, group_id, period_kind, period_key, operation_key
                ),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS loot_pity (
                user_pk INTEGER NOT NULL,
                pool_id TEXT NOT NULL,
                epic_misses INTEGER NOT NULL DEFAULT 0,
                legendary_misses INTEGER NOT NULL DEFAULT 0,
                total_draws INTEGER NOT NULL DEFAULT 0,
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_pk, pool_id),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS equipment_rework_state (
                equipment_id INTEGER NOT NULL,
                ruleset_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                original_snapshot_json TEXT NOT NULL DEFAULT '{}',
                reworked_snapshot_json TEXT NOT NULL DEFAULT '{}',
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(equipment_id, ruleset_id),
                FOREIGN KEY(equipment_id) REFERENCES equipment_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS workshop_wallet (
                user_pk INTEGER PRIMARY KEY,
                scrap_balance INTEGER NOT NULL DEFAULT 0,
                season_tokens INTEGER NOT NULL DEFAULT 0,
                lifetime_earned INTEGER NOT NULL DEFAULT 0,
                lifetime_spent INTEGER NOT NULL DEFAULT 0,
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_activity_daily (
                user_pk INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                day_key TEXT NOT NULL,
                valid_messages INTEGER NOT NULL DEFAULT 0,
                reward_rolls INTEGER NOT NULL DEFAULT 0,
                exp_events INTEGER NOT NULL DEFAULT 0,
                reserved_exp INTEGER NOT NULL DEFAULT 0,
                awarded_exp INTEGER NOT NULL DEFAULT 0,
                equipment_drops INTEGER NOT NULL DEFAULT 0,
                spellbook_drops INTEGER NOT NULL DEFAULT 0,
                last_reward_roll_ts INTEGER NOT NULL DEFAULT 0,
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_pk, group_id, day_key),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_activity_pity (
                user_pk INTEGER PRIMARY KEY,
                equipment_misses INTEGER NOT NULL DEFAULT 0,
                spellbook_misses INTEGER NOT NULL DEFAULT 0,
                equipment_drops_total INTEGER NOT NULL DEFAULT 0,
                spellbook_drops_total INTEGER NOT NULL DEFAULT 0,
                updated_at_ts INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_activity_events (
                event_key TEXT PRIMARY KEY,
                user_pk INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                occurred_at_ts INTEGER NOT NULL,
                content_fingerprint TEXT NOT NULL DEFAULT '',
                accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0, 1)),
                decision_reason TEXT NOT NULL DEFAULT '',
                day_key TEXT NOT NULL DEFAULT '',
                valid_message_index INTEGER,
                reward_roll_index INTEGER,
                reward_key TEXT NOT NULL DEFAULT '',
                intent_json TEXT NOT NULL DEFAULT '{}',
                equipment_probability REAL NOT NULL DEFAULT 0,
                spellbook_probability REAL NOT NULL DEFAULT 0,
                settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0, 1)),
                actual_exp INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_dungeon_runs_user
                ON dungeon_runs(user_pk);
            CREATE INDEX IF NOT EXISTS idx_dungeon_adventures_owner_cycle
                ON dungeon_adventures(owner_pk, cycle_key, dungeon_id);
            CREATE INDEX IF NOT EXISTS idx_dungeon_adventures_group_cycle
                ON dungeon_adventures(group_key, cycle_key, dungeon_id);
            CREATE INDEX IF NOT EXISTS idx_checkins_user_date
                ON checkins(user_pk, checkin_date);
            CREATE INDEX IF NOT EXISTS idx_battles_attacker
                ON battles(attacker_pk);
            CREATE INDEX IF NOT EXISTS idx_battles_defender
                ON battles(defender_pk);
            CREATE INDEX IF NOT EXISTS idx_battles_created_at_ts
                ON battles(created_at_ts);
            CREATE INDEX IF NOT EXISTS idx_level_up_logs_user
                ON level_up_logs(user_pk);
            CREATE INDEX IF NOT EXISTS idx_level_freezes_user_status
                ON level_freezes(user_pk, status, frozen_level);
            CREATE INDEX IF NOT EXISTS idx_stat_point_logs_user
                ON stat_point_logs(user_pk);
            CREATE INDEX IF NOT EXISTS idx_nickname_mappings_lookup
                ON nickname_mappings(platform, group_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_equipment_owner_template
                ON equipment_items(owner_pk, template_id);
            CREATE INDEX IF NOT EXISTS idx_advanced_attribute_logs_user
                ON advanced_attribute_logs(user_pk, created_at);
            CREATE INDEX IF NOT EXISTS idx_attribute_growth_user
                ON attribute_growth_logs(user_pk, battle_id);
            CREATE INDEX IF NOT EXISTS idx_external_activity_reward_lookup
                ON external_activity_rewards(user_pk, source, reward_key);
            CREATE INDEX IF NOT EXISTS idx_skill_growth_user
                ON skill_growth_logs(user_pk, battle_id);
            CREATE INDEX IF NOT EXISTS idx_spellbooks_owner
                ON spellbook_items(owner_pk, spell_id);
            CREATE INDEX IF NOT EXISTS idx_spell_research_user_created
                ON spell_research_logs(user_pk, id);
            CREATE INDEX IF NOT EXISTS idx_spell_growth_user
                ON spell_growth_logs(user_pk, battle_id);
            CREATE INDEX IF NOT EXISTS idx_seasons_group_status
                ON seasons(group_id, status, start_at_ts, end_at_ts);
            CREATE INDEX IF NOT EXISTS idx_season_users_rating
                ON season_users(season_id, rating DESC);
            CREATE INDEX IF NOT EXISTS idx_reward_ledger_user_created
                ON reward_ledger(user_pk, created_at_ts);
            CREATE INDEX IF NOT EXISTS idx_reward_ledger_battle
                ON reward_ledger(battle_id);
            CREATE INDEX IF NOT EXISTS idx_operation_progress_period
                ON operation_progress(group_id, period_kind, period_key);
            CREATE INDEX IF NOT EXISTS idx_equipment_rework_status
                ON equipment_rework_state(ruleset_id, status);
            CREATE INDEX IF NOT EXISTS idx_chat_activity_events_antispam
                ON chat_activity_events(
                    user_pk, group_id, occurred_at_ts, accepted
                );
            CREATE INDEX IF NOT EXISTS idx_chat_activity_events_fingerprint
                ON chat_activity_events(
                    user_pk, group_id, content_fingerprint, occurred_at_ts
                );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_activity_reward_key
                ON chat_activity_events(reward_key)
                WHERE reward_key <> '';
            CREATE INDEX IF NOT EXISTS idx_chat_activity_pending
                ON chat_activity_events(settled, occurred_at_ts)
                WHERE reward_key <> '';
            """
        )
        await _ensure_column(
            db,
            "battles",
            "is_counterattack",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(db, "battles", "countered_battle_id", "INTEGER")
        await _ensure_column(
            db, "battles", "battle_mode", "TEXT NOT NULL DEFAULT 'probability'"
        )
        await _ensure_column(
            db, "battles", "engine_version", "TEXT NOT NULL DEFAULT 'legacy-v1'"
        )
        await _ensure_column(db, "battles", "random_seed", "INTEGER")
        await _ensure_column(
            db, "battles", "duration_ticks", "INTEGER NOT NULL DEFAULT 0"
        )
        await _ensure_column(
            db, "battles", "finish_reason", "TEXT NOT NULL DEFAULT ''"
        )
        await _ensure_column(
            db, "battles", "simulation_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        await _ensure_column(
            db, "battles", "loser_exp_gain", "INTEGER NOT NULL DEFAULT 0"
        )
        await _ensure_column(
            db, "battles", "ruleset_id", "TEXT NOT NULL DEFAULT 'legacy-v1'"
        )
        await _ensure_column(
            db, "battles", "environment_id", "TEXT NOT NULL DEFAULT ''"
        )
        await _ensure_column(db, "battles", "attacker_rating_before", "REAL")
        await _ensure_column(db, "battles", "attacker_rating_after", "REAL")
        await _ensure_column(db, "battles", "defender_rating_before", "REAL")
        await _ensure_column(db, "battles", "defender_rating_after", "REAL")
        await _ensure_column(
            db, "battles", "rated", "INTEGER NOT NULL DEFAULT 0 CHECK(rated IN (0, 1))"
        )
        await _ensure_column(
            db, "battles", "reward_reason", "TEXT NOT NULL DEFAULT ''"
        )
        await _ensure_column(
            db,
            "battles",
            "attacker_tactic_plan_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        await _ensure_column(
            db,
            "battles",
            "defender_tactic_plan_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        await _ensure_column(
            db,
            "workshop_wallet",
            "season_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(db, "spell_read_logs", "reading_difficulty", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "spell_read_logs", "reading_power", "REAL NOT NULL DEFAULT 0")
        await _ensure_column(db, "spell_read_logs", "reading_attribute", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "spell_read_logs", "activity_day_key", "TEXT NOT NULL DEFAULT ''")
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_spell_read_progress
                ON spell_read_logs(user_pk, spell_id, success, id)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_spell_read_daily_failure
                ON spell_read_logs(user_pk, spell_id, activity_day_key)
                WHERE success = 0
            """
        )
        await _ensure_column(db, "users", "life_growth", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "mana_growth", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "advanced_speed", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "advanced_luck", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "skill_points", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(
            db,
            "combat_states",
            "recovery_turn_phase",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(
            db,
            "combat_states",
            "hard_control_immunity_ticks",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(db, "users", "willpower", "INTEGER NOT NULL DEFAULT 1")
        await _ensure_column(
            db,
            "equipment_items",
            "description",
            "TEXT NOT NULL DEFAULT ''",
        )
        await _ensure_column(
            db,
            "equipment_items",
            "source_effects_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        await _ensure_column(
            db,
            "equipment_items",
            "is_locked",
            "INTEGER NOT NULL DEFAULT 0 CHECK(is_locked IN (0, 1))",
        )
        await _ensure_column(db, "level_up_logs", "skill_points_gain", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "level_freezes", "frozen_skill_points", "INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_battles_countered
                ON battles(countered_battle_id)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_battles_ruleset_created
                ON battles(ruleset_id, created_at_ts)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_battles_environment_created
                ON battles(environment_id, created_at_ts)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_battles_rated_created
                ON battles(rated, created_at_ts)
            """
        )
        await db.commit()
        await _apply_primary_attribute_rebalance(db, db_path)
        await _apply_elona_balance_reset(db, db_path)
        await _apply_classic_black_star_level_migration(db)
        await _apply_classic_black_star_effects_migration(db)
        await _apply_elona_progression_migration(db, db_path)
        await _apply_v11_progression_migration(db, db_path)


async def _ensure_column(db, table_name: str, column_name: str, definition: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cursor.fetchall()
    await cursor.close()
    if any(row["name"] == column_name for row in rows):
        return
    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _backup_database_once(db_path: str) -> str | None:
    return _backup_database_with_suffix(
        db_path, PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX
    )


def _backup_database_with_suffix(
    db_path: str, suffix: str
) -> str | None:
    if db_path == ":memory:" or not os.path.isfile(db_path):
        return None
    backup_path = db_path + suffix
    if os.path.exists(backup_path):
        return backup_path
    temporary_path = backup_path + ".tmp"
    if os.path.exists(temporary_path):
        os.remove(temporary_path)
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
        destination.close()
        source.close()
        os.replace(temporary_path, backup_path)
        return backup_path
    except Exception:
        destination.close()
        source.close()
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


async def _apply_primary_attribute_rebalance(db, db_path: str) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    cursor = await db.execute(
        "SELECT COUNT(*) AS count FROM user_attribute_progress"
    )
    modern_progress_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if modern_progress_count:
        await db.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,),
        )
        await db.commit()
        return

    cursor = await db.execute("SELECT COUNT(*) AS count FROM users")
    user_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if user_count:
        _backup_database_once(db_path)

    await db.execute("BEGIN")
    try:
        await db.execute(
            """
            UPDATE users
            SET hp = 1, atk = 1, defense = 1,
                speed = 1, luck = 1, willpower = 1,
                stat_points = MAX(0, level - 1),
                life_growth = 100, mana_growth = 100,
                advanced_speed = 100, advanced_luck = 100
            """
        )
        await db.execute(
            """
            UPDATE user_attribute_progress
            SET exp = 0, potential = 100
            """
        )
        await db.execute(
            """
            UPDATE level_freezes
            SET frozen_stats_json = '{}', frozen_stat_points = 1
            WHERE status = 'frozen'
            """
        )
        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _apply_elona_balance_reset(db, db_path: str) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (ELONA_BALANCE_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    cursor = await db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM user_skills)
          + (SELECT COUNT(*) FROM user_spells)
          + (SELECT COUNT(*) FROM equipment_items) AS count
        """
    )
    modern_progress_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if modern_progress_count:
        await db.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (ELONA_BALANCE_MIGRATION,),
        )
        await db.commit()
        return

    cursor = await db.execute("SELECT COUNT(*) AS count FROM users")
    user_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if user_count:
        _backup_database_with_suffix(
            db_path, ELONA_BALANCE_BACKUP_SUFFIX
        )

    await db.execute("BEGIN")
    try:
        await db.execute(
            """
            UPDATE users
            SET hp = 1, atk = 1, defense = 1,
                speed = 1, luck = 1, willpower = 1,
                stat_points = MAX(0, level - 1),
                skill_points = MAX(0, level - 1),
                life_growth = 100, mana_growth = 100,
                advanced_speed = 100, advanced_luck = 100
            """
        )
        await db.execute(
            """
            UPDATE user_attribute_progress
            SET exp = 0, potential = 100
            """
        )
        await db.execute(
            """
            UPDATE level_freezes
            SET status = 'released',
                released_at = COALESCE(released_at, datetime('now'))
            WHERE status = 'frozen'
            """
        )
        await db.execute("DELETE FROM active_skill_slots")
        await db.execute("DELETE FROM user_skills")
        await db.execute("DELETE FROM user_spells")
        await db.execute("DELETE FROM spellbook_items")
        await db.execute("DELETE FROM equipment_loadout")
        await db.execute("DELETE FROM equipment_items")
        await db.execute(
            """
            DELETE FROM feature_grants
            WHERE grant_key IN (
                'skills-v1',
                'starter-armory-v1',
                'starter-armory-v2-materials'
            )
            """
        )
        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (ELONA_BALANCE_MIGRATION,),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _apply_elona_progression_migration(db, db_path: str) -> None:
    cursor = await db.execute(
        """
        SELECT migration_id
        FROM schema_migrations
        WHERE migration_id IN (?, ?)
        """,
        (ELONA_PROGRESSION_MIGRATION, V11_PROGRESSION_MIGRATION),
    )
    applied_migrations = {
        str(row["migration_id"])
        for row in await cursor.fetchall()
    }
    await cursor.close()
    if ELONA_PROGRESSION_MIGRATION in applied_migrations:
        return

    # A later migration marker proves that this database has already crossed
    # the v2 progression boundary.  Replaying the predecessor after its marker
    # was lost would reinterpret current v11 skill XP as legacy XP and convert
    # it a second time.  Heal the missing historical marker without touching
    # player data so repeated startup remains idempotent.
    if V11_PROGRESSION_MIGRATION in applied_migrations:
        await db.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (ELONA_PROGRESSION_MIGRATION,),
        )
        await db.commit()
        return

    cursor = await db.execute("SELECT COUNT(*) AS count FROM users")
    user_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if user_count:
        backup_path = _backup_database_with_suffix(
            db_path, ELONA_PROGRESSION_BACKUP_SUFFIX
        )
        if db_path != ":memory:" and backup_path is None:
            raise RuntimeError("无法创建成长系统迁移备份")

    await db.execute("BEGIN")
    try:
        for table_name in (
            "attribute_growth_logs",
            "skill_growth_logs",
            "spell_growth_logs",
        ):
            await _ensure_column(
                db,
                table_name,
                "rules_version",
                f"TEXT NOT NULL DEFAULT '{LEGACY_RULESET_ID}'",
            )

        cursor = await db.execute(
            """
            SELECT p.user_pk, p.attribute_id, p.exp, p.potential,
                   u.hp, u.defense, u.speed, u.atk, u.luck, u.willpower
            FROM user_attribute_progress AS p
            JOIN users AS u ON u.id = p.user_pk
            """
        )
        attribute_rows = await cursor.fetchall()
        await cursor.close()
        attribute_columns = {
            "strength": "hp",
            "constitution": "defense",
            "dexterity": "speed",
            "perception": "atk",
            "magic": "luck",
            "willpower": "willpower",
        }
        for row in attribute_rows:
            attribute_id = row["attribute_id"]
            if attribute_id not in attribute_columns:
                continue
            value = int(row[attribute_columns[attribute_id]])
            converted = migrate_exp_preserving_progress(
                int(row["exp"]),
                legacy_attribute_exp_required(value),
                attribute_exp_required(value),
            )
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = ?, potential = ?
                WHERE user_pk = ? AND attribute_id = ?
                """,
                (
                    converted,
                    clamp_potential(int(row["potential"])),
                    int(row["user_pk"]),
                    attribute_id,
                ),
            )

        cursor = await db.execute(
            "SELECT user_pk, skill_id, level, exp, potential FROM user_skills"
        )
        skill_rows = await cursor.fetchall()
        await cursor.close()
        for row in skill_rows:
            level = int(row["level"])
            converted = migrate_exp_preserving_progress(
                int(row["exp"]),
                legacy_skill_exp_required(level),
                v10_skill_exp_required(level),
            )
            await db.execute(
                """
                UPDATE user_skills
                SET exp = ?, potential = ?
                WHERE user_pk = ? AND skill_id = ?
                """,
                (
                    converted,
                    clamp_potential(int(row["potential"])),
                    int(row["user_pk"]),
                    row["skill_id"],
                ),
            )

        cursor = await db.execute(
            "SELECT user_pk, spell_id, level, exp, potential FROM user_spells"
        )
        spell_rows = await cursor.fetchall()
        await cursor.close()
        for row in spell_rows:
            level = int(row["level"])
            converted = migrate_exp_preserving_progress(
                int(row["exp"]),
                legacy_spell_exp_required(level),
                spell_exp_required(level),
            )
            await db.execute(
                """
                UPDATE user_spells
                SET exp = ?, potential = ?
                WHERE user_pk = ? AND spell_id = ?
                """,
                (
                    converted,
                    clamp_potential(int(row["potential"])),
                    int(row["user_pk"]),
                    row["spell_id"],
                ),
            )

        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (ELONA_PROGRESSION_MIGRATION,),
        )
        cursor = await db.execute("PRAGMA quick_check")
        integrity = await cursor.fetchone()
        await cursor.close()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("成长系统迁移后的数据库完整性检查失败")
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _apply_v11_progression_migration(db, db_path: str) -> None:
    """Preserve level and current-bar percentage while adopting v11 pacing."""
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (V11_PROGRESSION_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    cursor = await db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users)
          + (SELECT COUNT(*) FROM user_skills) AS count
        """
    )
    row_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if row_count:
        backup_path = _backup_database_with_suffix(
            db_path,
            V11_PROGRESSION_BACKUP_SUFFIX,
        )
        if db_path != ":memory:" and backup_path is None:
            raise RuntimeError("无法创建 v11 成长系统迁移备份")

    await db.execute("BEGIN")
    try:
        cursor = await db.execute("SELECT id, level, exp FROM users")
        users = await cursor.fetchall()
        await cursor.close()
        for row in users:
            await db.execute(
                "UPDATE users SET exp = ? WHERE id = ?",
                (
                    migrate_level_exp_preserving_progress(
                        int(row["level"]),
                        int(row["exp"]),
                    ),
                    int(row["id"]),
                ),
            )

        cursor = await db.execute(
            "SELECT user_pk, skill_id, level, exp, potential FROM user_skills"
        )
        skills = await cursor.fetchall()
        await cursor.close()
        for row in skills:
            await db.execute(
                """
                UPDATE user_skills
                SET exp = ?, potential = ?
                WHERE user_pk = ? AND skill_id = ?
                """,
                (
                    migrate_v10_skill_exp_preserving_progress(
                        int(row["level"]),
                        int(row["exp"]),
                    ),
                    clamp_skill_potential(int(row["potential"])),
                    int(row["user_pk"]),
                    row["skill_id"],
                ),
            )

        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (V11_PROGRESSION_MIGRATION,),
        )
        cursor = await db.execute("PRAGMA quick_check")
        integrity = await cursor.fetchone()
        await cursor.close()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("v11 成长迁移后的数据库完整性检查失败")
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _apply_classic_black_star_level_migration(db) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (CLASSIC_BLACK_STAR_LEVEL_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    placeholders = ", ".join("?" for _ in CLASSIC_BLACK_STAR_TEMPLATE_IDS)
    await db.execute("BEGIN")
    try:
        await db.execute(
            f"""
            UPDATE equipment_items
            SET item_level = 40
            WHERE template_id IN ({placeholders})
            """,
            CLASSIC_BLACK_STAR_TEMPLATE_IDS,
        )
        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (CLASSIC_BLACK_STAR_LEVEL_MIGRATION,),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _apply_classic_black_star_effects_migration(db) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (CLASSIC_BLACK_STAR_EFFECTS_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    try:
        from .equipment_catalog import DEFAULT_EQUIPMENT_CATALOG
    except ImportError:
        from services.equipment_catalog import DEFAULT_EQUIPMENT_CATALOG

    entries = tuple(
        DEFAULT_EQUIPMENT_CATALOG.snapshot.by_template_id[template_id]
        for template_id in CLASSIC_BLACK_STAR_TEMPLATE_IDS
    )
    await db.execute("BEGIN")
    try:
        for entry in entries:
            template = entry.template
            await db.execute(
                """
                UPDATE equipment_items
                SET name = ?, item_level = 40, weight = ?,
                    base_stats_json = ?, inherent_affixes_json = ?,
                    description = ?, source_effects_json = ?
                WHERE template_id = ?
                """,
                (
                    template.name,
                    template.weight,
                    json.dumps(template.base_stats, ensure_ascii=False),
                    json.dumps(
                        template.inherent_affixes, ensure_ascii=False
                    ),
                    template.description,
                    json.dumps(template.source_effects, ensure_ascii=False),
                    template.template_id,
                ),
            )
        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (CLASSIC_BLACK_STAR_EFFECTS_MIGRATION,),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
