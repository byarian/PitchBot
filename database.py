"""Database setup and query functions for PitchBot."""

import os
import sqlite3

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
    hitting_team    TEXT,
    pitching_team   TEXT,
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

CREATE INDEX IF NOT EXISTS idx_pitches_date    ON pitches(game_date);
CREATE INDEX IF NOT EXISTS idx_pitches_umpire  ON pitches(umpire);
CREATE INDEX IF NOT EXISTS idx_pitches_pitcher ON pitches(pitcher_id);
CREATE INDEX IF NOT EXISTS idx_pitches_batter  ON pitches(batter_id);
CREATE INDEX IF NOT EXISTS idx_pitches_called  ON pitches(is_called);
"""


def get_db_path():
    return DB_PATH


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(DDL)
    _migrate()


def _migrate():
    """Add columns introduced after initial release without breaking existing DBs."""
    with sqlite3.connect(DB_PATH) as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(pitches)")}
        for col in ("hitting_team", "pitching_team"):
            if col not in existing:
                conn.execute(f"ALTER TABLE pitches ADD COLUMN {col} TEXT")
        # Back-fill hitting_team / pitching_team for rows that predate the column.
        conn.execute("""
            UPDATE pitches
            SET hitting_team  = CASE WHEN inning_topbot='Top' THEN away_team ELSE home_team END,
                pitching_team = CASE WHEN inning_topbot='Top' THEN home_team ELSE away_team END
            WHERE hitting_team IS NULL AND inning_topbot IS NOT NULL
        """)
        # Team indexes — created here so they exist even when added via ALTER TABLE
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pitches_hitting_team"
                     " ON pitches(hitting_team)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pitches_pitching_team"
                     " ON pitches(pitching_team)")


# ---------------------------------------------------------------------------
# Simple getters
# ---------------------------------------------------------------------------

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
            "INSERT OR REPLACE INTO players(player_id,player_name,updated_at) VALUES(?,?,datetime('now'))",
            (player_id, player_name),
        )


def get_player_name(player_id):
    if not os.path.exists(DB_PATH):
        return str(player_id)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT player_name FROM players WHERE player_id=?", (player_id,)).fetchone()
    return row[0] if row else str(player_id)


# ---------------------------------------------------------------------------
# WHERE clause builder
# ---------------------------------------------------------------------------

def _build_where(filters):
    """Return (where_str, params) for is_called pitches matching filters."""
    clauses = ["is_called = 1"]
    params = []

    if filters.get("start_date"):
        clauses.append("game_date >= ?")
        params.append(filters["start_date"])
    if filters.get("end_date"):
        clauses.append("game_date <= ?")
        params.append(filters["end_date"])
    if filters.get("umpires"):
        ph = ",".join("?" * len(filters["umpires"]))
        clauses.append(f"umpire IN ({ph})")
        params.extend(filters["umpires"])
    if filters.get("hitting_teams"):
        ph = ",".join("?" * len(filters["hitting_teams"]))
        clauses.append(f"hitting_team IN ({ph})")
        params.extend(filters["hitting_teams"])
    if filters.get("pitching_teams"):
        ph = ",".join("?" * len(filters["pitching_teams"]))
        clauses.append(f"pitching_team IN ({ph})")
        params.extend(filters["pitching_teams"])
    if filters.get("pitchers"):
        ph = ",".join("?" * len(filters["pitchers"]))
        clauses.append(f"pitcher_id IN ({ph})")
        params.extend(filters["pitchers"])
    if filters.get("batters"):
        ph = ",".join("?" * len(filters["batters"]))
        clauses.append(f"batter_id IN ({ph})")
        params.extend(filters["batters"])
    if filters.get("pitch_types"):
        ph = ",".join("?" * len(filters["pitch_types"]))
        clauses.append(f"pitch_type IN ({ph})")
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


# ---------------------------------------------------------------------------
# Dynamic (cascading) filter options
# ---------------------------------------------------------------------------

def get_dynamic_options(filters=None):
    """
    Return dropdown options for every filter, narrowed by all currently active
    filters.  Firing on every filter-change gives the cascading update behaviour.
    """
    empty = {k: [] for k in ("umpires", "hitting_teams", "pitching_teams",
                              "pitch_types", "pitchers", "batters")}
    if not os.path.exists(DB_PATH):
        return empty

    filters = filters or {}
    where, params = _build_where(filters)

    with sqlite3.connect(DB_PATH) as conn:
        umpires = [
            {"label": r[0], "value": r[0]}
            for r in conn.execute(
                f"SELECT DISTINCT umpire FROM pitches"
                f" WHERE {where} AND umpire IS NOT NULL AND umpire != '' ORDER BY umpire",
                params,
            )
        ]
        hitting_teams = [
            {"label": r[0], "value": r[0]}
            for r in conn.execute(
                f"SELECT DISTINCT hitting_team FROM pitches"
                f" WHERE {where} AND hitting_team IS NOT NULL ORDER BY hitting_team",
                params,
            )
        ]
        pitching_teams = [
            {"label": r[0], "value": r[0]}
            for r in conn.execute(
                f"SELECT DISTINCT pitching_team FROM pitches"
                f" WHERE {where} AND pitching_team IS NOT NULL ORDER BY pitching_team",
                params,
            )
        ]
        pitch_types = [
            {"label": f"{r[0]} ({r[1]:,})", "value": r[0]}
            for r in conn.execute(
                f"SELECT pitch_type, COUNT(*) n FROM pitches"
                f" WHERE {where} AND pitch_type IS NOT NULL GROUP BY pitch_type ORDER BY n DESC",
                params,
            )
        ]
        pitchers = [
            {"label": r[0] or f"ID {r[1]}", "value": r[1]}
            for r in conn.execute(
                f"SELECT pitcher_name, pitcher_id FROM pitches"
                f" WHERE {where} AND pitcher_id IS NOT NULL"
                f" GROUP BY pitcher_id ORDER BY pitcher_name",
                params,
            )
        ]
        # Subquery reuses same params — SQLite matches placeholders in order
        batter_sql = (
            f"SELECT COALESCE(pl.player_name,'ID '||p.batter_id), p.batter_id"
            f" FROM (SELECT DISTINCT batter_id FROM pitches WHERE {where}"
            f"       AND batter_id IS NOT NULL) p"
            f" LEFT JOIN players pl ON pl.player_id = p.batter_id ORDER BY 1"
        )
        batters = [
            {"label": r[0], "value": r[1]}
            for r in conn.execute(batter_sql, params)
        ]

    return {
        "umpires": umpires,
        "hitting_teams": hitting_teams,
        "pitching_teams": pitching_teams,
        "pitch_types": pitch_types,
        "pitchers": pitchers,
        "batters": batters,
    }


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def query_pitches(filters=None, limit=15000):
    if not os.path.exists(DB_PATH):
        return []
    filters = filters or {}
    where, params = _build_where(filters)
    sql = f"""
        SELECT game_date, pitcher_name, batter_name, batter_id, umpire,
               home_team, away_team, hitting_team, pitching_team,
               pitch_type, release_speed, p_throws, stand,
               plate_x, plate_z, sz_top, sz_bot,
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


