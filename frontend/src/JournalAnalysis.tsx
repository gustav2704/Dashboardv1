import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { JournalAnalysisData, JournalTrade } from "./journalTypes";
import {
  buildAccountAnalytics,
  temporalValue,
  type TemporalBucket,
  type TemporalMetric,
} from "./journalAnalytics";
import "./JournalAnalysis.css";

const GREEN = "#2dd4bf";
const RED = "#fb7185";
const YELLOW = "#fbbf24";
const GRID = "#172633";
const BOT_COLORS = [
  "#38bdf8",
  "#fbbf24",
  "#a78bfa",
  "#fb7185",
  "#34d399",
  "#f97316",
  "#60a5fa",
  "#e879f9",
];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function dayKey(ms: number) {
  const date = new Date(ms);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function monthKey(ms: number) {
  const date = new Date(ms);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}
function maxDrawdown(trades: JournalTrade[]) {
  let equity = 0;
  let peak = 0;
  let drawdown = 0;
  for (const trade of [...trades].sort(
    (left, right) => left.close_time_msc - right.close_time_msc,
  )) {
    equity += trade.net_profit;
    peak = Math.max(peak, equity);
    drawdown = Math.max(drawdown, peak - equity);
  }
  return drawdown;
}
function normalizePf(value: number) {
  return !Number.isFinite(value) ? 1 : value <= 0 ? 0 : Math.min(value, 3) / 3;
}
function scoreColor(score: number) {
  return score >= 60 ? GREEN : score >= 40 ? YELLOW : RED;
}

export type BotRanking = {
  id: number;
  name: string;
  symbol: string;
  primaryMagicNumber: number | null;
  magicNumbers: number[];
  historicalMagicNumbers: number[];
  count: number;
  wins: number;
  net: number;
  volume: number;
  winRate: number;
  pf: number;
  dd: number;
  netPerLot: number | null;
  returnToDd: number;
  score: number;
  efficiencyScore: number;
};
export function buildBotRankings(trades: JournalTrade[]): BotRanking[] {
  const grouped = new Map<number, JournalTrade[]>();
  for (const trade of trades)
    if (trade.strategy_id > 0 && !trade.is_stopped)
      grouped.set(trade.strategy_id, [
        ...(grouped.get(trade.strategy_id) || []),
        trade,
      ]);
  const rows = [...grouped.entries()].map(([id, items]) => {
    const net = items.reduce((sum, trade) => sum + trade.net_profit, 0);
    const grossProfit = items
      .filter((trade) => trade.profit > 0)
      .reduce((sum, trade) => sum + trade.profit, 0);
    const grossLoss = Math.abs(
      items
        .filter((trade) => trade.profit <= 0)
        .reduce((sum, trade) => sum + trade.profit, 0),
    );
    const volume = items.reduce((sum, trade) => sum + trade.volume, 0);
    const drawdown = maxDrawdown(items);
    const magicNumbers = [...new Set(items.flatMap((trade) => trade.magic_numbers || []))].sort((left, right) => left - right);
    const historicalMagicNumbers = [...new Set(items.flatMap((trade) => trade.historical_magic_numbers || []))].sort((left, right) => left - right);
    return {
      id,
      name: items[0].strategy_name,
      symbol: items[0].symbol,
      primaryMagicNumber: items.find((trade) => trade.primary_magic_number != null)?.primary_magic_number ?? null,
      magicNumbers,
      historicalMagicNumbers,
      count: items.length,
      wins: items.filter((trade) => trade.net_profit > 0).length,
      net,
      volume,
      winRate:
        (items.filter((trade) => trade.net_profit > 0).length / items.length) *
        100,
      pf: grossLoss ? grossProfit / grossLoss : grossProfit ? Infinity : 0,
      dd: drawdown,
      netPerLot: volume > 0 ? net / volume : null,
      returnToDd: drawdown > 0 ? net / drawdown : net > 0 ? Infinity : 0,
      score: 0,
      efficiencyScore: 0,
    };
  });
  const maxNet = Math.max(0, ...rows.map((row) => Math.max(row.net, 0))),
    maxDd = Math.max(0, ...rows.map((row) => row.dd)),
    maxTrades = Math.max(0, ...rows.map((row) => row.count));
  for (const row of rows)
    row.score =
      (maxNet ? (Math.max(row.net, 0) / maxNet) * 45 : 0) +
      (row.winRate / 100) * 25 +
      normalizePf(row.pf) * 15 +
      (maxTrades ? (row.count / maxTrades) * 10 : 0) -
      (maxDd ? (row.dd / maxDd) * 20 : 0);
  const eligible = rows.filter(
    (row) => row.count >= 10 && row.netPerLot != null,
  );
  const positive = (values: Array<number | null>, value: number | null) =>
    value === Infinity
      ? 1
      : value == null || value <= 0
        ? 0
        : value /
          Math.max(
            1,
            ...values.filter(
              (item): item is number =>
                item != null && Number.isFinite(item) && item > 0,
            ),
          );
  for (const row of eligible)
    row.efficiencyScore =
      positive(
        eligible.map((item) => item.netPerLot),
        row.netPerLot,
      ) *
        35 +
      positive(
        eligible.map((item) => item.returnToDd),
        row.returnToDd,
      ) *
        30 +
      (row.winRate / 100) * 15 +
      normalizePf(row.pf) * 15 +
      (maxTrades ? (row.count / maxTrades) * 5 : 0);
  return rows;
}

export function buildRemovalCandidates(trades: JournalTrade[]): BotRanking[] {
  return buildBotRankings(trades)
    .filter((row) => row.count >= 10 && row.net < 0)
    .sort((left, right) => left.net - right.net);
}

export type BotEquitySeries = {
  id: number;
  key: string;
  name: string;
  color: string;
};
export type BotEquityPoint = { timestamp: number; label: string } & Record<
  string,
  number | string | undefined
>;
export function buildBotEquityCurves(trades: JournalTrade[]): {
  series: BotEquitySeries[];
  points: BotEquityPoint[];
} {
  const botTrades = trades.filter(
    (trade) => trade.strategy_id > 0 && !trade.is_stopped,
  );
  const grouped = new Map<number, JournalTrade[]>();
  for (const trade of botTrades)
    grouped.set(trade.strategy_id, [
      ...(grouped.get(trade.strategy_id) || []),
      trade,
    ]);
  const series = [...grouped.entries()]
    .sort(([left], [right]) => left - right)
    .map(([id, items]) => ({
      id,
      key: `bot_${id}`,
      name: `${items[0].strategy_name} #${id}`,
      color: BOT_COLORS[Math.abs(id) % BOT_COLORS.length],
    }));
  const balances = new Map(series.map((item) => [item.id, 0]));
  const points: BotEquityPoint[] = [];
  const ordered = [...botTrades].sort(
    (left, right) =>
      left.close_time_msc - right.close_time_msc ||
      left.strategy_id - right.strategy_id,
  );
  for (let index = 0; index < ordered.length; ) {
    const timestamp = ordered[index].close_time_msc;
    const tradedBotIds = new Set<number>();
    while (
      index < ordered.length &&
      ordered[index].close_time_msc === timestamp
    ) {
      const trade = ordered[index];
      balances.set(
        trade.strategy_id,
        (balances.get(trade.strategy_id) || 0) + trade.net_profit,
      );
      tradedBotIds.add(trade.strategy_id);
      index++;
    }
    const point: BotEquityPoint = {
      timestamp,
      label: new Date(timestamp).toLocaleString(),
    };
    for (const item of series)
      if (tradedBotIds.has(item.id)) point[item.key] = balances.get(item.id) || 0;
    points.push(point);
  }
  return { series, points };
}

function ChartTip({
  active,
  payload,
  suffix = "",
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; color: string }>;
  suffix?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="journal-tooltip">
      {payload.map((item) => (
        <div key={item.name}>
          <span>{item.name}</span>
          <strong>
            {item.value.toLocaleString("en-US", { maximumFractionDigits: 2 })}
            {suffix}
          </strong>
        </div>
      ))}
    </div>
  );
}
function strategyIdentifier(row: BotRanking) {
  const active = row.magicNumbers.length ? row.magicNumbers.join(", ") : "No disponible";
  const historical = row.historicalMagicNumbers.length ? row.historicalMagicNumbers.join(", ") : "No disponible";
  return `ID interno: ${row.id}\nMN principal: ${row.primaryMagicNumber ?? "No disponible"}\nMN activos: ${active}\nMN históricos: ${historical}`;
}

