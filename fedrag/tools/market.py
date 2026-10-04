"""Live market and economic data tools (no API keys needed).

* FX: Frankfurter (ECB reference rates, historical) with open.er-api.com fallback
* Economic series: FRED (St. Louis Fed) CSV endpoint
* Prices of indices / stocks / ETFs / futures / crypto: ``stockcache`` (local cache
  of Yahoo daily bars) when installed, otherwise yfinance
* A safe calculator
"""

from __future__ import annotations

import ast
import asyncio
import csv
import datetime as dt
import io
import math
import operator
import re

from .base import RunContext, Tool, schema

FRANKFURTER = "https://api.frankfurter.dev/v1"
ER_API = "https://open.er-api.com/v6/latest"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# Curated FRED series (id: description). Agents may also use any other valid FRED id.
FRED_CATALOG: dict[str, str] = {
    "DFEDTARU": "Federal funds target range, upper limit (percent, daily)",
    "DFEDTARL": "Federal funds target range, lower limit (percent, daily)",
    "DFF": "Effective federal funds rate (percent, daily)",
    "FEDFUNDS": "Effective federal funds rate (percent, monthly average)",
    "IORB": "Interest rate on reserve balances (percent, daily)",
    "RRPONTSYAWARD": "Overnight reverse repo award rate (percent, daily)",
    "SOFR": "Secured Overnight Financing Rate (percent, daily)",
    "DPRIME": "Bank prime loan rate (percent, daily)",
    "DGS1MO": "1-month Treasury constant maturity yield (percent, daily)",
    "DGS3MO": "3-month Treasury yield (percent, daily)",
    "DGS1": "1-year Treasury yield (percent, daily)",
    "DGS2": "2-year Treasury yield (percent, daily)",
    "DGS5": "5-year Treasury yield (percent, daily)",
    "DGS10": "10-year Treasury yield (percent, daily)",
    "DGS30": "30-year Treasury yield (percent, daily)",
    "T10Y2Y": "10-year minus 2-year Treasury spread (percentage points, daily)",
    "T10Y3M": "10-year minus 3-month Treasury spread (percentage points, daily)",
    "T10YIE": "10-year breakeven inflation rate (percent, daily)",
    "T5YIFR": "5-year, 5-year forward inflation expectation rate (percent, daily)",
    "DFII10": "10-year TIPS real yield (percent, daily)",
    "MORTGAGE30US": "30-year fixed mortgage rate, Freddie Mac (percent, weekly)",
    "BAMLH0A0HYM2": "ICE BofA US high-yield option-adjusted spread (percent, daily)",
    "BAMLC0A0CM": "ICE BofA US investment-grade corporate OAS (percent, daily)",
    "VIXCLS": "CBOE VIX volatility index (daily)",
    "DTWEXBGS": "Nominal broad U.S. dollar index (daily)",
    "CPIAUCSL": "CPI, all items, seasonally adjusted (index 1982-84=100, monthly) - use transform=yoy for inflation",
    "CPILFESL": "Core CPI, less food and energy (index, monthly) - use transform=yoy",
    "PCEPI": "PCE price index (index, monthly) - use transform=yoy for PCE inflation",
    "PCEPILFE": "Core PCE price index (index, monthly) - the Fed's preferred core measure; transform=yoy",
    "MICH": "University of Michigan expected inflation, 1 year ahead (percent, monthly)",
    "UNRATE": "Unemployment rate (percent, monthly)",
    "PAYEMS": "Total nonfarm payrolls (thousands of persons, monthly) - transform=diff for monthly job gains",
    "CIVPART": "Labor force participation rate (percent, monthly)",
    "JTSJOL": "JOLTS job openings (thousands, monthly)",
    "ICSA": "Initial jobless claims (number, weekly)",
    "CES0500000003": "Average hourly earnings, total private (dollars, monthly) - transform=yoy for wage growth",
    "GDPC1": "Real GDP (billions of chained 2017 dollars, quarterly)",
    "A191RL1Q225SBEA": "Real GDP growth, percent change at annual rate (quarterly)",
    "GDP": "Nominal GDP (billions of dollars, quarterly)",
    "INDPRO": "Industrial production index (monthly)",
    "RSAFS": "Advance retail sales (millions of dollars, monthly)",
    "HOUST": "Housing starts (thousands of units, annual rate, monthly)",
    "CSUSHPINSA": "S&P/Case-Shiller U.S. national home price index (monthly)",
    "UMCSENT": "University of Michigan consumer sentiment (index, monthly)",
    "WALCL": "Federal Reserve total assets (millions of dollars, weekly)",
    "WRESBAL": "Reserve balances with Federal Reserve Banks (billions of dollars, weekly)",
    "RRPONTSYD": "Overnight reverse repurchase agreements outstanding (billions of dollars, daily)",
    "TREAST": "Treasury securities held by the Federal Reserve (millions of dollars, weekly)",
    "WSHOMCB": "Mortgage-backed securities held by the Federal Reserve (millions of dollars, weekly)",
    "M2SL": "M2 money stock (billions of dollars, monthly)",
    "TOTLL": "Loans and leases in bank credit, all commercial banks (billions, weekly)",
    "DRTSCILM": "Senior Loan Officer Survey: net % of banks tightening C&I loan standards, large firms (quarterly)",
    "DCOILWTICO": "WTI crude oil price (dollars per barrel, daily)",
    "GFDEBTN": "Federal debt: total public debt (millions of dollars, quarterly)",
}


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------


