# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One user: the owner, a solo algorithmic trader who runs a portfolio of MetaTrader 5 Expert Advisors built in StrategyQuant X across several broker accounts (live and demo) on Windows VPS machines. They check the dashboard several times a day and also use it in longer review sessions.

## Product Purpose

EA Observatory (Dashboardv1) is a local web dashboard that compares how each deployed EA actually performs in MT5 against its StrategyQuant X history and backtests. It exists so one person can supervise dozens of bots without opening each terminal.

Success means the user can, in order of priority:
1. **Spot bots to pause.** Quickly see which bots are drifting from their SQX baseline (profit factor, drawdown, loss streaks, trade frequency) and decide whether to stop them.
2. **Run a deep strategy review.** Investigate one strategy in depth: journal, trade history, backtest risk audit, EGT and edge decay, deployment and magic numbers.
3. **Check ops health.** Confirm that VPS terminals, DashboardBridge services, SSH/SFTP syncs and SQX snapshots are alive and the data is fresh.

## Positioning

Each live bot is judged against its own StrategyQuant X baseline, not against generic thresholds alone. Identity is strict (server + account + normalized asset + normalized EX5 name), and the overview only shows strategies with confirmed terminal or VPS-inventory evidence.

## Operating Context

- Data comes from MT5 terminals through the read-only DashboardBridge service, sent locally or over SSH/SFTP from VPS machines. The data refreshes every 5 minutes, and a terminal counts as disconnected after 10 minutes with no response.
- StrategyQuant X syncs are read-only and run only while SQX is open. The dashboard keeps the snapshots afterwards.
- GuardianCuenta is an account-level risk EA. The dashboard reads its CSV files over SFTP: interventions, positions under watch, and the counterfactual impact of each intervention.
- The app runs locally at `http://127.0.0.1:8765`. The backend is FastAPI with SQLite; the frontend is React, Vite and TypeScript, using Recharts, lightweight-charts and lucide-react.
- Main sections: Operational status (overview), Ranking, Journal, MT5 events, Guardian, AlgoTAP, Strategy detail, Market trades, Settings.

## Capabilities and Constraints

- Desktop only. Designed for wide screens (body min-width 1180px); mobile layouts are not required.
- Light and dark themes are both required.
- The data path is read-only. The bridge and SQX sync never send, modify or close orders.
- Labels are in a mix of Spanish and English. A language standard is undecided.
- Vocabulary: EA, bot, strategy, magic number (MN), SQX, databank, OOS/IS, PF (profit factor), DD (drawdown), SQN, EGT, Guardian, VPS, terminal.

## Evidence on Hand

- Real account, trade and strategy data live in `data/dashboard.db` and the `data/` snapshots. They are private and must never appear in public material.
- There are no testimonials, customers or external users, and none should be invented.

## Product Principles

1. The baseline is the reference. Every live metric is read against its SQX or backtest counterpart.
2. Attention first. Bots that need a decision (pause, review) surface before healthy ones.
3. Data freshness is visible. Stale terminals and syncs are never mistaken for healthy ones.
4. Evidence over inference. Only confirmed identities feed metrics; anything uncertain is labeled as uncertain.
5. Dense but scannable. One expert user who prefers information density over hand-holding.
