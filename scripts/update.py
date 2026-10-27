#!/usr/bin/env python3
"""Piyasa terminali veri toplayıcı. GitHub Actions düzenli çalıştırır ve data.json üretir.
Her kaynak ayrı korunur: biri çökerse önceki veri kalır, sayfa boş kalmaz."""
import csv
import io
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; piyasa-terminali/1.0)"}
TR, ET = ZoneInfo("Europe/Istanbul"), ZoneInfo("America/New_York")
GUN = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
ERR = {}


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r


# ---------- fiyatlar (Yahoo Finance, resmî olmayan uç nokta) ----------
YF = [("S&P 500", "^GSPC", "idx"), ("Nasdaq", "^IXIC", "idx"), ("Dow", "^DJI", "idx"), ("VIX", "^VIX", "idx"),
      ("Altın", "GC=F", "usd"), ("Gümüş", "SI=F", "usd"), ("WTI", "CL=F", "usd"), ("Brent", "BZ=F", "usd"),
      ("DXY", "DX-Y.NYB", "idx"), ("ABD 10Y", "^TNX", "yield"), ("USD/TRY", "TRY=X", "idx"), ("BIST 100", "XU100.IS", "idx")]


def fetch_prices():
    out = []
    for name, sym, kind in YF:
        try:
            u = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?range=5d&interval=1d"
            res = get(u).json()["chart"]["result"][0]
            price = res["meta"].get("regularMarketPrice")
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            prev = closes[-2] if len(closes) >= 2 else res["meta"].get("previousClose")
            if price is None or not prev:
                continue
            if kind == "yield" and price > 20:
                price, prev = price / 10, prev / 10
            out.append({"n": name, "p": round(price, 4), "c": round((price / prev - 1) * 100, 2),
                        "d": round(price - prev, 3), "k": kind})
        except Exception as e:  # noqa: BLE001
            ERR["fiyat:" + name] = str(e)[:100]
        time.sleep(0.3)
    return out


def fetch_crypto():
    r = get("https://api.coingecko.com/api/v3/simple/price", params={
        "ids": "bitcoin,ethereum,solana,ripple,binancecoin", "vs_currencies": "usd", "include_24hr_change": "true"}).json()
    names = [("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana"), ("XRP", "ripple"), ("BNB", "binancecoin")]
    return [{"n": n, "p": r[i]["usd"], "c": round(r[i].get("usd_24h_change", 0), 2), "k": "usd"} for n, i in names if i in r]


# ---------- makro (FRED, anahtarsız CSV) ----------
FRED = [("Fed faizi (efektif)", "DFF", "%"), ("ABD 2 yıllık", "DGS2", "%"), ("ABD 10 yıllık", "DGS10", "%"),
        ("ABD 30 yıllık", "DGS30", "%"), ("10Y beklenen enflasyon", "T10YIE", "%"), ("30Y mortgage", "MORTGAGE30US", "%"),
        ("Yüksek getirili tahvil farkı", "BAMLH0A0HYM2", "%"), ("İşsizlik oranı", "UNRATE", "%"),
        ("Haftalık işsizlik başvurusu", "ICSA", ""), ("Tüketici güveni (Michigan)", "UMCSENT", "")]


def fetch_fred():
    out, start = [], (datetime.now() - timedelta(days=500)).strftime("%Y-%m-%d")
    for name, sid, unit in FRED:
        try:
            t = get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid, "cosd": start}).text
            rows = list(csv.reader(io.StringIO(t)))[1:]
            vals = [(d, float(v)) for d, v in rows if v not in (".", "")]
            out.append({"n": name, "v": vals[-1][1], "prev": vals[-2][1], "date": vals[-1][0], "u": unit})
        except Exception as e:  # noqa: BLE001
            ERR["fred:" + sid] = str(e)[:100]
    return out


