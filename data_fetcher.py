"""Fetch Statcast data from Baseball Savant via pybaseball and store in SQLite."""

import os
import time
import sqlite3
import logging
from datetime import date, timedelta

import pandas as pd

from database import DB_PATH, init_db, log_update, upsert_player

logger = logging.getLogger(__name__)

STRIKE_ZONE_LEFT = -0.83
STRIKE_ZONE_RIGHT = 0.83

CALLED_DESCRIPTIONS = {"called_strike", "ball"}

# Columns we keep from raw Statcast; subset avoids OOM on large pulls
KEEP_COLS = [
    "game_date", "game_pk", "at_bat_number", "pitch_number",
    "pitcher", "player_name",
    "batter",
    "home_team", "away_team",
    "inning", "inning_topbot",
    "description", "type",
    "pitch_type", "release_speed",
    "p_throws", "stand",
    "plate_x", "plate_z", "sz_top", "sz_bot", "zone",
    "umpire",
]

ABS_CANDIDATE_COLS = [
    "challenge_type", "challenge_result", "abs_challenge_result",
    "automated_ball_strike_result", "review_type", "review_result",
]


def _is_in_zone(plate_x, plate_z, sz_top, sz_bot):
    try:
        return (
            STRIKE_ZONE_LEFT <= float(plate_x) <= STRIKE_ZONE_RIGHT
            and float(sz_bot) <= float(plate_z) <= float(sz_top)
        )
    except (TypeError, ValueError):
        return None


def _correct_call(description, in_zone):
    if in_zone is None:
        return None
    if description == "called_strike":
        return 1 if in_zone else 0
    if description == "ball":
        return 0 if in_zone else 1
    return None


def _abs_result(row, abs_col):
    if abs_col is None:
        return None
    val = str(row.get(abs_col, "") or "").lower()
    if not val:
        return None
    if "overturn" in val or "reversed" in val or "upheld" in val:
        return "overturned"
    if "stand" in val or "confirmed" in val or "denied" in val:
        return "stands"
    return val if val else None


