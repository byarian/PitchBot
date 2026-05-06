#!/usr/bin/env python3
"""
PitchBot nightly updater.

Usage
-----
  python update.py                                           # fetch yesterday → today
  python update.py --start-date 2025-03-20                  # fetch from date to yesterday
  python update.py --start-date 2025-04-01 --end-date 2025-04-30
  python update.py --full-season                            # fetch full 2025 season to date
  python update.py --update-abs                             # retroactively fix ABS data for all stored games
  python update.py --update-abs --start-date 2025-04-01    # ABS fix for specific range

Cron example (runs at 4 AM daily):
  0 4 * * * cd /home/user/PitchBot && /usr/bin/python3 update.py >> logs/update.log 2>&1
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SEASON_START = "2025-03-20"


def main():
    parser = argparse.ArgumentParser(description="PitchBot data updater")
    parser.add_argument("--start-date",  type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date",    type=str, help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--full-season", action="store_true", help="Fetch entire 2025 season")
    parser.add_argument("--chunk-days",  type=int, default=7, help="Days per API chunk (default 7)")
    parser.add_argument("--update-abs",  action="store_true",
                        help="Retroactively fetch ABS challenge data for already-stored games")
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs("logs", exist_ok=True)

    from database import init_db, get_last_game_date, get_first_game_date
    from data_fetcher import fetch_and_store, update_abs_data

    init_db()

    yesterday = str(date.today() - timedelta(days=1))

    # ABS-only retroactive update
    if args.update_abs:
        start_dt = args.start_date or get_first_game_date() or SEASON_START
        end_dt   = args.end_date   or yesterday
        logger.info("Retroactively updating ABS challenge data: %s → %s", start_dt, end_dt)
        try:
            n = update_abs_data(start_dt, end_dt)
            logger.info("Done. %d ABS results updated.", n)
        except Exception as exc:
            logger.exception("ABS update failed: %s", exc)
            sys.exit(1)
        sys.exit(0)

    # Normal Statcast fetch
    if args.full_season:
        start_dt = SEASON_START
    elif args.start_date:
        start_dt = args.start_date
    else:
        last = get_last_game_date()
        if last:
            from datetime import datetime
            start_dt = str(datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1))
        else:
            start_dt = SEASON_START

    end_dt = args.end_date or yesterday

    if start_dt > end_dt:
        logger.info("Database is up to date through %s — nothing to fetch.", end_dt)
        sys.exit(0)

    logger.info("Fetching Statcast data: %s → %s", start_dt, end_dt)
    try:
        count = fetch_and_store(start_dt, end_dt, chunk_days=args.chunk_days)
        logger.info("Done. %d called-pitch rows inserted/updated.", count)
    except Exception as exc:
        logger.exception("Update failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
