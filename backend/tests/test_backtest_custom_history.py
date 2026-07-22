from datetime import date

from app import backtest_batches


def test_custom_history_end_uses_last_available_year(tmp_path, monkeypatch):
    data_dir = tmp_path / "tester"
    history = data_dir / "bases" / "Custom" / "history" / "WS30_DK_2016"
    history.mkdir(parents=True)
    (history / "2016.hcc").write_bytes(b"")
    (history / "2025.hcc").write_bytes(b"")
    monkeypatch.setattr(
        backtest_batches.mt5_backtests,
        "broker_profile",
        lambda broker: {"data_dir": data_dir, "broker": broker},
    )

    assert backtest_batches._custom_history_end("WS30_DK_2016", "Darwinex") == "2025-12-31"


def test_custom_history_end_caps_current_year(tmp_path, monkeypatch):
    data_dir = tmp_path / "tester"
    history = data_dir / "bases" / "Custom" / "history" / "XAUUSD_M1"
    history.mkdir(parents=True)
    (history / f"{date.today().year}.hcc").write_bytes(b"")
    monkeypatch.setattr(
        backtest_batches.mt5_backtests,
        "broker_profile",
        lambda broker: {"data_dir": data_dir, "broker": broker},
    )

    assert backtest_batches._custom_history_end("XAUUSD_M1", "Darwinex") == (date.today() - backtest_batches.timedelta(days=1)).isoformat()


def test_custom_comment_is_limited_to_mt5_order_comment_size():
    comment = "WF_Matrix_Strategy_4_7_21_2_EXTRA"
    assert len(comment[:30]) == 30


def test_stage_expert_uses_requested_broker_data_dir(tmp_path, monkeypatch):
    fpm = tmp_path / "fpm"
    darwinex = tmp_path / "darwinex"
    source = tmp_path / "source.ex5"
    source.write_bytes(b"expert")

    def profile(name):
        return {
            "broker": name,
            "data_dir": darwinex if name == "Darwinex" else fpm,
            "terminal_exe": darwinex / "terminal64.exe",
        }

    monkeypatch.setattr(backtest_batches.mt5_backtests, "broker_profile", profile)
    staged = backtest_batches._stage_expert(source, "a" * 64, "Darwinex")

    assert darwinex / "MQL5" / "Experts" in staged.parents
    assert not (fpm / "MQL5" / "Experts").exists()
