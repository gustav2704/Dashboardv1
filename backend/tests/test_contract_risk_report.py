import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("contract_risk_report", ROOT / "scripts" / "build_contract_risk_report.py")
REPORT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(REPORT)


def test_observed_lots_extracts_entry_deals_from_mt5_report():
    result = REPORT.observed_lots(ROOT / "data" / "backtests" / "185" / "report.html")
    assert result["status"] == "observed"
    assert result["count"] > 0
    assert result["min"] > 0
    assert result["max"] >= result["min"]
