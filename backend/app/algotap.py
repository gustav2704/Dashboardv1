"""On-demand copy of account and magic KPIs from AlgoTAP Analyzer.

AlgoTAP exposes an undocumented JSON API (``api.algotap-analyzer.com``) whose
user endpoints authenticate with the session cookie set by ``/auth/v2/login``.
The dashboard never polls it: a sync only happens when the user presses the
button, and the last successful result is cached on disk so the panel can be
shown without touching the remote service.

Credentials are read from the environment or from ``data/algotap.env``
(git-ignored).  Either ``ALGOTAP_USER`` + ``ALGOTAP_PASSWORD`` or an
``ALGOTAP_REFRESH_TOKEN`` (for Google-only accounts) must be configured.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import DATA_DIR

API_BASE = os.environ.get("ALGOTAP_API_BASE", "https://api.algotap-analyzer.com")
CREDENTIALS_FILE = DATA_DIR / "algotap.env"
SNAPSHOT_FILE = DATA_DIR / "algotap_snapshot.json"
REQUEST_TIMEOUT_SECONDS = 20.0
USER_AGENT = "Dashboardv1-AlgoTAP-sync/1.0"

MISSING_CREDENTIALS = (
    "Faltan credenciales de AlgoTAP. Define ALGOTAP_USER y ALGOTAP_PASSWORD "
    "(o ALGOTAP_REFRESH_TOKEN) en data/algotap.env."
)

_sync_lock = threading.Lock()


class AlgoTapError(RuntimeError):
    """A sync failure whose message is safe to show in the UI."""


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_credentials(env: dict[str, str] | None = None, path: Path | None = None) -> dict[str, str]:
    """Environment variables win over the file so a one-off override is easy."""
    file_values = _read_env_file(path or CREDENTIALS_FILE)
    source = os.environ if env is None else env
    keys = ("ALGOTAP_USER", "ALGOTAP_PASSWORD", "ALGOTAP_REFRESH_TOKEN")
    return {key: (source.get(key) or file_values.get(key) or "") for key in keys}


def credentials_configured(creds: dict[str, str]) -> bool:
    return bool((creds.get("ALGOTAP_USER") and creds.get("ALGOTAP_PASSWORD")) or creds.get("ALGOTAP_REFRESH_TOKEN"))


def _authenticate(client: httpx.Client, creds: dict[str, str]) -> None:
    if creds.get("ALGOTAP_USER") and creds.get("ALGOTAP_PASSWORD"):
        response = client.post(
            "/auth/v2/login",
            json={"email_or_username": creds["ALGOTAP_USER"], "password": creds["ALGOTAP_PASSWORD"]},
        )
    elif creds.get("ALGOTAP_REFRESH_TOKEN"):
        response = client.post("/auth/v2/refresh", json={"refresh_token": creds["ALGOTAP_REFRESH_TOKEN"]})
    else:
        raise AlgoTapError(MISSING_CREDENTIALS)
    if response.status_code in (400, 401, 403):
        raise AlgoTapError("AlgoTAP rechazó el inicio de sesión. Revisa las credenciales en data/algotap.env.")
    if response.status_code >= 400:
        raise AlgoTapError(f"AlgoTAP respondió {response.status_code} al iniciar sesión.")
    # The browser flow relies on the Set-Cookie headers (kept by the client's
    # jar).  If the body also carries a token, send it as a bearer as well.
    try:
        body = response.json()
    except ValueError:
        body = {}
    token = body.get("access_token") if isinstance(body, dict) else None
    if token:
        client.headers["Authorization"] = f"Bearer {token}"


def _get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    response = client.get(path, params=params)
    if response.status_code == 401:
        raise AlgoTapError("La sesión de AlgoTAP no es válida (401). Revisa las credenciales.")
    response.raise_for_status()
    return response.json()


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _account_row(card: dict[str, Any], metrics: dict[str, Any] | None, live: dict[str, Any] | None = None) -> dict[str, Any]:
    # cards-summary's "balance" is the account's *initial* balance; the live
    # balance/equity come from /accounts/{id}/snapshot.
    metrics = metrics or {}
    live = live or {}
    pick = lambda key: _num(metrics.get(key)) if metrics.get(key) is not None else _num(card.get(key))  # noqa: E731
    return {
        "account_id": str(card.get("account_id") or ""),
        "display_name": card.get("display_name") or str(card.get("account_id") or ""),
        "broker": card.get("broker"),
        "server": card.get("server"),
        "platform": card.get("platform"),
        "is_demo": bool(card.get("is_demo")),
        "initial_balance": _num(card.get("balance")),
        "balance": _num(live.get("balance")),
        "equity": _num(live.get("equity")) if live.get("equity") is not None else _num(metrics.get("equity")),
        "margin_level": _num(live.get("margin_level")),
        "live_at": live.get("timestamp"),
        "floating_total": _num(card.get("floating_total")),
        "current_month_profit": _num(card.get("current_month_profit")),
        "total_profit": pick("total_profit"),
        "total_trades": pick("total_trades"),
        "winrate": pick("winrate"),
        "profit_factor": pick("profit_factor"),
        "sharpe": pick("sharpe"),
        "max_drawdown": pick("max_drawdown"),
        "max_drawdown_pct": pick("max_drawdown_pct"),
        "avg_win": pick("avg_win"),
        "avg_loss": pick("avg_loss"),
        "avg_trade": _num(metrics.get("avg_trade")),
        "max_consec_wins": _num(metrics.get("max_consec_wins")),
        "max_consec_losses": _num(metrics.get("max_consec_losses")),
        "metric_confidence": metrics.get("metric_confidence"),
        "ea_count": _num(card.get("ea_count")),
        "open_positions_count": _num(card.get("open_positions_count")),
        "status_ok": _num(card.get("status_ok")),
        "status_vigilancia": _num(card.get("status_vigilancia")),
        "status_alerta": _num(card.get("status_alerta")),
    }


def _magic_row(account_id: str, score: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "magic": score.get("magic"),
        "grade": score.get("grade"),
        "score": _num(score.get("score")),
        "trade_count": _num(score.get("trade_count")),
        "profit_factor": _num(score.get("profit_factor")),
        "sharpe": _num(score.get("sharpe")),
        "winrate": _num(score.get("winrate")),
        "ret_dd": _num(score.get("ret_dd")),
        "max_dd_pct": _num(score.get("max_dd_pct")),
        "stability": _num(score.get("stability")),
        "pillar_profit": _num(score.get("pillar_profit")),
        "pillar_risk": _num(score.get("pillar_risk")),
        "pillar_consistency": _num(score.get("pillar_consistency")),
        "computed_at": score.get("computed_at"),
    }


def _egt_row(account_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """AlgoTAP's incubator EGT: overall score (-30..30) + grade, and its trend."""
    try:
        magic = int(item.get("magic"))
    except (TypeError, ValueError):
        return None
    return {
        "account_id": account_id,
        "magic": magic,
        "egt_overall_score": _num(item.get("egt_overall_score")),
        "egt_grade": item.get("egt_grade"),
        "trend_direction": item.get("trend_direction"),
        "trend_slope": _num(item.get("trend_slope")),
    }


