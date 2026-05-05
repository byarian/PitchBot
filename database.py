"""Database setup and query functions for PitchBot."""

import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pitchbot.db")

STRIKE_ZONE_LEFT = -0.83
STRIKE_ZONE_RIGHT = 0.83

DDL = """
CREATE TABLE IF NOT EXISTS pitches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_date       TEXT    NOT NULL,
    game_pk         INTEGER,
    at_bat_number   INTEGER,
    pitch_number    INTEGER,
    pitcher_id      INTEGER,
    pitcher_name    TEXT,
    batter_id       INTEGER,
    batter_name     TEXT,
    home_team       TEXT,
    away_team       TEXT,
    inning          INTEGER,
    inning_topbot   TEXT,
    description     TEXT,
    pitch_type      TEXT,
    release_speed   REAL,
    p_throws        TEXT,
    stand           TEXT,
    plate_x         REAL,
    plate_z         REAL,
    sz_top          REAL,
    sz_bot          REAL,
    zone            INTEGER,
    umpire          TEXT,
    in_zone         INTEGER,
    is_called       INTEGER,
    correct_call    INTEGER,
    abs_result      TEXT,
    UNIQUE(game_pk, at_bat_number, pitch_number)
);

CREATE TABLE IF NOT EXISTS players (
    player_id   INTEGER PRIMARY KEY,
    player_name TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS update_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date      TEXT,
    fetch_start   TEXT,
    fetch_end     TEXT,
    pitches_added INTEGER,
    status        TEXT,
    error_msg     TEXT,
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pitches_date     ON pitches(game_date);
CREATE INDEX IF NOT EXISTS idx_pitches_umpire   ON pitches(umpire);
CREATE INDEX IF NOT EXISTS idx_pitches_pitcher  ON pitches(pitcher_id);
CREATE INDEX IF NOT EXISTS idx_pitches_batter   ON pitches(batter_id);
CREATE INDEX IF NOT EXISTS idx_pitches_called   ON pitches(is_called);
CREATE INDEX IF NOT EXISTS idx_pitches_team     ON pitches(home_team, away_team);
"""


def get_db_path():
    return DB_PATH


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(DDL)


def get_last_game_date():
    if not os.path.exists(DB_PATH):
        return None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT MAX(game_date) FROM pitches").fetchone()
    return row[0] if row and row[0] else None


def get_first_game_date():
    if not os.path.exists(DB_PATH):
        return None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT MIN(game_date) FROM pitches").fetchone()
    return row[0] if row and row[0] else None


def get_pitch_count():
    if not os.path.exists(DB_PATH):
        return 0
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM pitches WHERE is_called=1").fetchone()
    return row[0] if row else 0


def log_update(fetch_start, fetch_end, pitches_added, status, error_msg=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO update_log(run_date,fetch_start,fetch_end,pitches_added,status,error_msg,created_at)"
            " VALUES(date('now'),?,?,?,?,?,datetime('now'))",
            (fetch_start, fetch_end, pitches_added, status, error_msg),
        )


def upsert_player(player_id, player_name):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO players(player_id, player_name, updated_at) VALUES(?,?,datetime('now'))",
            (player_id, player_name),
        )