async def get_exchange_rate(ctx: RunContext, base: str, quote: str, date: str | None = None) -> str:
    base = base.upper().strip()
    quotes = [q.upper().strip() for q in re.split(r"[,\s]+", quote) if q.strip()]
    path = date if date else "latest"
    r = await ctx.get(f"{FRANKFURTER}/{path}", params={"base": base, "symbols": ",".join(quotes)})
    if r.status_code == 200:
        data = r.json()
        rates = data.get("rates", {})
        missing = [q for q in quotes if q not in rates]
        lines = [f"1 {base} = {rates[q]} {q}" for q in quotes if q in rates]
        text = f"ECB reference rates (via Frankfurter) for {data.get('date')}:\n" + "\n".join(lines)
        if date and data.get("date") != date:
            text += f"\n(note: {date} was not a publishing day; nearest earlier ECB fixing shown)"
        if missing:
            text += f"\nNot available from the ECB: {', '.join(missing)}"
        if lines:
            ev = ctx.evidence.add_data(f"fx:{base}:{','.join(quotes)}:{data.get('date')}",
                                       f"Exchange rate {base}/{','.join(quotes)}", text,
                                       f"Frankfurter/ECB reference rates, {data.get('date')}",
                                       url=str(r.request.url))
            text = f"[{ev.id}] " + text
            if not missing:
                return text
        quotes = missing
        prefix = text + "\n\n"
    else:
        prefix = ""
    if date:
        return prefix + f"Historical rates for {', '.join(quotes)} are not available from the ECB; " \
                        f"try get_market_prices with a Yahoo FX symbol like {base}{quotes[0]}=X."
    r2 = await ctx.get(f"{ER_API}/{base}")
    if r2.status_code != 200 or r2.json().get("result") != "success":
        return prefix + f"ERROR: no rate source has {base}->{','.join(quotes)}"
    data = r2.json()
    rates = data["rates"]
    lines = [f"1 {base} = {rates[q]} {q}" for q in quotes if q in rates]
    updated = data.get("time_last_update_utc", "")
    text = f"Daily rates from open.er-api.com (last update {updated}):\n" + "\n".join(lines)
    ev = ctx.evidence.add_data(f"erapi:{base}:{','.join(quotes)}:{updated}", f"Exchange rate {base}", text,
                               f"open.er-api.com (ExchangeRate-API), updated {updated}", url=f"{ER_API}/{base}")
    return prefix + f"[{ev.id}] " + text