def fetch_snapshot(creds: dict[str, str], client_factory: Callable[[], httpx.Client] | None = None) -> dict[str, Any]:
    """Log in, download the KPIs and normalise them.  Raises AlgoTapError."""
    if not credentials_configured(creds):
        raise AlgoTapError(MISSING_CREDENTIALS)
    factory = client_factory or (lambda: httpx.Client(
        base_url=API_BASE,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ))
    warnings: list[str] = []
    try:
        with factory() as client:
            _authenticate(client, creds)
            cards = _get(client, "/v2/accounts/cards-summary")
            if not isinstance(cards, list):
                raise AlgoTapError("Respuesta inesperada de AlgoTAP en cards-summary.")
            accounts: list[dict[str, Any]] = []
            magics: list[dict[str, Any]] = []
            egt: list[dict[str, Any]] = []
            for card in cards:
                if not isinstance(card, dict) or card.get("is_hidden"):
                    continue
                account_id = str(card.get("account_id") or "")
                metrics = None
                try:
                    metrics = _get(
                        client,
                        f"/v2/metatrader/accounts/{account_id}/metrics/global",
                        {"is_demo": str(bool(card.get("is_demo"))).lower()},
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"{account_id}: métricas globales no disponibles ({type(exc).__name__})")
                live = None
                try:
                    live = (_get(client, f"/accounts/{account_id}/snapshot") or {}).get("snapshot")
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    warnings.append(f"{account_id}: balance en vivo no disponible ({type(exc).__name__})")
                accounts.append(_account_row(
                    card,
                    metrics if isinstance(metrics, dict) else None,
                    live if isinstance(live, dict) else None,
                ))
                try:
                    scores = _get(client, f"/v2/accounts/{account_id}/magics/score/bulk")
                    if isinstance(scores, list):
                        magics.extend(_magic_row(account_id, s) for s in scores if isinstance(s, dict))
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"{account_id}: scores por magic no disponibles ({type(exc).__name__})")
                try:
                    body = _get(client, f"/incubadora/accounts/{account_id}/magics/egt/bulk")
                    items = body.get("items") if isinstance(body, dict) else None
                    if isinstance(items, list):
                        egt.extend(row for row in (_egt_row(account_id, i) for i in items if isinstance(i, dict)) if row)
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"{account_id}: EGT por magic no disponible ({type(exc).__name__})")
    except httpx.TimeoutException as exc:
        raise AlgoTapError("AlgoTAP no respondió a tiempo. Inténtalo de nuevo más tarde.") from exc
    except httpx.HTTPStatusError as exc:
        raise AlgoTapError(f"AlgoTAP respondió {exc.response.status_code}.") from exc
    except httpx.HTTPError as exc:
        raise AlgoTapError(f"No se pudo conectar con AlgoTAP ({type(exc).__name__}).") from exc
    return {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "magics": magics,
        "egt": egt,
        "warnings": warnings,
    }


def read_cached(path: Path | None = None) -> dict[str, Any] | None:
    try:
        return json.loads((path or SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(path: Path | None = None) -> dict[str, Any]:
    snapshot = read_cached(path)
    return {
        "configured": credentials_configured(load_credentials()),
        "snapshot": snapshot,
    }


def sync(path: Path | None = None, creds: dict[str, str] | None = None, client_factory: Callable[[], httpx.Client] | None = None) -> dict[str, Any]:
    """Run one sync; concurrent clicks are rejected instead of queued."""
    if not _sync_lock.acquire(blocking=False):
        raise AlgoTapError("Ya hay una sincronización con AlgoTAP en curso.")
    try:
        snapshot = fetch_snapshot(creds if creds is not None else load_credentials(), client_factory)
        target = path or SNAPSHOT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
        return snapshot
    finally:
        _sync_lock.release()
