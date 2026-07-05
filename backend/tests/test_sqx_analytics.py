import importlib.util

from app.config import SQX_EXTRACTOR


def _extractor():
    spec = importlib.util.spec_from_file_location("sqx_extract_test", SQX_EXTRACTOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeEgtClient:
    def __init__(self, module, fail=False):
        self.module = module
        self.fail = fail

    def egt_monthly_slopes(self, settings):
        if self.fail:
            raise self.module.SQXError("history missing")
        return {
            "monthlySlopes": [
                {"ym": "2024-01", "slope": 1.0},
                {"ym": "2024-02", "slope": -1.0},
                {"ym": "2024-03", "slope": 0.5},
            ]
        }


def _settings():
    return {
        "lastSettingsXml": """
          <Settings><Data><Setups>
            <Setup dateFrom="2024.01.01" dateTo="2024.03.31" session="No Session">
              <Chart symbol="TEST" timeframe="H1" />
            </Setup>
          </Setups></Data></Settings>
        """
    }


def _orders():
    return [
        {"CloseTime": "2024-01-10T12:00:00", "Type": "Buy", "ProfitLoss": 100},
        {"CloseTime": "2024-02-10T12:00:00", "Type": "Buy", "ProfitLoss": 50},
        {"CloseTime": "2024-03-10T12:00:00", "Type": "Buy", "ProfitLoss": -25},
        {"CloseTime": "2024-01-11T12:00:00", "Type": "Sell", "ProfitLoss": -20},
        {"CloseTime": "2024-02-11T12:00:00", "Type": "Sell", "ProfitLoss": 80},
    ]


def test_egt_matches_installed_plugin_formula_fixture():
    module = _extractor()

    result = module.calculate_egt(FakeEgtClient(module), _settings(), _orders())

    assert result["available"] is True
    assert result["buy"] == 5.583333333333334
    assert result["sell"] == 3.375
    assert result["total"] == 4.7
    assert result["grade"] == "Adaptativo"
    assert result["n_buy"] == 3
    assert result["n_sell"] == 2
    assert result["months"] == 3
    assert result["source"] == "sqx_bridge"
    assert result["history_source"] == "EGTHistoryBridge"


def test_egt_bridge_failure_is_an_unavailable_result():
    module = _extractor()

    result = module.calculate_egt(
        FakeEgtClient(module, fail=True),
        _settings(),
        _orders(),
    )

    assert result["available"] is False
    assert result["reason"] == "Bridge no disponible"


def test_egt_uses_csv_fallback_when_bridge_fails(tmp_path, monkeypatch):
    module = _extractor()
    history_dir = tmp_path / "egt_history"
    history_dir.mkdir()
    csv_path = history_dir / "NAQ.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Date,Time,Open,High,Low,Close,Volume",
                "2024.01.01,00:00,1,1,1,1,1",
                "2024.01.02,00:00,2,2,2,2,1",
                "2024.02.01,00:00,2,2,2,2,1",
                "2024.02.02,00:00,1,1,1,1,1",
                "2024.03.01,00:00,1,1,1,1,1",
                "2024.03.02,00:00,3,3,3,3,1",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "DEFAULT_EGT_HISTORY_DIR", history_dir)

    result = module.calculate_egt(
        FakeEgtClient(module, fail=True),
        {
            "lastSettingsXml": """
              <Settings><Data><Setups>
                <Setup dateFrom="2024.01.01" dateTo="2024.03.31" session="No Session">
                  <Chart symbol="USATECHIDXUSD_clonedwnx" timeframe="H1" />
                </Setup>
              </Setups></Data></Settings>
            """
        },
        _orders(),
    )

    assert result["available"] is True
    assert result["source"] == "csv_fallback"
    assert result["history_source"] == "CSV fallback"
    assert result["source_file"].endswith("NAQ.csv")
    assert result["bars"] == 6
    assert result["months"] == 3
    assert result["total"] == 4.7


def test_edge_defaults_match_verified_sqx_strategy_fixture():
    module = _extractor()
    stats = {
        "is": {"ProfitFactor": 1.3700000047683716},
        "oos": {
            "ProfitFactor": 1.4299999475479126,
            "NetProfit": 8595.2998046875,
            "AvgTrade": 21.926799774169922,
            "Stability": 0.6299999952316284,
            "WinningPct": 47.70000076293945,
            "ReturnDDRatio": 4.059999942779541,
            "PctDrawdown": 2.069999933242798,
            "SharpeRatio": 1.409999966621399,
            "NumberOfTrades": 392,
        },
    }
    result = module.calculate_edge_score(
        stats,
        [{"year": 2025, "xs": 0.6144826772202294, "is_oos": True}],
        module.DEFAULT_EDGE_CONFIG,
        "default",
    )

    assert result["score"] == 76
    assert result["grade"] == "B"


def test_edge_without_oos_trades_is_unavailable():
    module = _extractor()

    result = module.calculate_edge_score(
        {"is": {}, "oos": {"NumberOfTrades": 0}},
        [],
        module.DEFAULT_EDGE_CONFIG,
        "default",
    )

    assert result == {
        "available": False,
        "reason": "Sin trades OOS",
        "config_source": "default",
    }
