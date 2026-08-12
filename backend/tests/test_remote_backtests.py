import hashlib
import json
from pathlib import Path
from threading import Event

from app import db, mt5_backtests, remote_backtests


class BytesBridge:
    def __init__(self, payload=b"compiled-expert"):
        self.payload = payload

    def read_bytes_path(self, path):
        return self.payload


def test_profile_keeps_real_magic_comment_and_inputs():
    payload = "\0".join(
        [
            "path=Experts\\Gold\\Bot.ex5",
            "symbol=XAUUSD",
            "period_type=16385",
            "MyNumber=21136",
            "CustomComment=XAU_Strategy_2_11_36",
            "RiskPercent=0.5",
        ]
    ).encode("utf-16le")

    profile = remote_backtests._profile(payload)

    assert profile["expert_name"] == "Bot.ex5"
    assert profile["symbol"] == "XAUUSD"
    assert profile["timeframe"] == "H1"
    assert profile["magic"] == 21136
    assert profile["comment"] == "XAU_Strategy_2_11_36"
    assert profile["inputs"]["RiskPercent"] == "0.5"


def test_profile_resolves_chart_unit_and_size_timeframes():
    def profile_for(period_type: str, period_size: str) -> dict:
        payload = "\0".join([
            "path=Experts\\Bot.ex5", "symbol=US30",
            f"period_type={period_type}", f"period_size={period_size}",
            "MagicNumber=7",
        ]).encode("utf-16le")
        return remote_backtests._profile(payload)

    assert profile_for("1", "1")["timeframe"] == "H1"
    assert profile_for("0", "15")["timeframe"] == "M15"


def test_safety_preflight_blocks_live_positions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "remote.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(
                 id,name,data_dir,account_login,server,status,last_seen,last_sync,transport,
                 ssh_host,ssh_port,ssh_username,ssh_key_path,ssh_host_fingerprint,remote_data_dir,created_at
               ) VALUES(210,'Darwinex Demo','ssh://demo','3000097316','Darwinex-Demo',
                 'connected',?,?, 'ssh','host',22,'Administrator','key','SHA256:key','C:\\demo',?)""",
            (now, now, now),
        ).lastrowid
        live_id = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,server,status,last_seen,last_sync,transport,
                 ssh_host,ssh_port,ssh_username,ssh_key_path,ssh_host_fingerprint,remote_data_dir,created_at
               ) VALUES('Darwinex Live','ssh://live','4000094894','Darwinex-Live',
                 'connected',?,?, 'ssh','host',22,'Administrator','key','SHA256:key','C:\\live',?)""",
            (now, now, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO positions(
                 terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
                 open_price,current_price,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (live_id, 1, 1, "XAUUSD", "Long", 1, 0.1, 1, 1, 7, "bot", "{}"),
        )

    safe, reason = remote_backtests.safety_preflight()

    assert terminal_id == 210
    assert not safe
    assert "open position" in reason


def test_audit_requires_exact_profile_and_symbol_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.db")
    db.init_db()
    now = db.utcnow()
    expert = b"exact-build"
    with db.session() as conn:
        conn.execute(
            """INSERT INTO terminals(id,name,data_dir,account_login,server,status,transport,
                 ssh_host,ssh_port,ssh_username,ssh_key_path,ssh_host_fingerprint,remote_data_dir,created_at)
               VALUES(210,'Demo','ssh://demo','3000097316','Darwinex-Demo','disconnected','ssh',
                 'host',22,'Administrator','key','SHA256:key','C:\\demo',?)""",
            (now,),
        )
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES('US30','Bot','3000097316','mt5',?)",
            (now,),
        ).lastrowid
        import_id = conn.execute(
            """INSERT INTO bot_inventory_imports(source_path,source_hash,status,row_count,imported_at)
               VALUES('fixture','fixture','imported',1,?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO bot_deployments(
                 account_login,server,chart_id,symbol,timeframe,ex5_name,normalized_ex5,magic,
                 expected_comment,strategy_id,confirmed,observed_at,source_import_id,created_at,updated_at
               ) VALUES('3000097316','Darwinex-Demo','1','US30','H1','Bot.ex5','bot',51221,
                 'US30_H1_51221_opt',?,1,?,?,?,?)""",
            (strategy_id, now, import_id, now, now),
        )
        conn.execute(
            "UPDATE symbol_mappings SET target_symbol='WS30' WHERE broker='Darwinex' AND source_symbol='US30'"
        )
    terminal = {
        "id": 210, "source_terminal_id": 210, "account_login": "3000097316",
        "ssh_host": "host", "remote_data_dir": r"C:\demo",
    }
    monkeypatch.setattr(remote_backtests, "_terminal_sources", lambda: {"3000097316": [terminal]})
    monkeypatch.setattr(
        remote_backtests,
        "_source_artifacts",
        lambda _, expert_names=None: {
            "bridge": BytesBridge(expert),
            "expert_paths": [r"C:\demo\MQL5\Experts\Bot.ex5"],
            "expert_hashes": {r"C:\demo\MQL5\Experts\Bot.ex5": hashlib.sha256(expert).hexdigest()},
            "profiles": [{
                "expert_name": "Bot.ex5", "symbol": "US30", "timeframe": "H1",
                "magic": 51221, "comment": "US30_H1_51221_opt", "inputs": {"MagicNumber": "51221"},
                "remote_profile_path": r"C:\demo\MQL5\Profiles\Charts\chart.chr",
            }],
            "available_symbols": {"ws30": "WS30"},
        },
    )

    audit = remote_backtests.audit_candidates()

    assert audit["counts"] == {"eligible": 1, "blocked": 0}
    candidate = audit["candidates"][0]
    assert candidate["expert_hash"] == hashlib.sha256(expert).hexdigest()
    assert candidate["target_symbol"] == "WS30"
    assert candidate["magic"] == 51221