def query_summary_stats(filters=None):
    if not os.path.exists(DB_PATH):
        return {}
    filters = filters or {}
    where, params = _build_where(filters)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            f"""SELECT COUNT(*),
                       SUM(correct_call),
                       SUM(CASE WHEN correct_call=0 AND description='called_strike' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN correct_call=0 AND description='ball'          THEN 1 ELSE 0 END),
                       SUM(CASE WHEN abs_result='overturned' THEN 1 ELSE 0 END)
                FROM pitches WHERE {where}""",
            params,
        ).fetchone()
    if not row or not row[0]:
        return {"total": 0, "correct": 0, "phantom_strikes": 0, "missed_strikes": 0, "abs_overturned": 0}
    return dict(zip(["total", "correct", "phantom_strikes", "missed_strikes", "abs_overturned"], row))


def query_by_group(group_col, filters=None, limit=25):
    if not os.path.exists(DB_PATH):
        return []
    filters = filters or {}
    where, params = _build_where(filters)
    join = ""
    sel = f"p.{group_col}"
    if group_col == "batter_id":
        join = "LEFT JOIN players pl ON pl.player_id = p.batter_id"
        sel = "COALESCE(pl.player_name,'ID '||p.batter_id)"
    sql = f"""
        SELECT {sel} AS grp,
               COUNT(*) AS called,
               SUM(CASE WHEN p.correct_call=0 THEN 1 ELSE 0 END) AS incorrect,
               ROUND(100.0*SUM(CASE WHEN p.correct_call=0 THEN 1 ELSE 0 END)/COUNT(*),1) AS miss_rate
        FROM pitches p {join}
        WHERE {where} AND {group_col} IS NOT NULL AND {group_col} != ''
        GROUP BY {sel}
        HAVING called >= 10
        ORDER BY miss_rate DESC
        LIMIT {int(limit)}
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"group": r[0], "called": r[1], "incorrect": r[2], "miss_rate": r[3]} for r in rows]


def query_by_pitch_type_hand(filters=None):
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
    return [{"pitch_type": r[0], "p_throws": r[1], "called": r[2], "incorrect": r[3], "miss_rate": r[4]}
            for r in rows]
