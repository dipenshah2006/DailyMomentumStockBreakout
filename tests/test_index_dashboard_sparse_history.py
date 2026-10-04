import importlib.util
import json
import sys
import types
import unittest

import pandas as pd


def _install_plotly_stubs_when_missing():
    if importlib.util.find_spec("plotly") is not None:
        return

    plotly = types.ModuleType("plotly")
    plotly.__path__ = []

    plotly_io = types.ModuleType("plotly.io")
    plotly_subplots = types.ModuleType("plotly.subplots")
    plotly_subplots.make_subplots = lambda *args, **kwargs: None
    plotly_graph_objects = types.ModuleType("plotly.graph_objects")
    plotly_utils = types.ModuleType("plotly.utils")
    plotly_utils.PlotlyJSONEncoder = json.JSONEncoder
    plotly.io = plotly_io

    sys.modules.update({
        "plotly": plotly,
        "plotly.io": plotly_io,
        "plotly.subplots": plotly_subplots,
        "plotly.graph_objects": plotly_graph_objects,
        "plotly.utils": plotly_utils,
    })


_install_plotly_stubs_when_missing()

from IndexDashBoard import realtime_analysis4 as dashboard


class SparseSyntheticIndexTests(unittest.TestCase):
    def test_one_observation_synthetic_index_does_not_crash_indicator_calculation(self):
        index = pd.DatetimeIndex(["2026-10-01"])
        constituent = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "Volume": [1000],
            },
            index=index,
        )
        synthetic = dashboard.build_synthetic_index(
            ["TEST"],
            {"TEST.NS": constituent},
        )

        self.assertEqual(len(synthetic), 1)
        indicators = dashboard.compute_ticker_indicators(
            "test_synthetic",
            synthetic,
            pd.DataFrame(),
            pd.DataFrame(),
        )

        self.assertEqual(indicators["last_price"], 100.0)
        self.assertIsNone(indicators["daily_rsi"])
        self.assertEqual(indicators["daily_rsi_cross"], "sideways")
        self.assertEqual(indicators["daily_macd_34_200_9_cross"], "sideways")

    def test_single_value_indicator_helpers_preserve_series_shape(self):
        close = pd.Series([100.0], index=pd.DatetimeIndex(["2026-10-01"]))

        self.assertIsInstance(dashboard.rsi(close), pd.Series)
        self.assertIsInstance(dashboard.sma(close, 14), pd.Series)
        self.assertTrue(all(
            isinstance(value, pd.Series)
            for value in dashboard.macd(close, 12, 26, 9)
        ))


if __name__ == "__main__":
    unittest.main()