async def get_exchange_rate_history(ctx: RunContext, base: str, quote: str, start_date: str,
                                    end_date: str | None = None) -> str:
    base, quote = base.upper().strip(), quote.upper().strip()
    span = f"{start_date}..{end_date or ''}"
    r = await ctx.get(f"{FRANKFURTER}/{span}", params={"base": base, "symbols": quote})
    if r.status_code != 200:
        return f"ERROR: Frankfurter returned HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    series = sorted((d, v[quote]) for d, v in data.get("rates", {}).items() if quote in v)
    if not series:
        return f"No {base}/{quote} data in that range."
    text = _summarize_series(f"{base}/{quote}", series, unit=f"{quote} per {base}")
    ev = ctx.evidence.add_data(f"fxh:{base}:{quote}:{span}", f"{base}/{quote} history {span}", text,
                               f"Frankfurter/ECB reference rates {series[0][0]} to {series[-1][0]}",
                               url=str(r.request.url))
    return f"[{ev.id}] " + text


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------


def _summarize_series(name: str, series: list[tuple[str, float]], unit: str = "", max_rows: int = 24,
                      is_rate: bool = False) -> str:
    """``is_rate``: values are already percentages, so report changes in percentage points."""
    first, last = series[0], series[-1]
    vals = [v for _, v in series]
    hi = max(series, key=lambda x: x[1])
    lo = min(series, key=lambda x: x[1])
    chg = last[1] - first[1]
    if is_rate:
        change = f"change {chg:+.4g} percentage points"
    else:
        pct = (chg / first[1] * 100) if first[1] else float("nan")
        change = f"change {chg:+.4g} ({pct:+.2f}%)"
    lines = [
        f"{name}{f' ({unit})' if unit else ''}: {len(series)} observations {first[0]} to {last[0]}",
        f"latest: {last[1]:g} on {last[0]}; first: {first[1]:g} on {first[0]}; {change}",
        f"high {hi[1]:g} on {hi[0]}; low {lo[1]:g} on {lo[0]}; mean {sum(vals) / len(vals):.4g}",
    ]
    step = max(1, math.ceil(len(series) / max_rows))
    sampled = series[::step]
    if sampled[-1] != last:
        sampled.append(last)
    lines.append("observations" + (" (sampled)" if step > 1 else "") + ":")
    lines += [f"  {d}: {v:g}" for d, v in sampled]
    return "\n".join(lines)


def _transform(series: list[tuple[str, float]], transform: str) -> tuple[list[tuple[str, float]], str]:
    if transform in ("", "none", "level", None):
        return series, ""
    if transform == "diff":
        return [(series[i][0], series[i][1] - series[i - 1][1]) for i in range(1, len(series))], "change from previous observation"
    if transform in ("pct", "mom"):
        return [(series[i][0], (series[i][1] / series[i - 1][1] - 1) * 100) for i in range(1, len(series))
                if series[i - 1][1]], "percent change from previous observation"
    if transform == "yoy":
        by_date = dict(series)
        out = []
        for d, v in series:
            prior = f"{int(d[:4]) - 1}{d[4:]}"
            if prior in by_date and by_date[prior]:
                out.append((d, (v / by_date[prior] - 1) * 100))
        return out, "percent change from a year earlier"
    raise ValueError(f"unknown transform {transform!r}")


