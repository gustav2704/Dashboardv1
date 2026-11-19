from __future__ import annotations

import json

import httpx
import pytest

from app import algotap

CREDS = {"ALGOTAP_USER": "user", "ALGOTAP_PASSWORD": "secret", "ALGOTAP_REFRESH_TOKEN": ""}


def make_factory(handler):
    return lambda: httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))


def happy_handler(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/auth/v2/login":
            assert json.loads(request.content) == {"email_or_username": "user", "password": "secret"}
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "session=abc; Path=/"})
        assert request.headers.get("cookie") == "session=abc"
        if path == "/v2/accounts/cards-summary":
            return httpx.Response(200, json=[
                {"account_id": "111", "display_name": "Demo", "is_demo": True, "balance": 1000, "profit_factor": 1.1, "winrate": 50},
                {"account_id": "222", "display_name": "Oculta", "is_hidden": True},
            ])
        if path == "/v2/metatrader/accounts/111/metrics/global":
            assert request.url.params["is_demo"] == "true"
            return httpx.Response(200, json={"profit_factor": 1.4, "equity": 1010, "max_consec_losses": 5})
        if path == "/accounts/111/snapshot":
            return httpx.Response(200, json={"snapshot": {"balance": 950, "equity": 940, "margin_level": 300, "timestamp": "2026-11-19T00:00:00+00:00"}})
        if path == "/v2/accounts/111/magics/score/bulk":
            return httpx.Response(200, json=[{"magic": 777, "grade": "A", "score": 81.5, "trade_count": 40}])
        if path == "/incubadora/accounts/111/magics/egt/bulk":
            return httpx.Response(200, json={"count": 2, "items": [
                {"magic": "777", "egt_overall_score": 12.5, "egt_grade": "BUENO", "trend_direction": "IMPROVING", "trend_slope": 1.2},
                {"magic": "not-a-number", "egt_overall_score": 1},
            ]})
        return httpx.Response(404)
    return handler


def test_sync_writes_normalized_snapshot(tmp_path):
    seen: list[str] = []
    target = tmp_path / "snap.json"
    snapshot = algotap.sync(target, CREDS, make_factory(happy_handler(seen)))

    assert seen[0] == "POST /auth/v2/login"
    assert [a["account_id"] for a in snapshot["accounts"]] == ["111"]  # hidden skipped
    account = snapshot["accounts"][0]
    assert account["profit_factor"] == 1.4  # global metrics win over card
    assert account["winrate"] == 50  # falls back to card
    assert account["initial_balance"] == 1000  # card "balance" is the initial one
    assert account["balance"] == 950 and account["equity"] == 940  # live snapshot wins
    assert snapshot["magics"][0]["magic"] == 777 and snapshot["magics"][0]["grade"] == "A"
    # magic arrives as a string; invalid magics are dropped
    assert snapshot["egt"] == [{"account_id": "111", "magic": 777, "egt_overall_score": 12.5, "egt_grade": "BUENO", "trend_direction": "IMPROVING", "trend_slope": 1.2}]
    assert snapshot["warnings"] == []
    assert json.loads(target.read_text(encoding="utf-8"))["accounts"][0]["account_id"] == "111"
    assert algotap.read_cached(target)["synced_at"] == snapshot["synced_at"]


def test_rejected_login_is_reported_and_cache_untouched(tmp_path):
    target = tmp_path / "snap.json"
    target.write_text('{"accounts": []}', encoding="utf-8")
    factory = make_factory(lambda request: httpx.Response(401, json={"detail": "bad"}))
    with pytest.raises(algotap.AlgoTapError, match="rechazó"):
        algotap.sync(target, CREDS, factory)
    assert target.read_text(encoding="utf-8") == '{"accounts": []}'


def test_missing_credentials():
    with pytest.raises(algotap.AlgoTapError, match="Faltan credenciales"):
        algotap.fetch_snapshot({"ALGOTAP_USER": "", "ALGOTAP_PASSWORD": "", "ALGOTAP_REFRESH_TOKEN": ""})


def test_refresh_token_flow_and_partial_failure(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/v2/refresh":
            assert json.loads(request.content) == {"refresh_token": "rt"}
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers.get("authorization") == "Bearer tok"
        if path == "/v2/accounts/cards-summary":
            return httpx.Response(200, json=[{"account_id": "111", "is_demo": False, "sharpe": 0.9}])
        if path.endswith("/metrics/global"):
            return httpx.Response(500)
        if path.endswith("/score/bulk"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    snapshot = algotap.sync(tmp_path / "s.json", {"ALGOTAP_REFRESH_TOKEN": "rt"}, make_factory(handler))
    assert snapshot["accounts"][0]["sharpe"] == 0.9
    # metrics/global 500, snapshot 404 and EGT 404 are reported, the sync still succeeds
    assert len(snapshot["warnings"]) == 3 and all("111" in w for w in snapshot["warnings"])


def test_credentials_file_and_env_precedence(tmp_path):
    env_file = tmp_path / "algotap.env"
    env_file.write_text("# comment\nALGOTAP_USER=file_user\nALGOTAP_PASSWORD='file pw'\n", encoding="utf-8")
    creds = algotap.load_credentials({"ALGOTAP_USER": "env_user"}, env_file)
    assert creds["ALGOTAP_USER"] == "env_user"
    assert creds["ALGOTAP_PASSWORD"] == "file pw"
    assert algotap.credentials_configured(creds)