function StrategyNameTick({ x = 0, y = 0, payload, rows }: { x?: number; y?: number; payload?: { value?: string; index?: number }; rows: BotRanking[] }) {
  const row = payload?.index == null ? null : rows[payload.index];
  return <g transform={`translate(${x},${y})`}><title>{row ? strategyIdentifier(row) : payload?.value}</title><text dy={4} textAnchor="end" fill="#a8bdca" fontSize={9}>{payload?.value}</text></g>;
}

function RankingTooltip({ active, payload, suffix = "" }: { active?: boolean; payload?: Array<{ name?: string; value?: number; payload?: BotRanking }>; suffix?: string }) {
  const item = payload?.[0];
  const row = item?.payload;
  if (!active || !item || !row) return null;
  return <div className="journal-tooltip ranking-tooltip"><strong>{row.name}</strong>{strategyIdentifier(row).split("\n").map(line => <small key={line}>{line}</small>)}<div><span>{item.name}</span><strong>{item.value?.toLocaleString("en-US", { maximumFractionDigits: 2 })}{suffix}</strong></div></div>;
}

function JournalChart({
  title,
  children,
  wide = false,
  bodyHeight,
}: {
  title: string;
  children: ReactNode;
  wide?: boolean;
  bodyHeight?: number;
}) {
  return (
    <section className={`panel journal-chart${wide ? " wide" : ""}`}>
      <div className="journal-chart-title">{title}</div>
      <div
        className="journal-chart-body"
        style={bodyHeight ? { height: bodyHeight } : undefined}
      >
        {children}
      </div>
    </section>
  );
}