def fetch_and_store(start_dt: str, end_dt: str, chunk_days: int = 7) -> int:
    """
    Fetch Statcast data between start_dt and end_dt (YYYY-MM-DD strings),
    process, and insert into SQLite.  Returns total rows inserted.
    """
    from pybaseball import statcast
    from pybaseball import cache as pb_cache
    pb_cache.enable()

    init_db()

    start = date.fromisoformat(start_dt)
    end = date.fromisoformat(end_dt)
    if start > end:
        logger.info("start_dt is after end_dt — nothing to fetch.")
        return 0

    total_inserted = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        s, e = str(cursor), str(chunk_end)
        logger.info("Fetching %s → %s …", s, e)
        try:
            df = statcast(start_dt=s, end_dt=e)
            if df is None or df.empty:
                logger.info("  No data returned for %s → %s", s, e)
                cursor = chunk_end + timedelta(days=1)
                continue

            # Detect ABS columns
            abs_col = None
            for cand in ABS_CANDIDATE_COLS:
                if cand in df.columns:
                    abs_col = cand
                    logger.info("  ABS column detected: %s", cand)
                    break

            # Keep only needed columns (plus any abs col)
            avail = [c for c in KEEP_COLS if c in df.columns]
            if abs_col:
                avail.append(abs_col)
            df = df[avail].copy()

            # Filter to called pitches only (keep all for storage but flag)
            df["is_called"] = df["description"].isin(CALLED_DESCRIPTIONS).astype(int)

            # Compute zone / correctness for called pitches
            def process_row(r):
                if r["is_called"] == 0:
                    return pd.Series({"in_zone": None, "correct_call": None, "abs_result": None})
                iz = _is_in_zone(r.get("plate_x"), r.get("plate_z"), r.get("sz_top"), r.get("sz_bot"))
                cc = _correct_call(r.get("description"), iz)
                ar = _abs_result(r, abs_col)
                return pd.Series({"in_zone": int(iz) if iz is not None else None, "correct_call": cc, "abs_result": ar})

            computed = df.apply(process_row, axis=1)
            df = pd.concat([df, computed], axis=1)

            # Collect batter IDs for player name lookup
            batter_ids = df["batter"].dropna().unique().tolist() if "batter" in df.columns else []

            # Build insert rows — only called pitches go into DB
            called_df = df[df["is_called"] == 1].copy()
            if called_df.empty:
                cursor = chunk_end + timedelta(days=1)
                continue

            rows = []
            for _, r in called_df.iterrows():
                rows.append((
                    str(r.get("game_date", ""))[:10],
                    _int(r.get("game_pk")),
                    _int(r.get("at_bat_number")),
                    _int(r.get("pitch_number")),
                    _int(r.get("pitcher")),
                    _clean(r.get("player_name")),     # pitcher name
                    _int(r.get("batter")),
                    None,                              # batter_name (filled separately)
                    _clean(r.get("home_team")),
                    _clean(r.get("away_team")),
                    _int(r.get("inning")),
                    _clean(r.get("inning_topbot")),
                    _clean(r.get("description")),
                    _clean(r.get("pitch_type")),
                    _float(r.get("release_speed")),
                    _clean(r.get("p_throws")),
                    _clean(r.get("stand")),
                    _float(r.get("plate_x")),
                    _float(r.get("plate_z")),
                    _float(r.get("sz_top")),
                    _float(r.get("sz_bot")),
                    _int(r.get("zone")),
                    _clean(r.get("umpire")),
                    r.get("in_zone"),
                    1,
                    r.get("correct_call"),
                    r.get("abs_result"),
                ))

            with sqlite3.connect(DB_PATH) as conn:
                inserted = conn.executemany(
                    """INSERT OR IGNORE INTO pitches
                       (game_date,game_pk,at_bat_number,pitch_number,
                        pitcher_id,pitcher_name,batter_id,batter_name,
                        home_team,away_team,inning,inning_topbot,
                        description,pitch_type,release_speed,
                        p_throws,stand,plate_x,plate_z,sz_top,sz_bot,
                        zone,umpire,in_zone,is_called,correct_call,abs_result)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                ).rowcount
            total_inserted += max(inserted, 0)
            logger.info("  Inserted %d rows (chunk total %d)", max(inserted, 0), len(rows))

            # Populate player names for batters
            _lookup_batters(batter_ids)

            time.sleep(1)  # polite delay for Baseball Savant

        except Exception as exc:
            logger.exception("Error fetching %s → %s: %s", s, e, exc)
            log_update(s, e, 0, "error", str(exc))
            raise

        cursor = chunk_end + timedelta(days=1)

    log_update(start_dt, end_dt, total_inserted, "success")
    return total_inserted


def _lookup_batters(batter_ids):
    """Look up batter names from MLBAM IDs and store in players table."""
    if not batter_ids:
        return
    try:
        from pybaseball import playerid_reverse_lookup
        ids = [int(i) for i in batter_ids if i]
        if not ids:
            return
        result = playerid_reverse_lookup(ids, key_type="mlbam")
        if result is None or result.empty:
            return
        for _, row in result.iterrows():
            pid = row.get("key_mlbam")
            first = row.get("name_first", "")
            last = row.get("name_last", "")
            name = f"{first} {last}".strip() if (first or last) else None
            if pid and name:
                upsert_player(int(pid), name)
        # Also update batter_name in pitches
        with sqlite3.connect(DB_PATH) as conn:
            for _, row in result.iterrows():
                pid = row.get("key_mlbam")
                first = row.get("name_first", "")
                last = row.get("name_last", "")
                name = f"{first} {last}".strip() if (first or last) else None
                if pid and name:
                    conn.execute(
                        "UPDATE pitches SET batter_name=? WHERE batter_id=? AND batter_name IS NULL",
                        (name, int(pid)),
                    )
    except Exception as exc:
        logger.warning("Batter name lookup failed: %s", exc)


# ---------------------------------------------------------------------------
# Type coercions
# ---------------------------------------------------------------------------

def _int(val):
    try:
        return int(val) if val is not None and val == val else None
    except (TypeError, ValueError):
        return None


def _float(val):
    try:
        return float(val) if val is not None and val == val else None
    except (TypeError, ValueError):
        return None


def _clean(val):
    if val is None or (isinstance(val, float) and val != val):
        return None
    return str(val).strip() or None
