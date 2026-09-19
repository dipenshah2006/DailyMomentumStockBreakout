import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytz

import rsi_mtf_report_nse as scanner


IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC


class NseMarketDateTests(unittest.TestCase):
    def test_utc_boundary_uses_indian_market_date(self):
        # 00:30 UTC is already 06:00 Saturday in India.
        utc_now = UTC.localize(datetime(2026, 9, 19, 0, 30))
        self.assertEqual(scanner._last_trading_day_str(utc_now), "2026-09-18")

    def test_weekends_keep_fridays_latest_session(self):
        saturday = IST.localize(datetime(2026, 9, 19, 21, 0))
        sunday = IST.localize(datetime(2026, 9, 20, 21, 0))
        self.assertEqual(scanner._last_trading_day_str(saturday), "2026-09-18")
        self.assertEqual(scanner._last_trading_day_str(sunday), "2026-09-18")

    def test_weekday_after_close_uses_current_session(self):
        before_close = IST.localize(datetime(2026, 9, 18, 15, 29))
        after_close = IST.localize(datetime(2026, 9, 18, 15, 30))
        self.assertEqual(scanner._last_trading_day_str(before_close), "2026-09-17")
        self.assertEqual(scanner._last_trading_day_str(after_close), "2026-09-18")

    def test_nrbbearing_cache_predating_latest_session_is_refreshed(self):
        old_date = pd.Timestamp("2026-09-17")
        new_date = pd.Timestamp("2026-09-18")
        old_df = pd.DataFrame({"Close": [100.0]}, index=[old_date])
        new_df = pd.DataFrame({"Close": [101.0]}, index=[new_date])
        original_cache = scanner._CACHE
        original_dirty = scanner._CACHE_DIRTY
        scanner._CACHE = {
            "NRBBEARING": {
                "df": old_df,
                "last_date": "2026-09-17",
                "marketcap": 1.0,
                "mcap_ts": 4_000_000_000.0,
            }
        }
        try:
            now = IST.localize(datetime(2026, 9, 18, 21, 0))
            with patch.object(scanner, "_now_ist", return_value=now), \
                    patch.object(scanner, "_batch_download",
                                 return_value={"NRBBEARING": new_df}), \
                    patch.object(scanner, "_save_cache_v2"):
                stats = scanner.prefetch_all(["NRBBEARING"])
            self.assertEqual(stats["stale_updated"], 1)
            self.assertEqual(scanner._CACHE["NRBBEARING"]["last_date"], "2026-09-18")
        finally:
            scanner._CACHE = original_cache
            scanner._CACHE_DIRTY = original_dirty


if __name__ == "__main__":
    unittest.main()