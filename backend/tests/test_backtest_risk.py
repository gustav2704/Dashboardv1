from app.backtest_risk import audit_report


def test_fixed_money_management_flags_tick_value_scale_error():
    result = audit_report({
        "configuration": {"inputs": {
            "UseMoneyManagement": "true", "mmRiskedMoney": "200", "mmMultiplier": "1",
        }},
        "metrics": {"worst_trade": -1970},
    })
    assert result["status"] == "risk_exceeded"
    assert result["risk_ratio"] == 9.85


def test_fixed_money_management_allows_small_execution_cost_overrun():
    result = audit_report({
        "configuration": {"inputs": {
            "UseMoneyManagement": "true", "mmRiskedMoney": "200", "mmMultiplier": "1",
        }},
        "metrics": {"worst_trade": -205},
    })
    assert result["status"] == "validated"


def test_no_money_management_is_not_claimed_as_fixed_risk():
    result = audit_report({"configuration": {"inputs": {}}, "metrics": {"worst_trade": -1000}})
    assert result["status"] == "no_mm"