# ---------- ABD veri takvimi + otomatik okuma ----------
SENS = {"h": [-1, -1.4, -1, -1.2, 1], "g": [.4, .7, -.4, .5, .6]}
ASSETS = ["BTC", "Altcoinler", "Altın", "Gümüş", "DXY"]
WHY = {
    "h": [("Likidite beklentisi daralır, risk iştahı düşer.", "Faiz indirimi beklentisi artar, likidite iyimserliği güçlenir."),
          ("Yüksek beta nedeniyle BTC’den sert düşer.", "Likidite artışından BTC’den fazla yararlanır."),
          ("Reel faiz ve dolar yükselir, altının elde tutma maliyeti artar.", "Reel faiz ve dolar geriler, altına talep artar."),
          ("Faiz ve dolar baskısı altında kalır, sanayi talebi beklentisi zayıflar.", "Faiz/dolar baskısı azalır, gümüş desteklenir."),
          ("Fed gevşeme beklentisi azalır, ABD faizleri ve dolar yükselir.", "Fed gevşeme beklentisi artar, faizler ve dolar geriler.")],
    "g": [("Güçlü büyüme risk iştahını destekler.", "Zayıf büyüme risk iştahını azaltır."),
          ("Risk iştahı artınca BTC’den fazla yükselir.", "Büyüme endişesinde BTC’den sert düşer."),
          ("Güçlü veri faizleri yükseltir, güvenli liman talebi azalır.", "Zayıf veri faizleri düşürür, güvenli liman talebi artar."),
          ("Sanayi talebi beklentisi güçlenir.", "Sanayi talebi endişesi gümüşü aşağı çeker."),
          ("Güçlü ABD verisi dolar talebini artırır.", "Zayıf veri dolar talebini azaltır.")],
}
RULES = [("unemployment rate", "h", -1, .1), ("unemployment claims", "h", -1, 10), ("average hourly", "h", 1, .1),
         ("non-farm", "h", 1, 50), ("cpi", "h", 1, .1), ("ppi", "h", 1, .1), ("pce", "h", 1, .1),
         ("federal funds", "h", 1, .25), ("retail sales", "g", 1, .3), ("ism", "g", 1, 1), ("gdp", "g", 1, .5),
         ("manufacturing index", "g", 1, 3)]


def num(s):
    if not s:
        return None
    s = str(s).strip().replace("%", "").replace(",", "")
    if s[-1:] in "KMBT":
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        return None


def read(ev):
    a, f = num(ev.get("actual")), num(ev.get("forecast"))
    if a is None or f is None:
        return None
    for kw, rg, sg, sc in RULES:
        if kw in ev["title"].lower():
            break
    else:
        return None
    diff = a - f
    mag = abs(diff) / sc
    d = (1 if diff > 0 else -1 if diff < 0 else 0) * sg
    res = []
    for i, n in enumerate(ASSETS):
        s = SENS[rg][i] * d * min(mag, 3) / 3
        v = 0 if abs(s) < .15 else (1 if s > 0 else -1)
        why = WHY[rg][i][0 if d > 0 else 1] if v else "Sürpriz küçük ya da veri beklentiyle uyumlu; yön belirleyici değil."
        res.append({"n": n, "v": v, "s": round(abs(s), 2), "why": why})
    return res


def fetch_calendar():
    evs = []
    for e in get("https://nfs.faireconomy.media/ff_calendar_thisweek.json").json():
        if e.get("country") != "USD":
            continue
        try:
            dt = datetime.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        t = dt.astimezone(TR)
        evs.append({"title": e.get("title", ""), "impact": e.get("impact", ""), "forecast": e.get("forecast", ""),
                    "previous": e.get("previous", ""), "actual": e.get("actual", ""),
                    "ts": int(dt.timestamp() * 1000), "tr": f"{GUN[t.weekday()]} {t:%d.%m %H:%M}"})
    for e in evs:
        e["reading"] = read(e)
    return sorted(evs, key=lambda x: x["ts"])


# ---------- haber akışları (RSS) + otomatik Türkçe + anahtar kelime etiketi ----------
FEEDS = [("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss", True),
         ("CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html", True),
         ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", True),
         ("FXStreet", "https://www.fxstreet.com/rss/news", True),
         ("ZeroHedge", "https://cms.zerohedge.com/fullrss2.xml", True),
         ("Bloomberg HT", "https://www.bloomberght.com/rss", False)]
