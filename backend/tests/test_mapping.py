from app.mapping import _candidate, normalize, symbol_family, version_signature


def test_name_normalization_handles_sqx_punctuation():
    assert normalize("XAU_B_Ichi-Strategy 3.5.57") == "xaubichistrategy3557"
    assert normalize("WF Matrix - NAQStrategy") == "wfmatrixnaqstrategy"


def test_symbol_family_maps_broker_symbols_to_catalog_symbols():
    assert symbol_family("GER40") == "dax"
    assert symbol_family("US100.cash") == "naq"
    assert symbol_family("XAUUSD.cyr") == "xau"


def test_version_signature_uses_strategy_version_numbers():
    assert version_signature("WF_Matrix_DAXStrategy_1_9_26_3") == (1, 9, 26, 3)
    assert version_signature("WF Matrix - DAXStrategy 1.9.26(3)dwnx") == (1, 9, 26, 3)


def test_candidate_requires_matching_account_and_symbol_family():
    strategy = {"id": 1, "account_login": "7396577", "symbol": "DAX", "sqx_name": "WF Matrix - DAXStrategy 4.8.16dwnx", "mql5_name": ""}
    item = {"account_login": "7396577", "symbol": "GER40", "magic": 4816, "comment": "WF_Matrix_DAXStrategy_4_8_16dw"}
    candidate = _candidate(strategy, item)
    assert candidate and candidate["signature_match"] and candidate["score"] >= 0.9
    assert _candidate(strategy, {**item, "account_login": "7396582"}) is None
