"""Fetch Statcast data from Baseball Savant via pybaseball and store in SQLite."""

import os
import time
import sqlite3
import logging
from datetime import date, timedelta

import pandas as pd
import requests

from database import DB_PATH, init_db, log_update, upsert_player

logger = logging.getLogger(__name__)

STRIKE_ZONE_LEFT = -0.83
STRIKE_ZONE_RIGHT = 0.83
CALLED_DESCRIPTIONS = {"called_strike", "ball"}

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

# Statcast column names tried for ABS data (in priority order).
# If none are found we fall back to the MLB Stats API play-by-play endpoint.
ABS_CANDIDATE_COLS = [
    "abs_challenge_result", "challenge_result", "challenge_type",
    "automated_ball_strike_result", "review_result", "review_type",
]


# ---------------------------------------------------------------------------
# Zone / call helpers
# ---------------------------------------------------------------------------

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


def _parse_abs_value(val):
    """Normalise any ABS column value to 'overturned', 'stands', or None."""
    s = str(val or "").strip().lower()
    if not s or s in ("nan", "none", ""):
        return None
    if any(w in s for w in ("overturn", "reverse", "changed")):
        return "overturned"
    if any(w in s for w in ("stand", "confirm", "denied", "upheld")):
        return "stands"
    return None


# ---------------------------------------------------------------------------
# ABS data from MLB Stats API play-by-play
# ---------------------------------------------------------------------------

def fetch_abs_from_statsapi(game_pks: list) -> dict:
    """
    Query the MLB Stats API play-by-play endpoint for each game and return a
    dict mapping (game_pk, at_bat_number, pitch_number) → 'overturned'|'stands'.

    at_bat_number is 1-indexed (matching Statcast convention).
    The API's atBatIndex is 0-indexed, so we add 1.
    """
    if not game_pks:
        return {}

    challenges: dict = {}
    for gp in game_pks:
        try:
            url = f"https://statsapi.mlb.com/api/v1/game/{int(gp)}/playByPlay"
            resp = requests.get(url, timeout=12)
            if resp.status_code != 200:
                logger.debug("playByPlay HTTP %s for game %s", resp.status_code, gp)
                continue

            for play in resp.json().get("allPlays", []):
                at_bat_num = play.get("about", {}).get("atBatIndex", -1) + 1
                last_pitch_num = None

                for ev in play.get("playEvents", []):
                    if ev.get("isPitch"):
                        last_pitch_num = ev.get("pitchNumber")
                        continue

                    # Action events — look for ABS ball/strike challenge
                    details = ev.get("details", {})
                    etype = (details.get("eventType") or "").lower()
                    desc  = (details.get("description") or "").lower()

                    is_abs = (
                        "challenge_balls_strikes" in etype
                        or ("challenge" in desc and (
                            "called ball" in desc or "called strike" in desc
                            or "ball call" in desc or "strike call" in desc))
                    )
                    if not is_abs:
                        continue

                    pnum = ev.get("pitchNumber") or last_pitch_num
                    if pnum is None:
                        continue

                    result = "overturned" if "overturn" in desc else "stands"
                    challenges[(int(gp), at_bat_num, int(pnum))] = result

            time.sleep(0.15)

        except Exception as exc:
            logger.debug("ABS fetch failed for game %s: %s", gp, exc)

    return challenges


def _apply_abs_to_db(challenges: dict) -> int:
    """Write ABS challenge results into the pitches table. Returns rows updated."""
    if not challenges:
        return 0
    updated = 0
    with sqlite3.connect(DB_PATH) as conn:
        for (gp, abn, pn), result in challenges.items():
            cur = conn.execute(
                "UPDATE pitches SET abs_result=? WHERE game_pk=? AND at_bat_number=? AND pitch_number=?",
                (result, gp, abn, pn),
            )
            updated += cur.rowcount
    return updated


# ---------------------------------------------------------------------------
# Main fetch entry point
# ---------------------------------------------------------------------------