UP = r"surge|jump|rall(y|ies|ied)|soar|climb|rise|rises|gain|record high|inflows?|rebound"
DN = r"plunge|tumble|slump|fall|falls|drop|slide|sell-?off|outflows?|slip|decline|crash"
HAWK = r"rate hike|hikes? rates|raise rates|hawkish|higher for longer|hot(ter)? (cpi|inflation|pce)|inflation (rose|accelerat|jump|surge)|yields? (rise|jump|surge|climb|hit)"
DOVE = r"rate cut|cuts? rates|dovish|inflation (eases|cools|slows|fell)|yields? (fall|drop|slip|retreat|tumble)"
OILUP = r"\b(oil|crude|brent|wti)\b.{0,25}(surge|jump|rall|soar|climb|rise|high)"
OILDN = r"\b(oil|crude|brent|wti)\b.{0,25}(fall|drop|slump|tumble|slide|slip|plunge)"
H = {"DXY": 1, "Altın": -1, "Gümüş": -1, "BTC": -1, "Altcoin": -1}


def classify(text):
    t, s = text.lower(), {}

    def add(d, m=1):
        for k, v in d.items():
            s[k] = s.get(k, 0) + v * m
    if re.search(HAWK, t):
        add(H)
    if re.search(DOVE, t):
        add(H, -1)
    if re.search(OILUP, t):
        add({"Petrol": 1, "DXY": 1, "Altın": -1, "BTC": -1})
    if re.search(OILDN, t):
        add({"Petrol": -1, "DXY": -1, "Altın": 1, "BTC": 1})
    up, dn = re.search(UP, t), re.search(DN, t)
    m = 1 if up and not dn else -1 if dn and not up else 0
    if m:
        if re.search(r"bitcoin|\bbtc\b|crypto|ether|solana|\bxrp\b", t):
            add({"BTC": 1, "Altcoin": 1}, m)
        if re.search(r"\bgold\b", t):
            add({"Altın": 1}, m)
        if re.search(r"\bsilver\b", t):
            add({"Gümüş": 1}, m)
        if re.search(r"dollar|\bdxy\b|greenback", t):
            add({"DXY": 1}, m)
    return [f"{k} {'▲' if v > 0 else '▼'}" for k, v in s.items() if v]


def clean(x):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x or "")).strip()


BUDGET = {"n": 80}


def translate(text):
    if not text or BUDGET["n"] <= 0:
        return None
    BUDGET["n"] -= 1
    r = get("https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "tr", "dt": "t", "q": text[:400]}).json()
    time.sleep(0.2)
    return "".join(p[0] for p in r[0] if p and p[0])


def fetch_news(cache):
    out = {}
    for name, url, tr in FEEDS:
        try:
            feed = feedparser.parse(get(url).content)
            items = []
            for e in feed.entries[:10]:
                link = e.get("link", "")
                title, summ = clean(e.get("title")), clean(e.get("summary"))[:300]
                old = cache.get(link, {})
                tt, ss = old.get("tt"), old.get("ss")
                if tr and not tt:
                    try:
                        tt, ss = translate(title), translate(summ) if summ else None
                    except Exception as ex:  # noqa: BLE001
                        ERR["çeviri"] = str(ex)[:100]
                pub = e.get("published_parsed")
                ts = int(time.mktime(pub)) * 1000 if pub else 0
                items.append({"t": title, "tt": tt, "s": summ, "ss": ss, "u": link, "ts": ts,
                              "tags": classify(title + " " + summ) if tr else []})
            if items:
                out[name] = items
        except Exception as ex:  # noqa: BLE001
            ERR["haber:" + name] = str(ex)[:100]
    return out


def main():
    old = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:  # noqa: BLE001
        pass

    def keep(name, fn):
        try:
            v = fn()
            if v:
                return v
            ERR[name] = "boş sonuç"
        except Exception as e:  # noqa: BLE001
            ERR[name] = str(e)[:140]
        return old.get(name)

    cache = {n["u"]: n for items in (old.get("news") or {}).values() for n in items}
    data = {"generated": datetime.now(timezone.utc).isoformat(),
            "prices": keep("prices", fetch_prices), "crypto": keep("crypto", fetch_crypto),
            "fred": keep("fred", fetch_fred), "events": keep("events", fetch_calendar),
            "news": keep("news", lambda: fetch_news(cache))}
    data["errors"] = ERR
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("tamam; hatalar:", ERR or "yok")


if __name__ == "__main__":
    main()