def test_execute_downloads_and_verifies_manifest(tmp_path, monkeypatch):
    report = b"<html>fixture report</html>"
    log = b"tester log"
    manifest = [
        {"name": "DashboardBacktest-7.htm", "bytes": len(report), "sha256": hashlib.sha256(report).hexdigest()},
        {"name": "tester.log", "bytes": len(log), "sha256": hashlib.sha256(log).hexdigest()},
    ]
    remote_files = {
        r"C:\MT5Backtest\Jobs\7\DashboardBacktest-7.htm": report,
        r"C:\MT5Backtest\Jobs\7\tester.log": log,
        r"C:\MT5Backtest\Jobs\7\manifest.json": json.dumps(manifest).encode(),
    }

    class FakeBridge:
        def __init__(self, terminal):
            self.writes = {}

        def write_bytes_path(self, path, payload):
            self.writes[path] = payload

        def execute(self, command, **kwargs):
            return {"status": 0, "output": '{"exit_code":0}\n', "error": ""}

        def list_files(self, root, suffixes=()):
            return list(remote_files)

        def download_path(self, remote_path, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(remote_files[remote_path])

    expert_path = tmp_path / "Bot.ex5"
    expert_path.write_bytes(b"expert")
    monkeypatch.setattr(remote_backtests, "SSHBridge", FakeBridge)
    monkeypatch.setattr(remote_backtests, "_target_terminal", lambda: {"id": 210})
    monkeypatch.setattr(remote_backtests, "status", lambda: {
        "runner_exists": True, "free_bytes": 10 * 1024**3, "minimum_free_bytes": 5 * 1024**3,
    })
    monkeypatch.setattr(remote_backtests, "safety_preflight", lambda: (True, ""))
    run = {
        "id": 7, "expert_path": str(expert_path), "expert_hash": hashlib.sha256(b"expert").hexdigest(),
        "symbol": "WS30", "timeframe": "H1", "from_date": "2016-01-01", "to_date": "2024-12-31",
        "deposit": 100000, "currency": "USD", "leverage": "1:100", "inputs": {"MagicNumber": "7"},
    }

    result = remote_backtests.execute_run(run, tmp_path / "run", Event())

    assert result["report_path"].read_bytes() == report
    assert result["remote_run_dir"].endswith(r"Jobs\7")
    assert result["manifest"] == manifest


def test_remote_run_persists_execution_target_and_ssh_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "run.db")
    db.init_db()
    now = db.utcnow()
    expert = tmp_path / "Bot.ex5"
    expert.write_bytes(b"expert")
    with db.session() as conn:
        conn.execute(
            """INSERT INTO terminals(id,name,data_dir,status,created_at)
               VALUES(210,'Remote Demo','ssh://demo','disconnected',?)""",
            (now,),
        )
        strategy_id = conn.execute(
            "INSERT INTO strategies(sqx_name,created_at) VALUES('Bot',?)", (now,)
        ).lastrowid
    run_id = mt5_backtests._insert_run({
        "strategy_id": strategy_id, "broker": "Darwinex", "execution_target": "ssh_darwinex_demo",
        "remote_terminal_id": 210, "expert_path": str(expert), "symbol": "XAUUSD",
        "timeframe": "H1", "from_date": "2016-01-01", "to_date": "2024-12-31",
        "deposit": 100000, "currency": "USD", "leverage": "1:100", "model": 1,
        "inputs": {}, "config_source": "test", "config_snapshot": {},
    })

    run = mt5_backtests._run_row(run_id)

    assert run["execution_target"] == "ssh_darwinex_demo"
    assert run["terminal_id"] == 210