function Calendar({ data }: { data: JournalAnalysisData }) {
  const [view, setView] = useState<"year" | "month">("month"),
    [metric, setMetric] = useState<"amount" | "pct">("amount");
  const [cursor, setCursor] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const appliedScope = useRef("");
  const latestTradeMsc = useMemo(
    () => Math.max(...data.trades.map((trade) => trade.close_time_msc), 0),
    [data.trades],
  );
  const scope = useMemo(
    () => [
      data.range_start_msc,
      data.range_end_msc,
      ...Object.keys(data.balances).sort(),
      latestTradeMsc,
    ].join("|"),
    [data.range_end_msc, data.range_start_msc, data.balances, latestTradeMsc],
  );
  useEffect(() => {
    if (appliedScope.current === scope) return;
    appliedScope.current = scope;
    if (!latestTradeMsc) return;
    const latest = new Date(latestTradeMsc);
    setCursor(new Date(latest.getFullYear(), latest.getMonth(), 1));
  }, [latestTradeMsc, scope]);
  const balance = Object.values(data.balances).reduce(
    (sum, value) => sum + value,
    0,
  );
  const daily = useMemo(() => {
    const map = new Map<string, number>();
    for (const trade of data.trades)
      map.set(
        dayKey(trade.close_time_msc),
        (map.get(dayKey(trade.close_time_msc)) || 0) + trade.net_profit,
      );
    return map;
  }, [data.trades]);
  const valueLabel = (value: number) =>
    metric === "pct"
      ? balance > 0
        ? `${value >= 0 ? "+" : ""}${((value / balance) * 100).toFixed(2)}%`
        : "N/A"
      : `${value >= 0 ? "+ $" : "- $"}${Math.abs(value) >= 1000 ? `${(Math.abs(value) / 1000).toFixed(1)}k` : Math.abs(value).toFixed(0)}`;
  const shift = (delta: number) =>
    setCursor(
      new Date(
        cursor.getFullYear() + (view === "year" ? delta : 0),
        cursor.getMonth() + (view === "month" ? delta : 0),
        1,
      ),
    );
  const yearMonths = Array.from({ length: 12 }, (_, month) => {
    const trades = data.trades.filter((trade) => {
      const date = new Date(trade.close_time_msc);
      return (
        date.getFullYear() === cursor.getFullYear() && date.getMonth() === month
      );
    });
    return {
      month,
      trades: trades.length,
      value: trades.reduce((sum, trade) => sum + trade.net_profit, 0),
    };
  });
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1),
    dayCount = new Date(
      cursor.getFullYear(),
      cursor.getMonth() + 1,
      0,
    ).getDate();
  return (
    <section className="panel journal-calendar">
      <div className="journal-calendar-head">
        <div>
          <span className="eyebrow">DAILY RESULTS</span>
          <h2>Profitability calendar</h2>
        </div>
        <div className="calendar-controls">
          <button
            className={view === "year" ? "active" : ""}
            onClick={() => setView("year")}
          >
            Year
          </button>
          <button
            className={view === "month" ? "active" : ""}
            onClick={() => setView("month")}
          >
            Month
          </button>
          <button onClick={() => shift(-1)}>‹</button>
          <strong>
            {view === "year"
              ? cursor.getFullYear()
              : `${MONTHS[cursor.getMonth()]} ${cursor.getFullYear()}`}
          </strong>
          <button onClick={() => shift(1)}>›</button>
          <button
            className={metric === "amount" ? "active" : ""}
            onClick={() => setMetric("amount")}
          >
            Amount
          </button>
          <button
            className={metric === "pct" ? "active" : ""}
            onClick={() => setMetric("pct")}
          >
            %
          </button>
        </div>
      </div>
      {view === "year" ? (
        <div className="calendar-year-grid">
          {yearMonths.map((item) => (
            <button
              type="button"
              key={item.month}
              className={`calendar-month-card ${item.trades ? (item.value >= 0 ? "positive" : "negative") : ""}`}
              onClick={() => {
                setCursor(new Date(cursor.getFullYear(), item.month, 1));
                setView("month");
              }}
            >
              <span>{MONTHS[item.month]}</span>
              <strong>{item.trades ? valueLabel(item.value) : "—"}</strong>
              {item.trades > 0 && <small>{item.trades} ops</small>}
            </button>
          ))}
        </div>
      ) : (
        <div className="calendar-months month">
          <div className="calendar-month">
            <strong>{MONTHS[cursor.getMonth()]}</strong>
            <div className="calendar-weekdays">
              {["M", "T", "W", "T", "F", "S", "S"].map((day, index) => (
                <span key={index}>{day}</span>
              ))}
            </div>
            <div className="calendar-days">
              {Array.from({ length: (first.getDay() + 6) % 7 }, (_, index) => (
                <span key={`empty-${index}`} />
              ))}
              {Array.from({ length: dayCount }, (_, index) => {
                const date = new Date(
                    cursor.getFullYear(),
                    cursor.getMonth(),
                    index + 1,
                  ),
                  value = daily.get(dayKey(date.getTime())) || 0;
                return (
                  <span
                    key={index}
                    className={
                      value > 0 ? "positive" : value < 0 ? "negative" : ""
                    }
                    title={`${dayKey(date.getTime())}: ${value.toFixed(2)}`}
                  >
                    <small>{index + 1}</small>
                    {value !== 0 && <b>{valueLabel(value)}</b>}
                  </span>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

export function TopTenCharts({
  data,
  accountLabel,
}: {
  data: JournalAnalysisData | null;
  accountLabel: string;
}) {
  const rankings = useMemo(() => buildBotRankings(data?.trades || []), [data]);
  const removalCandidates = useMemo(
    () => buildRemovalCandidates(data?.trades || []),
    [data],
  );
  const eligible = rankings.filter((row) => row.count >= 10);
  const charts = [
    {
      title: `Top 10 mejores bots por eficiencia — ${accountLabel} — orientativo`,
      key: "efficiencyScore" as const,
      rows: eligible
        .filter((row) => row.netPerLot != null)
        .sort((left, right) => right.efficiencyScore - left.efficiencyScore)
        .slice(0, 10),
    },
    {
      title: `Ranking compuesto de bots — ${accountLabel} — orientativo`,
      key: "score" as const,
      rows: [...eligible]
        .sort((left, right) => right.score - left.score)
        .slice(0, 10),
    },
    {
      title: `Top 10 porcentaje ganador por bot — ${accountLabel} — mín. 10 trades`,
      key: "winRate" as const,
      rows: [...eligible]
        .sort(
          (left, right) => left.winRate - right.winRate || right.net - left.net,
        )
        .slice(0, 10),
    },
  ];
  const removalHeight = Math.max(240, removalCandidates.length * 42 + 54);
  return (
    <section className="top-ten-section">
      <div className="top-ten-heading">
        <span className="eyebrow">JOURNAL RANKINGS</span>
        <h2>Todos los Top 10</h2>
      </div>
      <div className="top-ten-grid">
        {charts.map((chart) => (
          <JournalChart title={chart.title} key={chart.key}>
            <ResponsiveContainer>
              <BarChart
                data={chart.rows}
                layout="vertical"
                margin={{ left: 25, right: 22 }}
              >
                <CartesianGrid stroke={GRID} horizontal={false} />
                <XAxis type="number" tick={{ fill: "#74899a", fontSize: 9 }} />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={170}
                  tick={<StrategyNameTick rows={chart.rows} />}
                />
                <Tooltip
                  content={
                    <RankingTooltip suffix={chart.key === "winRate" ? "%" : ""} />
                  }
                />
                <Bar dataKey={chart.key} radius={[0, 3, 3, 0]}>
                  {chart.rows.map((row) => (
                    <Cell
                      key={row.id}
                      fill={
                        chart.key === "winRate"
                          ? GREEN
                          : scoreColor(row[chart.key])
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </JournalChart>
        ))}
      </div>
      <JournalChart
        wide
        title={`Candidatos a retirar — ${accountLabel} — mín. 10 trades y P/L neto negativo`}
        bodyHeight={removalHeight}
      >
        {removalCandidates.length ? (
          <ResponsiveContainer>
            <BarChart data={removalCandidates} layout="vertical" margin={{ left: 25, right: 22 }}>
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" tick={{ fill: "#74899a", fontSize: 9 }} />
              <YAxis type="category" dataKey="name" width={170} tick={<StrategyNameTick rows={removalCandidates} />} />
              <Tooltip content={<RankingTooltip />} />
              <Bar dataKey="net" name="P/L neto" radius={[0, 3, 3, 0]}>
                {removalCandidates.map((row) => <Cell key={row.id} fill={RED} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="removal-candidates-empty">No hay bots con al menos 10 trades y P/L neto negativo en los filtros actuales.</div>
        )}
      </JournalChart>
    </section>
  );
}

const TRADES_PER_PAGE = 50;
function TradeList({ data }: { data: JournalAnalysisData }) {
  const [page, setPage] = useState(0);
  const sortedTrades = useMemo(
    () =>
      [...data.trades].sort(
        (left, right) =>
          right.close_time_msc - left.close_time_msc ||
          right.strategy_id - left.strategy_id,
      ),
    [data.trades],
  );
  const totalPages = Math.max(
      1,
      Math.ceil(sortedTrades.length / TRADES_PER_PAGE),
    ),
    safePage = Math.min(page, totalPages - 1),
    pageTrades = sortedTrades.slice(
      safePage * TRADES_PER_PAGE,
      (safePage + 1) * TRADES_PER_PAGE,
    );
  useEffect(() => setPage(0), [data.trades]);
  return (
    <section className="panel journal-trades">
      <div className="panel-heading journal-trades-heading">
        <div>
          <span className="eyebrow">OPERACIONES FILTRADAS</span>
          <h2>Lista de trades</h2>
        </div>
        <span
          className={
            data.reconciliation.status === "reconciled"
              ? "value-good"
              : "value-bad"
          }
        >
          {sortedTrades.length} operaciones ·{" "}
          {data.reconciliation.attributed_trades} asignadas ·{" "}
          {data.reconciliation.unassigned_trades} sin asignar
        </span>
      </div>
      <div className="table-scroll journal-trades-scroll">
        <table className="journal-trades-table">
          <thead>
            <tr>
              <th>Apertura</th>
              <th>Cierre</th>
              <th>Cuenta</th>
              <th>Activo</th>
              <th>Estrategia</th>
              <th>Magic Number</th>
              <th>ID interno</th>
              <th>Volumen</th>
              <th>P/L neto</th>
            </tr>
          </thead>
          <tbody>
            {pageTrades.map((trade) => (
              <tr
                key={`${trade.strategy_id}-${trade.close_time_msc}-${trade.account}-${trade.net_profit}`}
              >
                <td>
                  {new Date(trade.open_time_msc).toLocaleString("en-GB")}
                </td>
                <td>
                  {new Date(trade.close_time_msc).toLocaleString("en-GB")}
                </td>
                <td>{trade.account || "Sin asignar"}</td>
                <td>{trade.symbol || "-"}</td>
                <td>{trade.strategy_name}</td>
                <td>{trade.magic_number}</td>
                <td>{trade.strategy_id || "\u2014"}</td>
                <td>
                  {trade.volume.toLocaleString("en-US", {
                    maximumFractionDigits: 2,
                  })}
                </td>
                <td
                  className={trade.net_profit >= 0 ? "value-good" : "value-bad"}
                >
                  {trade.net_profit >= 0 ? "+" : ""}
                  {trade.net_profit.toLocaleString("en-US", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="journal-trades-pagination">
          <span>
            Mostrando {safePage * TRADES_PER_PAGE + 1}-
            {Math.min((safePage + 1) * TRADES_PER_PAGE, sortedTrades.length)} de{" "}
            {sortedTrades.length}
          </span>
          <div>
            <button
              type="button"
              className="button"
              disabled={safePage === 0}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
            >
              Anterior
            </button>
            <span>
              Página {safePage + 1} de {totalPages}
            </span>
            <button
              type="button"
              className="button"
              disabled={safePage + 1 >= totalPages}
              onClick={() =>
                setPage((current) => Math.min(totalPages - 1, current + 1))
              }
            >
              Siguiente
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function UnassignedSummary({ data }: { data: JournalAnalysisData }) {
  const groups = data.unassigned_groups || [];
  if (!groups.length) return null;
  return (
    <section className="panel journal-unassigned-summary">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">PENDIENTES DE IDENTIFICACIÃ“N</span>
          <h2>Trades sin asignar</h2>
        </div>
        <span className="value-bad">{data.reconciliation.unassigned_trades} operaciones</span>
      </div>
      <p>Se conservan en el Journal hasta que exista evidencia suficiente para asignarlos al bot correcto.</p>
      <div className="table-scroll">
        <table className="journal-trades-table">
          <thead><tr><th>Cuenta</th><th>Activo</th><th>Magic Number</th><th>Motivo</th><th>Trades</th><th>Ãšltimo cierre</th></tr></thead>
          <tbody>{groups.map((group) => <tr key={`${group.account}-${group.symbol}-${group.magic_number}-${group.reason}`}>
            <td>{group.account}</td><td>{group.symbol}</td><td>{group.magic_number || "â€”"}</td><td>{group.reason}</td><td>{group.count}</td>
            <td>{new Date(group.last_close_time_msc).toLocaleString("en-GB")}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </section>
  );
}

function metricLabel(metric: TemporalMetric) {
  return metric === "winRate"
    ? "Win Rate"
    : metric === "expectancy"
      ? "Expectancy"
      : "Profit Factor";
}

function formatMetric(value: number | null, metric: TemporalMetric) {
  if (value == null || !Number.isFinite(value)) return "N/D";
  if (metric === "winRate") return `${value.toFixed(1)}%`;
  if (metric === "expectancy")
    return value.toLocaleString("es-ES", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  return value.toFixed(2);
}

function formatSigned(value: number | null) {
  if (value == null || !Number.isFinite(value)) return "N/D";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function formatAxisTick(value: number, metric: TemporalMetric) {
  if (metric === "profitFactor") return value.toFixed(2);
  if (metric === "winRate") return value.toFixed(0);
  return value.toLocaleString("es-ES", { maximumFractionDigits: 0 });
}

function Distribution({
  title,
  rows,
}: {
  title: string;
  rows: Array<{ label: string; count: number; pct: number }>;
}) {
  return (
    <section className="panel account-distribution">
      <div className="account-panel-title">
        <span>{title}</span>
        <small>{rows.reduce((sum, row) => sum + row.count, 0)} trades</small>
      </div>
      <div className="distribution-list">
        {rows.map((row) => (
          <div key={row.label}>
            <div>
              <strong>{row.label}</strong>
              <span>
                {row.count} · {row.pct.toFixed(1)}%
              </span>
            </div>
            <span className="distribution-track">
              <i style={{ width: `${row.pct}%` }} />
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function bestAndWorst(rows: TemporalBucket[], metric: TemporalMetric) {
  const available = rows.filter(
    (row) => row.count > 0 && temporalValue(row, metric) != null,
  );
  if (!available.length) return { best: null, worst: null };
  const sorted = [...available].sort(
    (left, right) =>
      (temporalValue(left, metric) as number) -
      (temporalValue(right, metric) as number),
  );
  return { worst: sorted[0], best: sorted[sorted.length - 1] };
}

export function temporalDomain(
  rows: TemporalBucket[],
  metric: TemporalMetric,
): [number, number] {
  const values = rows
    .map((row) => temporalVisualValue(row, metric))
    .filter(
      (value): value is number => value != null && Number.isFinite(value),
    );
  if (metric === "winRate") return [0, 100];
  const maximum = Math.max(1, ...values.map((value) => Math.abs(value)));
  return [-maximum, maximum];
}

export function temporalVisualValue(
  bucket: TemporalBucket,
  metric: TemporalMetric,
) {
  const value = temporalValue(bucket, metric);
  return metric === "profitFactor" && value != null ? value - 1 : value;
}

function temporalBarColor(
  value: number | null,
  metric: TemporalMetric,
  domain: [number, number],
  lowSample: boolean,
) {
  if (value == null || !Number.isFinite(value)) return "transparent";
  const isSigned = metric === "expectancy" || metric === "profitFactor";
  const intensity = isSigned
    ? Math.min(
        1,
        Math.abs(value) / Math.max(Math.abs(domain[0]), Math.abs(domain[1])),
      )
    : Math.min(1, value / domain[1]);
  const alpha = (lowSample ? 0.34 : 0.5) + intensity * (lowSample ? 0.18 : 0.4);
  if (isSigned && value < 0) return `rgba(251, 113, 133, ${alpha})`;
  return `rgba(45, 212, 191, ${alpha})`;
}

function TemporalChartTooltip({
  active,
  payload,
  metric,
}: {
  active?: boolean;
  payload?: Array<{ payload: TemporalBucket & { value: number | null } }>;
  metric: TemporalMetric;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  const rawValue = temporalValue(row, metric);
  return (
    <div className="journal-tooltip temporal-chart-tooltip">
      <strong>{row.label}</strong>
      <div>
        <span>{metricLabel(metric)}</span>
        <b>{formatMetric(rawValue, metric)}</b>
      </div>
      {metric === "profitFactor" && (
        <div>
          <span>Vs. equilibrio</span>
          <b>{formatSigned(row.value)}</b>
        </div>
      )}
      <div>
        <span>Muestra</span>
        <b>
          {row.count} operaciones{row.lowSample ? " · baja" : ""}
        </b>
      </div>
    </div>
  );
}

function TemporalChart({
  title,
  rows,
  metric,
}: {
  title: string;
  rows: TemporalBucket[];
  metric: TemporalMetric;
}) {
  const domain = temporalDomain(rows, metric);
  const data = rows.map((row) => ({
    ...row,
    value: temporalVisualValue(row, metric),
  }));
  const isHours = rows.length > 12;
  return (
    <section
      className={`panel temporal-panel ${isHours ? "temporal-hours" : ""}`}
    >
      <div className="account-panel-title">
        <span>{title}</span>
        <small>UTC+02:00</small>
      </div>
      <div className="temporal-chart-scroll">
        <div className="temporal-chart-canvas">
          <ResponsiveContainer width="100%" height={142}>
            <BarChart
              data={data}
              margin={{ top: 8, right: 4, bottom: 0, left: 0 }}
              barCategoryGap={isHours ? "22%" : "16%"}
            >
              <CartesianGrid stroke={GRID} vertical={false} />
              <YAxis
                domain={domain}
                tick={{ fill: "#718895", fontSize: 9 }}
                tickFormatter={(value) => formatAxisTick(value, metric)}
                width={42}
                tickCount={5}
              />
              <XAxis
                dataKey="label"
                tick={false}
                axisLine={false}
                tickLine={false}
              />
              <ReferenceLine y={0} stroke="#587081" />
              <Tooltip
                cursor={{ fill: "#15303a", opacity: 0.35 }}
                content={<TemporalChartTooltip metric={metric} />}
              />
              <Bar
                dataKey="value"
                radius={[5, 5, 3, 3]}
                maxBarSize={isHours ? 24 : 62}
              >
                {data.map((row) => (
                  <Cell
                    key={row.key}
                    fill={temporalBarColor(
                      row.value,
                      metric,
                      domain,
                      row.lowSample,
                    )}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div className="temporal-bar-values">
            {data.map((row) => (
              <div
                key={row.key}
                className={row.lowSample && row.count > 0 ? "low-sample" : ""}
                title={`${row.label}: ${row.count} trades${row.lowSample ? " · muestra baja" : ""}`}
              >
                <span>{row.label}</span>
                <strong>
                  {formatMetric(temporalValue(row, metric), metric)}
                </strong>
                <small>
                  {row.count
                    ? `${row.count} ops${row.lowSample ? " ⚠" : ""}`
                    : "sin muestra"}
                </small>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function TemporalBadge({
  tone,
  label,
  row,
  metric,
}: {
  tone: "best" | "worst";
  label: string;
  row: TemporalBucket | null;
  metric: TemporalMetric;
}) {
  const value = row ? temporalValue(row, metric) : null;
  const delta = row ? temporalVisualValue(row, metric) : null;
  const text =
    metric === "profitFactor"
      ? `${formatMetric(value, metric)} · Δ ${formatSigned(delta)}`
      : formatMetric(value, metric);
  return (
    <span className={`temporal-badge ${tone}`}>
      <b>{tone === "best" ? "↑" : "↓"}</b>
      {label}: {row ? `${row.label} · ${text}` : "N/D"}
    </span>
  );
}

function TemporalLegend({ metric }: { metric: TemporalMetric }) {
  const signed = metric === "expectancy" || metric === "profitFactor";
  const description =
    metric === "profitFactor"
      ? "PF − 1 · 0 = PF 1.00"
      : metric === "expectancy"
        ? "Negativo → Positivo"
        : "Baja → Alta";
  return (
    <div className="temporal-legend">
      <span>
        {metric === "profitFactor"
          ? "PF relativo al equilibrio"
          : metricLabel(metric)}
      </span>
      <i className={signed ? "signed" : "positive"} />
      <small>{description}</small>
    </div>
  );
}

function utcPlusTwoDate(ms: number) {
  return new Date(ms + 2 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 16)
    .replace("T", " ");
}

function AccountAnalytics({
  data,
  capitalEligible,
}: {
  data: JournalAnalysisData;
  capitalEligible: boolean;
}) {
  const [metric, setMetric] = useState<TemporalMetric>("winRate");
  const analytics = useMemo(
    () => buildAccountAnalytics(data, capitalEligible),
    [data, capitalEligible],
  );
  const temporalSummaries = {
    hours: bestAndWorst(analytics.temporal.hours, metric),
    weekdays: bestAndWorst(analytics.temporal.weekdays, metric),
    months: bestAndWorst(analytics.temporal.months, metric),
  };
  const card = (
    label: string,
    value: string,
    detail: string,
    unavailable = false,
  ) => (
    <div className={unavailable ? "account-kpi unavailable" : "account-kpi"}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
  const capitalReason =
    analytics.capital.reason ||
    "Balances reconciliados con el P/L de las cuentas seleccionadas.";
  return (
    <section className="account-analytics">
      <div className="account-analytics-heading">
        <div>
          <span className="eyebrow">ACCOUNT ANALYTICS</span>
          <h2>Nuevas analíticas de cuenta</h2>
        </div>
        <p>
          Exposición, dependencia estadística y riesgo temporal · agrupación
          fija UTC+02:00.
        </p>
      </div>
      <div className="account-kpi-grid">
        {card(
          "CAGR",
          analytics.capital.cagr == null
            ? "N/D"
            : `${analytics.capital.cagr.toFixed(2)}%`,
          analytics.capital.cagr == null
            ? capitalReason
            : "Rentabilidad anual compuesta",
          analytics.capital.cagr == null,
        )}
        {card(
          "Sharpe diario",
          analytics.capital.sharpe == null
            ? "N/D"
            : analytics.capital.sharpe.toFixed(2),
          analytics.capital.sharpe == null
            ? capitalReason
            : "Anualizado · rf 0 · √252",
          analytics.capital.sharpe == null,
        )}
        {card(
          "Z-Score",
          analytics.z.zScore == null ? "N/D" : analytics.z.zScore.toFixed(2),
          `${analytics.z.count} trades no neutros`,
          analytics.z.zScore == null,
        )}
        {card(
          "Z-Probabilidad",
          analytics.z.zProbability == null
            ? "N/D"
            : `${analytics.z.zProbability.toFixed(1)}%`,
          "Confianza bilateral de dependencia",
          analytics.z.zProbability == null,
        )}
        {card(
          "Estancación máx.",
          `${analytics.stagnationDays.toFixed(1)} días`,
          "Tiempo máximo sin nuevo máximo",
        )}
        {card(
          "Exposición",
          analytics.exposure == null
            ? "N/D"
            : `${analytics.exposure.toFixed(2)}%`,
          "Unión de intervalos con posiciones",
          analytics.exposure == null,
        )}
      </div>
      <div className="account-distribution-grid">
        <Distribution title="Exposición por símbolo" rows={analytics.symbols} />
        <Distribution title="Compra vs venta" rows={analytics.directions} />
        <Distribution title="Tiempo de exposición" rows={analytics.durations} />
      </div>
      <section className="temporal-analysis">
        <div className="temporal-analysis-head">
          <div>
            <span className="eyebrow">TEMPORAL MATRIX</span>
            <h3>Comportamiento por cierre</h3>
          </div>
          <div className="temporal-metric-toggle">
            {(
              ["winRate", "expectancy", "profitFactor"] as TemporalMetric[]
            ).map((value) => (
              <button
                type="button"
                key={value}
                className={metric === value ? "active" : ""}
                onClick={() => setMetric(value)}
              >
                {metricLabel(value)}
              </button>
            ))}
          </div>
        </div>
        <div className="temporal-insights">
          <div className="temporal-badges">
            <TemporalBadge
              tone="best"
              label="Mejor hora"
              row={temporalSummaries.hours.best}
              metric={metric}
            />
            <TemporalBadge
              tone="worst"
              label="Peor hora"
              row={temporalSummaries.hours.worst}
              metric={metric}
            />
            <TemporalBadge
              tone="best"
              label="Mejor día"
              row={temporalSummaries.weekdays.best}
              metric={metric}
            />
            <TemporalBadge
              tone="worst"
              label="Peor día"
              row={temporalSummaries.weekdays.worst}
              metric={metric}
            />
            <TemporalBadge
              tone="best"
              label="Mejor mes"
              row={temporalSummaries.months.best}
              metric={metric}
            />
            <TemporalBadge
              tone="worst"
              label="Peor mes"
              row={temporalSummaries.months.worst}
              metric={metric}
            />
          </div>
          <TemporalLegend metric={metric} />
        </div>
        <div className="temporal-grid">
          <TemporalChart
            title="Horas"
            rows={analytics.temporal.hours}
            metric={metric}
          />
          <TemporalChart
            title="Días"
            rows={analytics.temporal.weekdays}
            metric={metric}
          />
          <TemporalChart
            title="Meses"
            rows={analytics.temporal.months}
            metric={metric}
          />
        </div>
      </section>
      <section className="panel drawdown-episodes">
        <div className="account-panel-title">
          <span>Análisis de drawdown</span>
          <small>{analytics.drawdowns.length} episodios</small>
        </div>
        {analytics.drawdowns.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Profundidad</th>
                  <th>Profundidad %</th>
                  <th>Días</th>
                  <th>Inicio</th>
                  <th>Valle</th>
                  <th>Recuperación</th>
                </tr>
              </thead>
              <tbody>
                {analytics.drawdowns.map((episode, index) => (
                  <tr key={`${episode.start_msc}-${index}`}>
                    <td className="value-bad">
                      -
                      {episode.depth.toLocaleString("es-ES", {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      })}
                    </td>
                    <td>
                      {episode.depth_pct == null ? (
                        <span title={capitalReason}>N/D</span>
                      ) : (
                        `-${episode.depth_pct.toFixed(2)}%`
                      )}
                    </td>
                    <td>{episode.days.toFixed(1)}</td>
                    <td>{utcPlusTwoDate(episode.start_msc)}</td>
                    <td>{utcPlusTwoDate(episode.trough_msc)}</td>
                    <td>
                      {episode.ongoing ? (
                        <span className="drawdown-ongoing">En curso</span>
                      ) : (
                        utcPlusTwoDate(episode.recovery_msc as number)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">
            No se detectaron episodios de drawdown en el período.
          </div>
        )}
      </section>
    </section>
  );
}

export default function JournalAnalysis({
  data,
  capitalEligible = true,
}: {
  data: JournalAnalysisData | null;
  capitalEligible?: boolean;
}) {
  const [botEquityExpanded, setBotEquityExpanded] = useState(false);
  const charts = useMemo(() => {
    const trades = data?.trades || [],
      equity: Array<{ label: string; equity: number }> = [];
    let total = 0;
    for (const trade of trades) {
      total += trade.net_profit;
      equity.push({
        label: new Date(trade.close_time_msc).toLocaleDateString(),
        equity: total,
      });
    }
    const monthlyMap = new Map<string, JournalTrade[]>(),
      weekday = DAYS.map((day) => ({ day, wins: 0, count: 0 })),
      hourly = Array.from({ length: 24 }, (_, hour) => ({
        hour: `${String(hour).padStart(2, "0")}:00`,
        profit: 0,
      }));
    for (const trade of trades) {
      const key = monthKey(trade.close_time_msc);
      monthlyMap.set(key, [...(monthlyMap.get(key) || []), trade]);
      const date = new Date(trade.close_time_msc);
      weekday[date.getDay()].count++;
      if (trade.net_profit > 0) weekday[date.getDay()].wins++;
      hourly[date.getHours()].profit += trade.net_profit;
    }
    const monthly = [...monthlyMap].map(([month, items]) => ({
        month,
        trades: items.length,
        drawdown: maxDrawdown(items),
      })),
      botEquity = buildBotEquityCurves(trades);
    return {
      equity,
      botEquity: botEquity.series.length ? botEquity : null,
      monthly,
      weekday: weekday.map((item) => ({
        ...item,
        winRate: item.count ? (item.wins / item.count) * 100 : 0,
      })),
      hourly,
    };
  }, [data]);
  if (!data)
    return (
      <div className="panel empty-state performance-empty">
        Loading journal analysis…
      </div>
    );
  if (!data.trades.length)
    return (
      <div className="panel empty-state performance-empty">
        No closed trades match the selected accounts and dates.
      </div>
    );
  return (
    <div className="journal-view">
      <div className="performance-intro">
        <div>
          <span className="eyebrow">PORTFOLIO JOURNAL</span>
          <h2>Journal analysis</h2>
        </div>
        <p>
          All closed trades for the selected broker accounts, including
          operations awaiting strategy assignment.
        </p>
      </div>
      <div className="journal-grid">
        <JournalChart wide title="Equity curve — cumulative net profit">
          <ResponsiveContainer>
            <AreaChart data={charts.equity}>
              <defs>
                <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor={GREEN} stopOpacity={0.35} />
                  <stop offset="1" stopColor={GREEN} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={GRID} />
              <XAxis
                dataKey="label"
                tick={{ fill: "#74899a", fontSize: 9 }}
                minTickGap={30}
              />
              <YAxis tick={{ fill: "#74899a", fontSize: 9 }} />
              <Tooltip content={<ChartTip />} />
              <Area dataKey="equity" stroke={GREEN} fill="url(#equityFill)" />
            </AreaChart>
          </ResponsiveContainer>
        </JournalChart>
      </div>
      <TradeList data={data} />
      <UnassignedSummary data={data} />
      <div className="journal-grid">
        {charts.botEquity && (
          <section className="panel journal-chart wide bot-equity-chart">
            <div className="journal-chart-title">
              <span>Equity individual por bot</span>
              <button
                type="button"
                className="journal-chart-toggle"
                aria-expanded={botEquityExpanded}
                aria-controls="bot-equity-chart"
                aria-label={
                  botEquityExpanded
                    ? "Contraer equity individual por bot"
                    : "Desplegar equity individual por bot"
                }
                onClick={() => setBotEquityExpanded((expanded) => !expanded)}
              >
                <span aria-hidden="true">⌄</span>
              </button>
            </div>
            {botEquityExpanded && (
              <div id="bot-equity-chart" className="journal-chart-body">
                <ResponsiveContainer>
                  <LineChart data={charts.botEquity.points}>
                    <CartesianGrid stroke={GRID} />
                    <XAxis
                      dataKey="label"
                      tick={{ fill: "#74899a", fontSize: 9 }}
                      minTickGap={30}
                    />
                    <YAxis tick={{ fill: "#74899a", fontSize: 9 }} />
                    <Tooltip content={<ChartTip />} />
                    <Legend wrapperStyle={{ fontSize: 10 }} />
                    {charts.botEquity.series.map((series) => (
                      <Line
                        key={series.key}
                        type="monotone"
                        dataKey={series.key}
                        name={series.name}
                        stroke={series.color}
                        strokeWidth={2}
                        dot={false}
                        connectNulls
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </section>
        )}
        <Calendar data={data} />
        <JournalChart title="Trades executed by month">
          <ResponsiveContainer>
            <BarChart data={charts.monthly}>
              <CartesianGrid stroke={GRID} />
              <XAxis dataKey="month" tick={{ fill: "#74899a", fontSize: 9 }} />
              <YAxis tick={{ fill: "#74899a", fontSize: 9 }} />
              <Tooltip content={<ChartTip />} />
              <Bar dataKey="trades" fill="#38bdf8" />
            </BarChart>
          </ResponsiveContainer>
        </JournalChart>
        <JournalChart title="Win rate by weekday">
          <ResponsiveContainer>
            <BarChart data={charts.weekday}>
              <CartesianGrid stroke={GRID} />
              <XAxis dataKey="day" tick={{ fill: "#74899a", fontSize: 9 }} />
              <YAxis
                domain={[0, 100]}
                tick={{ fill: "#74899a", fontSize: 9 }}
              />
              <Tooltip content={<ChartTip suffix="%" />} />
              <Bar dataKey="winRate" fill={GREEN} />
            </BarChart>
          </ResponsiveContainer>
        </JournalChart>
        <JournalChart title="Net profit by hour">
          <ResponsiveContainer>
            <BarChart data={charts.hourly}>
              <CartesianGrid stroke={GRID} />
              <XAxis
                dataKey="hour"
                interval={2}
                tick={{ fill: "#74899a", fontSize: 9 }}
              />
              <YAxis tick={{ fill: "#74899a", fontSize: 9 }} />
              <Tooltip content={<ChartTip />} />
              <Bar dataKey="profit">
                {charts.hourly.map((item, index) => (
                  <Cell key={index} fill={item.profit >= 0 ? GREEN : RED} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </JournalChart>
        <JournalChart title="Max drawdown by month">
          <ResponsiveContainer>
            <LineChart data={charts.monthly}>
              <CartesianGrid stroke={GRID} />
              <XAxis dataKey="month" tick={{ fill: "#74899a", fontSize: 9 }} />
              <YAxis tick={{ fill: "#74899a", fontSize: 9 }} />
              <Tooltip content={<ChartTip />} />
              <Line dataKey="drawdown" stroke={RED} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </JournalChart>
      </div>
      <AccountAnalytics data={data} capitalEligible={capitalEligible} />
    </div>
  );
}