async def get_economic_series(ctx: RunContext, series_id: str, start_date: str | None = None,
                              end_date: str | None = None, transform: str = "none") -> str:
    sid = series_id.upper().strip()
    params = {"id": sid}
    fetch_start = start_date
    if transform == "yoy" and start_date:  # need a year of history before start_date
        fetch_start = f"{int(start_date[:4]) - 1}{start_date[4:10]}"
    if fetch_start:
        params["cosd"] = fetch_start
    if end_date:
        params["coed"] = end_date
    r = await ctx.get(FRED_CSV, params=params)
    if r.status_code != 200 or not r.text.lower().startswith("observation_date"):
        return (f"ERROR: FRED has no series {sid!r} (HTTP {r.status_code}). Use search_economic_series to "
                f"find a valid id.")
    rows = list(csv.reader(io.StringIO(r.text)))[1:]
    series = []
    for row in rows:
        if len(row) >= 2 and row[1] not in ("", "."):
            try:
                series.append((row[0], float(row[1])))
            except ValueError:
                continue
    if not series:
        return f"No observations for {sid} in that range."
    try:
        series, tdesc = _transform(series, transform)
    except ValueError as e:
        return f"ERROR: {e}"
    if start_date:
        series = [x for x in series if x[0] >= start_date[:10]] or series
    gaps = sum(1 for row in rows if len(row) >= 2 and row[1] in ("", "."))
    desc = FRED_CATALOG.get(sid, sid)
    is_rate = transform in ("yoy", "pct", "mom") or "(percent" in desc or "percentage points" in desc
    text = _summarize_series(f"FRED {sid} - {desc}", series, unit=tdesc, is_rate=is_rate)
    if gaps:
        text += f"\n({gaps} missing observation(s) in the raw data were skipped)"
    ev = ctx.evidence.add_data(f"fred:{sid}:{start_date}:{end_date}:{transform}", f"FRED {sid}", text,
                               f"FRED series {sid} ({desc}), retrieved {ctx.today.isoformat()}",
                               url=f"https://fred.stlouisfed.org/series/{sid}")
    return f"[{ev.id}] " + text


async def search_economic_series(ctx: RunContext, keywords: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", keywords.lower()) if len(w) > 1]
    scored = []
    for sid, desc in FRED_CATALOG.items():
        hay = f"{sid} {desc}".lower()
        s = sum(1 for w in words if w in hay)
        if s:
            scored.append((s, sid, desc))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return ("No catalog match. Any valid FRED series id also works with get_economic_series. Catalog:\n"
                + "\n".join(f"{k}: {v}" for k, v in FRED_CATALOG.items()))
    return "\n".join(f"{sid}: {desc}" for _, sid, desc in scored[:12])


# ---------------------------------------------------------------------------
# Market prices
# ---------------------------------------------------------------------------


def _load_prices(symbol: str, start: str | None, end: str | None) -> list[tuple[str, float]]:
    try:
        import stockcache as sc  # user-level cache of Yahoo daily bars

        s = sc.close(symbol, start=start, end=end)
    except ImportError:
        import yfinance as yf

        df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
        s = df["Close"].squeeze() if not df.empty else None
    if s is None or len(s) == 0:
        return []
    s = s.dropna()
    return [(str(idx)[:10], float(v)) for idx, v in s.items()]


async def get_market_prices(ctx: RunContext, symbol: str, start_date: str | None = None,
                            end_date: str | None = None) -> str:
    sym = symbol.strip()
    if not start_date:
        start_date = (ctx.today - dt.timedelta(days=60)).isoformat()
    try:
        series = await asyncio.wait_for(asyncio.to_thread(_load_prices, sym, start_date, end_date), timeout=60)
    except Exception as e:
        return f"ERROR: could not load {sym}: {type(e).__name__}: {e}"
    if not series:
        return (f"No data for {sym!r}. Yahoo symbols: indices ^GSPC ^DJI ^IXIC ^VIX, yields ^IRX ^FVX ^TNX ^TYX, "
                f"FX EURUSD=X, futures GC=F CL=F, crypto BTC-USD, stocks/ETFs by ticker.")
    text = _summarize_series(f"{sym} daily close", series, is_rate=sym.upper() in ("^IRX", "^FVX", "^TNX", "^TYX"))
    ev = ctx.evidence.add_data(f"px:{sym}:{start_date}:{end_date}", f"{sym} prices", text,
                               f"Yahoo Finance daily closes for {sym} ({series[0][0]} to {series[-1][0]})",
                               url=f"https://finance.yahoo.com/quote/{sym}")
    return f"[{ev.id}] " + text


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"abs": abs, "round": round, "min": min, "max": max, "sqrt": math.sqrt, "log": math.log,
          "log10": math.log10, "exp": math.exp, "sum": sum}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")


