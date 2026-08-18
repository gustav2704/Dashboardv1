# Current VPS MT5 backtest report

## Runner and connections

- **Execution runner:** terminal **210**, isolated Darwinex-Demo, VPS **145.63.135.196**.
- **Source VPS connections:** **145.63.135.196** (Darwinex Live/Demo, Axi, FPM) and **100.107.77.50** (CloudTrading/FPM).
- Backtests execute only on terminal 210; other terminals are source locations for EX5/chart profiles.

## ID 430 retry

- Symbol corrected to **NDX** (not US100).
- Run 410: **completed**; equity curve generated.

## Excluded — not included in the table

- Explicit IDs: `134, 144, 146, 150, 152, 242, 249, 271, 272, 273, 275, 277, 285, 287, 295, 296, 360, 448, 472`.
- Plus every bot in excluded account `10025867`.

## Remaining blocked/eligible bots

| ID | Name | Account | Source VPS | Status | Reason |
|---:|---|---|---|---|---|
| 140 | ORB_American_EMA9 | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 149 | TemaEA_V2 | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 157 | WF_Matrix_Strategy_4_7_21_2 | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | Darwinex Demo has no exact symbol mapping/history for WS30 |
| 246 | MomentA BuyStop | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 248 | GannPullbackBuyStop | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 391 | ULTRADAX_D1 | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 435 | XAUWF Matrix - Strategy 3.13.39dwnx | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 509 | SencillayEstupida | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 515 | USDJPY_M30_1139 | 3000097316 | 100.107.77.50 · CloudTrading SRV - Darwinex Demo (Tailscale) / 145.63.135.196 · Darwinex Demo VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 512 | USDJPY_M15_3_7_47_Real | 4000094894 | 145.63.135.196 · Darwinex Live VPS | eligible | Exact VPS EX5 and chart configuration resolved |
| 513 | US30v4 BUY | 4000094894 | 145.63.135.196 · Darwinex Live VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 514 | XAU_M15_3_69_opt | 4000094894 | 145.63.135.196 · Darwinex Live VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 430 | WF_Matrix_NAQStrategy_1_11_24d | 7396577 | 100.107.77.50 · CloudTrading SRV - FP Markets Demo (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets EURUSD (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets US30 (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets XAU (Tailscale) / 145.63.135.196 · FP Markets Demo 7396577 VPS | eligible | Exact VPS EX5 and chart configuration resolved |
| 431 | XAU_M15_GWF_Opt._Strategy_3_36 | 7396577 | 100.107.77.50 · CloudTrading SRV - FP Markets Demo (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets EURUSD (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets US30 (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets XAU (Tailscale) / 145.63.135.196 · FP Markets Demo 7396577 VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 432 | XAUWF_Matrix_Strategy_3_11_46d | 7396577 | 100.107.77.50 · CloudTrading SRV - FP Markets Demo (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets EURUSD (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets US30 (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets XAU (Tailscale) / 145.63.135.196 · FP Markets Demo 7396577 VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 433 | XAUWF_Matrix_Strategy_3_13_39d | 7396577 | 100.107.77.50 · CloudTrading SRV - FP Markets Demo (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets EURUSD (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets US30 (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets XAU (Tailscale) / 145.63.135.196 · FP Markets Demo 7396577 VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 434 | WF_Matrix_Strategy_5_14_71 | 7396577 | 100.107.77.50 · CloudTrading SRV - FP Markets Demo (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets EURUSD (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets US30 (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets XAU (Tailscale) / 145.63.135.196 · FP Markets Demo 7396577 VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |
| 497 | XAU_7_24_currenthour_1.4.61 | 7396577 | 100.107.77.50 · CloudTrading SRV - FP Markets Demo (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets EURUSD (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets US30 (Tailscale) / 100.107.77.50 · CloudTrading SRV - FP Markets XAU (Tailscale) / 145.63.135.196 · FP Markets Demo 7396577 VPS | blocked | No exact chart profile matches EX5, symbol, Magic and comment |

## Confirmed diagnosis

- The shared VPS DataDir `C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\2F12A47F908C38E1243546707DA9A3E6` is registered and was directly scanned; it contains chart profiles/experts.
- Fixed a discovery bug: shared sibling DataDirs were deduplicated only by host/path, so the first account hid the same VPS directory from Darwinex accounts. Deduplication now includes account/terminal.
- No dashboard record was found for an expert/MN literally named `545`; if 545 refers to a screenshot label, provide its EA filename or MN and I can match it directly.