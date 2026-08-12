from app.sqx_doctor import analyze


def _orders(count=240, profit=45):
    return [
        {
            "ProfitLoss": profit if index % 4 else -25,
            "OpenTime": f"2025-01-{index % 28 + 1:02d}T08:00:00",
            "CloseTime": f"2025-01-{index % 28 + 1:02d}T12:00:00",
            "Direction": "Buy" if index % 2 else "Sell",
            "Balance": 10_000,
            "MFE": 12 if index % 7 == 0 else 0,
        }
        for index in range(count)
    ]


def test_doctor_returns_reproducible_summary_for_complete_orders():
    first = analyze(_orders(), "Retester|Results|Doctor fixture")
    second = analyze(_orders(), "Retester|Results|Doctor fixture")

    assert first["available"] is True
    assert first["data_quality"] == "ALTA"
    assert first["score"] == second["score"]
    assert first["verdict"] in {"ACEPTABLE", "REPARABLE", "NO ACEPTABLE"}


def test_doctor_marks_small_or_incomplete_samples_without_failing():
    result = analyze([{"ProfitLoss": -20}], "small")

    assert result["available"] is True
    assert result["data_quality"] == "BAJA"
    assert result["verdict"] == "DATOS INSUFICIENTES"
    assert any(issue["title"] == "Muestra pequeña" for issue in result["issues"])


def test_doctor_rejects_missing_orders():
    assert analyze([], "empty") == {"available": False, "reason": "Sin órdenes SQX"}
