#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightweight ETF snapshot. Does not replace etf-backend/etf_analyzer.py."""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

FUNDS = [
    ("518880", "sh", "华安黄金ETF"),
    ("520590", "sh", "恒生科技ETF"),
    ("510300", "sh", "沪深300ETF"),
    ("515070", "sh", "人工智能ETF"),
    ("159928", "sz", "主要消费ETF"),
    ("512760", "sh", "新能源车ETF"),
    ("512480", "sh", "半导体ETF"),
    ("513100", "sh", "纳指ETF"),
    ("159825", "sz", "农业ETF"),
    ("510410", "sh", "自然资源ETF"),
]

UA = "Mozilla/5.0 (compatible; etf-lite/1.0)"
CST = timezone(timedelta(hours=8))


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def output_path() -> Path:
    root = repo_root()
    tools = root / "tools"
    if tools.is_dir():
        return tools / "etf-latest.html"
    pub = root / "public"
    pub.mkdir(exist_ok=True)
    return pub / "etf-latest.html"


def http_get(url: str, extra_headers: dict | None = None, retries: int = 2) -> bytes:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last or RuntimeError("http failed")


def with_pct(rows: list[dict]) -> list[dict]:
    for i, row in enumerate(rows):
        if "pct" in row and row["pct"] is not None:
            continue
        if i == 0 or not rows[i - 1]["close"]:
            row["pct"] = 0.0
        else:
            prev = rows[i - 1]["close"]
            row["pct"] = (row["close"] / prev - 1) * 100
    return rows


def fetch_eastmoney(code: str, market: str, limit: int) -> list[dict]:
    secid = ("1." if market == "sh" else "0.") + code
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=1&end=20500101&lmt={limit}"
    )
    data = json.loads(http_get(url, {"Referer": "https://quote.eastmoney.com/"}).decode("utf-8"))
    klines = (data.get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 11:
            continue
        rows.append(
            {
                "date": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "pct": float(p[8]),
            }
        )
    return rows


def fetch_sina(code: str, market: str, limit: int) -> list[dict]:
    symbol = f"{market}{code}"
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={limit}"
    )
    raw = http_get(url, {"Referer": "https://finance.sina.com.cn/"}).decode("utf-8", "ignore")
    data = json.loads(raw)
    rows = []
    for item in data or []:
        rows.append(
            {
                "date": str(item["day"])[:10],
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
            }
        )
    return with_pct(rows)


def fetch_tencent(code: str, market: str, limit: int) -> list[dict]:
    symbol = f"{market}{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{limit},qfq"
    data = json.loads(http_get(url, {"Referer": "https://gu.qq.com/"}).decode("utf-8"))
    pack = ((data.get("data") or {}).get(symbol) or {})
    klines = pack.get("qfqday") or pack.get("day") or []
    rows = []
    for p in klines:
        if len(p) < 5:
            continue
        rows.append(
            {
                "date": str(p[0])[:10],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
            }
        )
    return with_pct(rows)


def fetch_klines(code: str, market: str, limit: int = 80) -> list[dict]:
    last_err: Exception | None = None
    for fn in (fetch_eastmoney, fetch_sina, fetch_tencent):
        try:
            rows = fn(code, market, limit)
            if len(rows) >= 25:
                return rows
            last_err = RuntimeError(f"{fn.__name__} K线不足")
        except Exception as e:
            last_err = e
    raise RuntimeError(str(last_err) if last_err else "无K线")


def sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100


def advice(close: float, ma20: float | None, rsi_v: float | None, r20: float | None) -> str:
    notes = []
    if ma20 is not None:
        notes.append("站上20日均线" if close >= ma20 else "跌破20日均线")
    if rsi_v is not None:
        if rsi_v >= 75:
            notes.append("RSI超买")
        elif rsi_v <= 25:
            notes.append("RSI超卖")
        else:
            notes.append("RSI中性")
    if r20 is not None:
        notes.append(("近20日+" if r20 >= 0 else "近20日") + f"{r20:.1f}%")
    if rsi_v is not None and rsi_v >= 75:
        tag = "谨慎"
    elif ma20 is not None and close >= ma20 and (rsi_v is None or rsi_v < 70):
        tag = "偏多"
    elif ma20 is not None and close < ma20:
        tag = "观望"
    else:
        tag = "中性"
    return tag + " · " + "，".join(notes)