def get_player_name(player_id):
    if not os.path.exists(DB_PATH):
        return str(player_id)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT player_name FROM players WHERE player_id=?", (player_id,)).fetchone()
    return row[0] if row else str(player_id)


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def get_filter_options():
    """Return dicts of dropdown options for filters."""
    if not os.path.exists(DB_PATH):
        return {k: [] for k in ("umpires", "teams", "pitch_types", "pitchers", "batters")}
    with sqlite3.connect(DB_PATH) as conn:
        umpires = [
            {"label": r[0], "value": r[0]}
            for r in conn.execute(
                "SELECT DISTINCT umpire FROM pitches WHERE umpire IS NOT NULL AND umpire != '' ORDER BY umpire"
            )
        ]
        teams_raw = set()
        for r in conn.execute("SELECT DISTINCT home_team, away_team FROM pitches WHERE home_team IS NOT NULL"):
            if r[0]:
                teams_raw.add(r[0])
            if r[1]:
                teams_raw.add(r[1])
        teams = [{"label": t, "value": t} for t in sorted(teams_raw)]

        pitch_types = [
            {"label": f"{r[0]} ({r[1]:,})", "value": r[0]}
            for r in conn.execute(
                "SELECT pitch_type, COUNT(*) AS n FROM pitches WHERE pitch_type IS NOT NULL AND is_called=1"
                " GROUP BY pitch_type ORDER BY n DESC"
            )
        ]
        pitchers = [
            {"label": r[0] or f"ID {r[1]}", "value": r[1]}
            for r in conn.execute(
                "SELECT pitcher_name, pitcher_id FROM pitches WHERE pitcher_id IS NOT NULL"
                " GROUP BY pitcher_id ORDER BY pitcher_name"
            )
        ]
        batters_sql = """
            SELECT COALESCE(pl.player_name, 'ID '||p.batter_id), p.batter_id
            FROM (SELECT DISTINCT batter_id FROM pitches WHERE batter_id IS NOT NULL) p
            LEFT JOIN players pl ON pl.player_id = p.batter_id
            ORDER BY 1
        """
        batters = [
            {"label": r[0], "value": r[1]}
            for r in conn.execute(batters_sql)
        ]
    return {
        "umpires": umpires,
        "teams": teams,
        "pitch_types": pitch_types,
        "pitchers": pitchers,
        "batters": batters,
    }


# ---------------------------------------------------------------------------
# Main data queries
# ---------------------------------------------------------------------------

def _build_where(filters):
    """Build WHERE clause and params list from filter dict. Always limits to is_called=1."""
    clauses = ["is_called = 1"]
    params = []

    if filters.get("start_date"):
        clauses.append("game_date >= ?")
        params.append(filters["start_date"])
    if filters.get("end_date"):
        clauses.append("game_date <= ?")
        params.append(filters["end_date"])
    if filters.get("umpires"):
        placeholders = ",".join("?" * len(filters["umpires"]))
        clauses.append(f"umpire IN ({placeholders})")
        params.extend(filters["umpires"])
    if filters.get("teams"):
        placeholders = ",".join("?" * len(filters["teams"]))
        clauses.append(f"(home_team IN ({placeholders}) OR away_team IN ({placeholders}))")
        params.extend(filters["teams"] * 2)
    if filters.get("pitchers"):
        placeholders = ",".join("?" * len(filters["pitchers"]))
        clauses.append(f"pitcher_id IN ({placeholders})")
        params.extend(filters["pitchers"])
    if filters.get("batters"):
        placeholders = ",".join("?" * len(filters["batters"]))
        clauses.append(f"batter_id IN ({placeholders})")
        params.extend(filters["batters"])
    if filters.get("pitch_types"):
        placeholders = ",".join("?" * len(filters["pitch_types"]))
        clauses.append(f"pitch_type IN ({placeholders})")
        params.extend(filters["pitch_types"])
    if filters.get("p_throws") and filters["p_throws"] != "B":
        clauses.append("p_throws = ?")
        params.append(filters["p_throws"])
    if filters.get("call_type") == "correct":
        clauses.append("correct_call = 1")
    elif filters.get("call_type") == "incorrect":
        clauses.append("correct_call = 0")
    if filters.get("abs_filter") == "overturned":
        clauses.append("abs_result = 'overturned'")

    return " AND ".join(clauses), params