def fetch_and_store(start_dt: str, end_dt: str, chunk_days: int = 7) -> int:
    """
    Fetch Statcast data between start_dt and end_dt (YYYY-MM-DD strings),
    process, store in SQLite, and enrich with ABS challenge data from the
    MLB Stats API.  Returns total called-pitch rows inserted.
    """
    from pybaseball import statcast
    from pybaseball import cache as pb_cache
    pb_cache.enable()

    init_db()

    start = date.fromisoformat(start_dt)
    end   = date.fromisoformat(end_dt)
    if start > end:
        return 0

    total_inserted = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        s, e = str(cursor), str(chunk_end)
        logger.info("Fetching Statcast %s → %s …", s, e)
        try:
            df = statcast(start_dt=s, end_dt=e)
            if df is None or df.empty:
                logger.info("  No data for %s → %s", s, e)
                cursor = chunk_end + timedelta(days=1)
                continue

            # Log all column names once to help diagnose ABS availability
            logger.debug("Statcast columns: %s", sorted(df.columns.tolist()))

            # Detect ABS column from Statcast (may not exist — we use Stats API instead)
            abs_col = next(
                (c for c in ABS_CANDIDATE_COLS if c in df.columns and df[c].notna().any()),
                None,
            )
            if abs_col:
                logger.info("  Statcast ABS column found: %s", abs_col)
            else:
                logger.info("  No ABS column in Statcast data — will use MLB Stats API.")

            # Trim to needed columns
            avail = [c for c in KEEP_COLS if c in df.columns]
            if abs_col:
                avail.append(abs_col)
            df = df[avail].copy()

            df["is_called"] = df["description"].isin(CALLED_DESCRIPTIONS).astype(int)
            called_df = df[df["is_called"] == 1].copy()
            if called_df.empty:
                cursor = chunk_end + timedelta(days=1)
                continue

            batter_ids = df["batter"].dropna().unique().tolist() if "batter" in df.columns else []

            rows = []
            for _, r in called_df.iterrows():
                it = _clean(r.get("inning_topbot"))
                ht = _clean(r.get("home_team"))
                at = _clean(r.get("away_team"))
                hitting_team  = at if it == "Top" else ht
                pitching_team = ht if it == "Top" else at

                iz = _is_in_zone(r.get("plate_x"), r.get("plate_z"), r.get("sz_top"), r.get("sz_bot"))
                cc = _correct_call(r.get("description"), iz)
                # ABS from Statcast column if available; will be overwritten by Stats API
                ar = _parse_abs_value(r.get(abs_col)) if abs_col else None

                rows.append((
                    str(r.get("game_date", ""))[:10],
                    _int(r.get("game_pk")),
                    _int(r.get("at_bat_number")),
                    _int(r.get("pitch_number")),
                    _int(r.get("pitcher")),
                    _clean(r.get("player_name")),
                    _int(r.get("batter")),
                    None,                              # batter_name filled separately
                    ht, at,
                    hitting_team, pitching_team,
                    _int(r.get("inning")),
                    it,
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
                    int(iz) if iz is not None else None,
                    1,
                    cc,
                    ar,
                ))

            with sqlite3.connect(DB_PATH) as conn:
                inserted = conn.executemany(
                    """INSERT OR IGNORE INTO pitches
                       (game_date,game_pk,at_bat_number,pitch_number,
                        pitcher_id,pitcher_name,batter_id,batter_name,
                        home_team,away_team,hitting_team,pitching_team,
                        inning,inning_topbot,description,pitch_type,release_speed,
                        p_throws,stand,plate_x,plate_z,sz_top,sz_bot,
                        zone,umpire,in_zone,is_called,correct_call,abs_result)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                ).rowcount
            total_inserted += max(inserted, 0)
            logger.info("  Inserted %d rows", max(inserted, 0))

            # Batter name lookup
            _lookup_batters(batter_ids)

            # Enrich with ABS challenge data from MLB Stats API
            chunk_game_pks = called_df["game_pk"].dropna().unique().tolist()
            if chunk_game_pks:
                logger.info("  Fetching ABS data for %d games via MLB Stats API…", len(chunk_game_pks))
                abs_challenges = fetch_abs_from_statsapi(chunk_game_pks)
                n_abs = _apply_abs_to_db(abs_challenges)
                logger.info("  Applied %d ABS challenge results", n_abs)

            time.sleep(1)

        except Exception as exc:
            logger.exception("Error fetching %s → %s: %s", s, e, exc)
            log_update(s, e, 0, "error", str(exc))
            raise

        cursor = chunk_end + timedelta(days=1)

    log_update(start_dt, end_dt, total_inserted, "success")
    return total_inserted


def update_abs_data(start_dt: str, end_dt: str) -> int:
    """
    Re-fetch ABS challenge data from the MLB Stats API for all games already
    stored in the database within the date range.  Use this to retroactively
    populate abs_result for data fetched before ABS tracking was added.
    """
    if not os.path.exists(DB_PATH):
        return 0
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT game_pk FROM pitches"
            " WHERE game_date >= ? AND game_date <= ? AND game_pk IS NOT NULL",
            (start_dt, end_dt),
        ).fetchall()
    game_pks = [r[0] for r in rows]
    if not game_pks:
        logger.info("No games found for %s → %s", start_dt, end_dt)
        return 0
    logger.info("Fetching ABS data for %d games (%s → %s)…", len(game_pks), start_dt, end_dt)
    challenges = fetch_abs_from_statsapi(game_pks)
    updated = _apply_abs_to_db(challenges)
    logger.info("Updated %d ABS results", updated)
    return updated


# ---------------------------------------------------------------------------
# Player name lookup
# ---------------------------------------------------------------------------

def _lookup_batters(batter_ids):
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
        with sqlite3.connect(DB_PATH) as conn:
            for _, row in result.iterrows():
                pid   = row.get("key_mlbam")
                first = row.get("name_first", "")
                last  = row.get("name_last", "")
                name  = f"{first} {last}".strip() or None
                if pid and name:
                    upsert_player(int(pid), name)
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