def analyze_one(code: str, market: str, name: str) -> dict:
    rows = fetch_klines(code, market)
    if len(rows) < 25:
        raise RuntimeError("K线不足")
    closes = [r["close"] for r in rows]
    last = rows[-1]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    rsi_v = rsi(closes)
    r5 = ret(closes, 5)
    r20 = ret(closes, 20)
    return {
        "code": code,
        "market": market.upper(),
        "name": name,
        "date": last["date"],
        "close": last["close"],
        "pct": last["pct"],
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "rsi": rsi_v,
        "r5": r5,
        "r20": r20,
        "advice": advice(last["close"], ma20, rsi_v, r20),
        "error": "",
    }


def fmt(v, n=2, suffix=""):
    if v is None:
        return "—"
    return f"{v:.{n}f}{suffix}"


def render_html(rows: list[dict], generated: str) -> str:
    body_rows = []
    for r in rows:
        if r.get("error"):
            body_rows.append(
                f"<tr><td>{escape(r['name'])}</td><td>{escape(r['code'])}</td>"
                f"<td colspan='8' class='err'>{escape(r['error'])}</td></tr>"
            )
            continue
        cls = "up" if r["pct"] >= 0 else "down"
        body_rows.append(
            "<tr>"
            f"<td>{escape(r['name'])}</td>"
            f"<td>{escape(r['code'])}.{escape(r['market'])}</td>"
            f"<td>{r['date']}</td>"
            f"<td>{fmt(r['close'], 3)}</td>"
            f"<td class='{cls}'>{fmt(r['pct'], 2, '%')}</td>"
            f"<td>{fmt(r['ma5'], 3)}</td>"
            f"<td>{fmt(r['ma10'], 3)}</td>"
            f"<td>{fmt(r['ma20'], 3)}</td>"
            f"<td>{fmt(r['rsi'], 1)}</td>"
            f"<td>{escape(r['advice'])}</td>"
            "</tr>"
        )
    table = "\n".join(body_rows)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ETF 轻量日报</title>
<style>
  body {{ margin: 0; background: #0d1117; color: #e6edf3; font: 15px/1.5 "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 20px 14px 48px; }}
  a {{ color: #7ee787; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 8px; }}
  .meta {{ color: #8b949e; font-size: 13px; margin-bottom: 18px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ border: 1px solid #30363d; padding: 8px 7px; text-align: left; }}
  th {{ background: #161b22; }}
  .up {{ color: #f85149; }}
  .down {{ color: #3fb950; }}
  .err {{ color: #d29922; }}
  .note {{ margin-top: 18px; color: #8b949e; font-size: 13px; }}
  @media (max-width: 700px) {{
    table {{ font-size: 12px; display: block; overflow-x: auto; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <p><a href="../index.html">回首页</a></p>
  <h1>ETF 轻量日报</h1>
  <p class="meta">生成时间（北京）：{escape(generated)} · 行情来源东方财富 · 非投资建议</p>
  <table>
    <thead><tr>
      <th>名称</th><th>代码</th><th>日期</th><th>收盘</th><th>涨跌</th>
      <th>MA5</th><th>MA10</th><th>MA20</th><th>RSI</th><th>简评</th>
    </tr></thead>
    <tbody>
{table}
    </tbody>
  </table>
  <p class="note">这是 Railway 停服后的免费替代：GitHub Actions 每个交易日早晨自动刷新。不含机器学习/新闻情绪。原完整脚本仍在 my_website/etf-backend，未改。</p>
</div>
</body>
</html>
"""


def maybe_email(html: str, generated: str) -> None:
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    mail_to = os.environ.get("MAIL_TO", "").strip()
    if not (user and password and mail_to):
        print("skip email: SMTP secrets not set")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"ETF 轻量日报 {generated[:10]}"
    msg["From"] = user
    msg["To"] = mail_to
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30, context=ctx) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [e.strip() for e in mail_to.split(",") if e.strip()], msg.as_string())
    print("email sent")


def main() -> int:
    generated = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    rows = []
    for code, market, name in FUNDS:
        try:
            rows.append(analyze_one(code, market, name))
            print("ok", code, name)
        except Exception as e:
            print("fail", code, e)
            rows.append({"code": code, "market": market.upper(), "name": name, "error": str(e)})
    html = render_html(rows, generated)
    out = output_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("wrote", out)
    try:
        maybe_email(html, generated)
    except Exception as e:
        print("email failed:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
