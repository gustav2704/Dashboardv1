from app.operational_identity import conflicts, identity, mappings_for_primary


def _mapping(id, terminal, symbol, magic, account="100", role="live", server="Broker-Demo", expert="BotA"):
    return {
        "id": id, "terminal_id": terminal, "account_login": account,
        "symbol": symbol, "magic": magic, "role": role, "confirmed": 1,
        "terminal_server": server, "operational_expert_key": expert,
    }


def test_broker_symbol_aliases_are_one_operational_identity():
    left = _mapping(1, 7, "XAUUSD", 11)
    right = _mapping(2, 7, "XAUUSD.cyr", 11)
    assert identity(left) == identity(right)


def test_terminal_or_magic_does_not_change_identity():
    base = _mapping(1, 7, "NAS100", 11)
    assert identity(base) == identity(_mapping(2, 8, "US100", 11))
    assert identity(base) == identity(_mapping(3, 7, "US100", 12))


def test_server_or_ex5_is_a_different_identity():
    base = _mapping(1, 7, "NAS100", 11)
    assert identity(base) != identity(_mapping(2, 7, "US100", 11, server="Broker-Live"))
    assert identity(base) != identity(_mapping(3, 7, "US100", 11, expert="BotB"))


def test_legacy_conflict_uses_primary_identity_only():
    strategy = {"symbol": "BTCUSD", "account_login": "300"}
    btc = _mapping(1, 7, "BTCUSD", 3, "100", "historical")
    gold = _mapping(2, 7, "XAUUSD", 99, "100", "historical")
    assert mappings_for_primary(strategy, [gold, btc]) == [btc]
    assert conflicts(strategy, [gold, btc]) == []


def test_archive_and_active_terminal_are_one_live_identity():
    strategy = {"symbol": "XAUUSD", "account_login": "400"}
    active = {**_mapping(1, 86, "XAUUSD", 78, "400"), "terminal_data_dir": "C:/terminal/live"}
    archived = {**_mapping(2, 103, "XAUUSD", 78, "400"), "terminal_data_dir": "archive://terminal/86/400"}

    assert identity(active) == identity(archived)
    assert mappings_for_primary(strategy, [archived, active]) == [archived, active]
    assert conflicts(strategy, [archived, active]) == []


def test_same_archive_root_does_not_merge_different_accounts():
    active = {**_mapping(1, 86, "XAUUSD", 78, "400"), "terminal_data_dir": "C:/terminal/live"}
    other = {**_mapping(2, 103, "XAUUSD", 78, "300"), "terminal_data_dir": "archive://terminal/86/300"}

    assert identity(active) != identity(other)
