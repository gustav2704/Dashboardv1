from types import SimpleNamespace

from app import db
from app import migration_integrity


def _strategy(conn, strategy_id: int, account: str, name: str = "EA") -> int:
    now = db.utcnow()
    conn.execute(
        """INSERT INTO strategies(id,symbol,sqx_name,mql5_name,account_login,origin,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (strategy_id, "XAUUSD", name, name, account, "mt5", now),
    )
    return strategy_id


def _terminal(conn, account: str) -> int:
    return int(conn.execute(
        "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
        (account, f"fixture://{account}", account, "connected", db.utcnow()),
    ).lastrowid)


def _mapping(conn, strategy_id: int, terminal_id: int, account: str, *, role: str, magic: int = 2, name: str = "EA", flexible: int = 0) -> int:
    return int(conn.execute(
        """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                                 role,historical_magic_flexible,confirmed,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (strategy_id, terminal_id, account, "XAUUSD", magic, name, role, flexible, 1, db.utcnow()),
    ).lastrowid)


def test_history_owner_audit_detects_exact_and_unique_flexible_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.db")
    db.init_db()
    with db.session() as conn:
        old_terminal, live_terminal = _terminal(conn, "old"), _terminal(conn, "new")
        _strategy(conn, 1, "new", "EA")
        _strategy(conn, 2, "old", "EA")
        _mapping(conn, 1, old_terminal, "old", role="historical", magic=7, flexible=1)
        _mapping(conn, 2, live_terminal, "old", role="live", magic=99)
        conflicts = migration_integrity.history_ownership_conflicts(conn)
    assert len(conflicts) == 1
    assert conflicts[0]["historical"]["strategy_id"] == 1
    assert conflicts[0]["owner"]["strategy_id"] == 2


def test_axi_repair_is_atomic_and_keeps_live_owners(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "repair.db")
    db.init_db()
    with db.session() as conn:
        old_terminal = _terminal(conn, migration_integrity.AXI_ACCOUNT)
        new_terminal = _terminal(conn, "3000097316")
        for source, owner, name in migration_integrity.AXI_HISTORY_REPAIRS:
            _strategy(conn, source, "3000097316", name)
            _strategy(conn, owner, migration_integrity.AXI_ACCOUNT, name)
            _mapping(conn, source, old_terminal, migration_integrity.AXI_ACCOUNT, role="historical", name=name, flexible=1)
            _mapping(conn, source, new_terminal, "3000097316", role="live", name=name)
            _mapping(conn, owner, old_terminal, migration_integrity.AXI_ACCOUNT, role="live", name=name)
            conn.execute(
                "INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at) VALUES(?,?,?,?,?)",
                (source, migration_integrity.AXI_ACCOUNT, "predecessor", "fixture", db.utcnow()),
            )
        result = migration_integrity.repair_axi_history_ownership(conn)
        assert len(result["repaired"]) == 5
        assert result["post_audit"]["actionable_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM mappings WHERE role='historical'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mappings WHERE role='live'").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM identity_repair_audits WHERE action='detached_parallel_history_owner'").fetchone()[0] == 5


def test_axi_repair_refuses_partial_inventory_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "partial.db")
    db.init_db()
    with db.session() as conn:
        old_terminal = _terminal(conn, migration_integrity.AXI_ACCOUNT)
        new_terminal = _terminal(conn, "3000097316")
        source, owner, name = migration_integrity.AXI_HISTORY_REPAIRS[0]
        _strategy(conn, source, "3000097316", name)
        _strategy(conn, owner, migration_integrity.AXI_ACCOUNT, name)
        _mapping(conn, source, old_terminal, migration_integrity.AXI_ACCOUNT, role="historical", name=name, flexible=1)
        _mapping(conn, source, new_terminal, "3000097316", role="live", name=name)
        _mapping(conn, owner, old_terminal, migration_integrity.AXI_ACCOUNT, role="live", name=name)
        try:
            migration_integrity.repair_axi_history_ownership(conn)
        except ValueError:
            pass
        else:
            raise AssertionError("partial inventory must be rejected")
        assert conn.execute("SELECT COUNT(*) FROM mappings WHERE role='historical'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM identity_repair_audits").fetchone()[0] == 0


def test_migration_validation_blocks_existing_live_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "owner.db")
    db.init_db()
    with db.session() as conn:
        old_terminal = _terminal(conn, "old")
        _strategy(conn, 1, "new", "EA")
        _strategy(conn, 2, "old", "EA")
        _mapping(conn, 2, old_terminal, "old", role="live")
        payload = SimpleNamespace(
            canonical_strategy_id=1, old_account="old", new_account="new", tradebuddy_path=None,
            historical_mappings=[SimpleNamespace(terminal_id=old_terminal, account_login="old", symbol="XAUUSD", magic=2, comment_pattern="EA")],
            live_mapping=None,
        )
        result = migration_integrity.validate_migration(conn, payload)
    assert not result["valid"]
    assert result["owner_conflicts"][0]["owner"]["strategy_id"] == 2


def test_migration_validation_blocks_overlapping_closed_trade_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "overlap.db")
    db.init_db()
    export = tmp_path / "old.txt"
    export.write_text("1;XAUUSD;0.1;Long;2000;2026.01.01 10:00;2010;2026.01.02 11:00;0;0;10;0;0;2;EA;", encoding="utf-8")
    with db.session() as conn:
        terminal = _terminal(conn, "new")
        _strategy(conn, 1, "new", "EA")
        _mapping(conn, 1, terminal, "new", role="live")
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal, 1, 1, 1767261600000, "XAUUSD", "BUY", "IN", .1, 2000, 0, 2, "EA", "{}"),
                (terminal, 2, 1, 1767351600000, "XAUUSD", "SELL", "OUT", .1, 2010, 10, 2, "EA", "{}"),
            ],
        )
        payload = SimpleNamespace(
            canonical_strategy_id=1, old_account="old", new_account="new", tradebuddy_path=str(export),
            historical_mappings=[SimpleNamespace(terminal_id=None, account_login="old", symbol="XAUUSD", magic=2, comment_pattern="EA")],
            live_mapping=SimpleNamespace(terminal_id=terminal, account_login="new", symbol="XAUUSD", magic=2, comment_pattern="EA"),
        )
        result = migration_integrity.validate_migration(conn, payload)
    assert not result["valid"]
    assert result["overlap"] is not None