def query_pitches(filters=None, limit=15000):
    """Return individual pitch rows for scatter plot."""
    if not os.path.exists(DB_PATH):
        return []
    filters = filters or {}
    where, params = _build_where(filters)
    sql = f"""
        SELECT
            game_date, pitcher_name, batter_name, batter_id, umpire,
            home_team, away_team, pitch_type, release_speed,
            p_throws, stand, plate_x, plate_z, sz_top, sz_bot,
            description, in_zone, correct_call, abs_result, zone
        FROM pitches
        WHERE {where}
        ORDER BY game_date DESC, game_pk DESC, at_bat_number DESC, pitch_number DESC
        LIMIT {int(limit)}
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_total_called(filters=None):
    if not os.path.exists(DB_PATH):
        return 0
    filters = filters or {}
    where, params = _build_where(filters)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM pitches WHERE {where}", params).fetchone()
    return row[0] if row else 0


def query_summary_stats(filters=None):
    """Return aggregated stats dict."""
    if not os.path.exists(DB_PATH):
        return {}
    filters = filters or {}
    where, params = _build_where(filters)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            f"""SELECT
                COUNT(*) AS total,
                SUM(correct_call) AS correct,
                SUM(CASE WHEN correct_call=0 AND description='called_strike' THEN 1 ELSE 0 END) AS phantom_strikes,
                SUM(CASE WHEN correct_call=0 AND description='ball' THEN 1 ELSE 0 END) AS missed_strikes,
                SUM(CASE WHEN abs_result='overturned' THEN 1 ELSE 0 END) AS abs_overturned
            FROM pitches WHERE {where}""",
            params,
        ).fetchone()
    if not row or not row[0]:
        return {"total": 0, "correct": 0, "phantom_strikes": 0, "missed_strikes": 0, "abs_overturned": 0}
    return dict(zip(["total", "correct", "phantom_strikes", "missed_strikes", "abs_overturned"], row))


def query_by_group(group_col, filters=None, limit=25):
    """Return miss-rate aggregation by group_col."""
    if not os.path.exists(DB_PATH):
        return []
    filters = filters or {}
    where, params = _build_where(filters)
    label_join = ""
    select_col = f"p.{group_col}"
    if group_col == "batter_id":
        label_join = "LEFT JOIN players pl ON pl.player_id = p.batter_id"
        select_col = "COALESCE(pl.player_name, 'ID '||p.batter_id)"
    sql = f"""
        SELECT {select_col} AS grp,
               COUNT(*) AS called,
               SUM(CASE WHEN p.correct_call=0 THEN 1 ELSE 0 END) AS incorrect,
               ROUND(100.0*SUM(CASE WHEN p.correct_call=0 THEN 1 ELSE 0 END)/COUNT(*),1) AS miss_rate
        FROM pitches p
        {label_join}
        WHERE {where} AND {group_col} IS NOT NULL AND {group_col} != ''
        GROUP BY {select_col}
        HAVING called >= 10
        ORDER BY miss_rate DESC
        LIMIT {int(limit)}
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"group": r[0], "called": r[1], "incorrect": r[2], "miss_rate": r[3]} for r in rows]


def query_by_pitch_type_hand(filters=None):
    """Return miss rate breakdown by pitch_type × p_throws."""
    if not os.path.exists(DB_PATH):
        return []
    filters = filters or {}
    where, params = _build_where(filters)
    sql = f"""
        SELECT pitch_type, p_throws,
               COUNT(*) AS called,
               SUM(CASE WHEN correct_call=0 THEN 1 ELSE 0 END) AS incorrect,
               ROUND(100.0*SUM(CASE WHEN correct_call=0 THEN 1 ELSE 0 END)/COUNT(*),1) AS miss_rate
        FROM pitches
        WHERE {where} AND pitch_type IS NOT NULL AND p_throws IS NOT NULL
        GROUP BY pitch_type, p_throws
        HAVING called >= 10
        ORDER BY miss_rate DESC
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"pitch_type": r[0], "p_throws": r[1], "called": r[2], "incorrect": r[3], "miss_rate": r[4]}
        for r in rows
    ]
