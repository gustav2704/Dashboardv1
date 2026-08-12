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


def test_multiplier_calibrates_lot_sizing_without_reducing_declared_money_risk():
    result = audit_report({
        "configuration": {"inputs": {
            "UseMoneyManagement": "true", "mmRiskedMoney": "200", "mmMultiplier": "0.1",
        }},
        "metrics": {"worst_trade": -219.85},
    })
    assert result["mm_multiplier"] == 0.1
    assert result["declared_risk"] == 200
    assert result["risk_ratio"] == 1.09925
    assert result["status"] == "validated"


def test_no_money_management_is_not_claimed_as_fixed_risk():
    result = audit_report({"configuration": {"inputs": {}}, "metrics": {"worst_trade": -1000}})
    assert result["status"] == "no_mm"
    assert result["no_mm_reasons"] == ["UseMoneyManagement=false"]


def test_minimum_lot_entries_warn_even_when_money_management_is_enabled():
    result = audit_report({
        "configuration": {"inputs": {"UseMoneyManagement": "true", "mmRiskedMoney": "200"}},
        "metrics": {"worst_trade": -100, "executed_entry_count": 3, "all_entries_min_lot": True},
    })
    assert result["status"] == "validated"
    assert result["no_mm_reasons"] == ["All executed entries used 0.01 lots"]


def test_no_mm_audit_reports_both_reasons_when_applicable():
    result = audit_report({
        "configuration": {"inputs": {"UseMoneyManagement": "false"}},
        "metrics": {"worst_trade": -100, "executed_entry_count": 2, "all_entries_min_lot": True},
    })
    assert result["no_mm_reasons"] == ["UseMoneyManagement=false", "All executed entries used 0.01 lots"]