async def calculator(ctx: RunContext, expression: str) -> str:
    # "12,583.7" -> "12583.7" (thousands separators), "2^3" -> "2**3"
    expr = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression).replace("^", "**")
    try:
        val = _eval(ast.parse(expr, mode="eval"))
    except Exception as e:
        return f"ERROR: {e}"
    return f"{expression} = {val:.10g}" if isinstance(val, float) else f"{expression} = {val}"


def make_market_tools() -> list[Tool]:
    return [
        Tool(
            "get_exchange_rate",
            "Current or historical exchange rate(s). ECB reference rates (about 30 major currencies, published "
            "on business days around 16:00 CET), with a fallback source for other currencies (latest only).",
            schema({
                "base": {"type": "string", "description": "ISO code, e.g. USD"},
                "quote": {"type": "string", "description": "ISO code or comma-separated codes, e.g. EUR,JPY"},
                "date": {"type": "string", "description": "YYYY-MM-DD for a historical rate; omit for latest"},
            }, ["base", "quote"]),
            get_exchange_rate,
        ),
        Tool(
            "get_exchange_rate_history",
            "Daily exchange-rate series between two dates with summary statistics (change, high, low).",
            schema({
                "base": {"type": "string"}, "quote": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD; omit for today"},
            }, ["base", "quote", "start_date"]),
            get_exchange_rate_history,
        ),
        Tool(
            "get_economic_series",
            "Fetch an official U.S. economic/financial time series from FRED (latest value, summary and "
            "observations). Use transform='yoy' to turn price indexes into inflation rates, 'diff' for "
            "monthly changes (e.g. payroll gains). Common ids: DFEDTARU/DFEDTARL (fed funds target range), "
            "DFF (effective fed funds), IORB, SOFR, DGS2, DGS10, T10Y2Y, MORTGAGE30US, CPIAUCSL, CPILFESL, "
            "PCEPI, PCEPILFE, UNRATE, PAYEMS, A191RL1Q225SBEA (real GDP growth), WALCL (Fed balance sheet). "
            "Use search_economic_series for others.",
            schema({
                "series_id": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "transform": {"type": "string", "enum": ["none", "yoy", "diff", "pct"]},
            }, ["series_id"]),
            get_economic_series,
        ),
        Tool(
            "search_economic_series",
            "Find FRED series ids by keyword in a curated catalog of ~55 key macro/financial series.",
            schema({"keywords": {"type": "string"}}, ["keywords"]),
            search_economic_series,
        ),
        Tool(
            "get_market_prices",
            "Daily closing prices from Yahoo Finance for indices (^GSPC, ^DJI, ^IXIC, ^VIX), Treasury yield "
            "indices (^IRX 13-week, ^FVX 5y, ^TNX 10y, ^TYX 30y; quoted in percent), stocks/ETFs (e.g. JPM, "
            "SPY), FX pairs (EURUSD=X, USDJPY=X), futures (GC=F gold, CL=F oil) and crypto (BTC-USD). Defaults "
            "to roughly the last two months.",
            schema({
                "symbol": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD (inclusive)"},
            }, ["symbol"]),
            get_market_prices,
        ),
        Tool(
            "calculator",
            "Evaluate an arithmetic expression exactly (+ - * / ** %, parentheses, abs, round, min, max, sqrt, "
            "log, exp). Use it for every computation instead of mental arithmetic.",
            schema({"expression": {"type": "string", "description": "e.g. (334.131/321.5 - 1)*100"}}, ["expression"]),
            calculator,
        ),
    ]
