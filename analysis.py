#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coeur d'analyse financiere - version "backend" (avec logs, cache, secrets
chiffres et bascule Yahoo -> FMP tracee). Reprend la methodologie validee
dans stock_analysis_engine.py (technique 50% / fondamental 30% / sentiment
20%, risque pondere 30/20/20/15/15) et l'expose comme des fonctions
appelables par l'API (voir app.py), avec journalisation et mise en cache.
"""

from __future__ import annotations
import logging
import math
import os
import time
from datetime import datetime
from typing import Optional

import requests
import concurrent.futures

from cache import cache, TTL_REALTIME, TTL_HISTORIQUE
import secrets_util

logger = logging.getLogger("advice.analysis")

FMP_BASE = "https://financialmodelingprep.com/stable"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockAdviceBackend/1.0)"}

# Yahoo Finance (via yfinance) est utilise en repli pour les valeurs hors
# couverture du plan FMP gratuit (notamment Euronext Paris, cf. README).
# Ces appels ne prennent PAS de "timeout=" en interne et peuvent, sur
# certains tickers/reseaux, bloquer bien au-dela des 10s habituelles -
# jusqu'a ce que le navigateur de l'utilisateur abandonne (NetworkError).
# On leur impose donc un delai dur via un thread dedie : au-dela de
# YFINANCE_TIMEOUT_SECONDS, on abandonne ce repli et on renvoie une
# reponse degradee mais rapide plutot que de laisser la requete pendre.
YFINANCE_TIMEOUT_SECONDS = 15
_YF_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="yfinance")

SECTOR_AVG_PER = {
    "Technology": 30, "Tech": 30, "Communication Services": 26,
    "Consumer Cyclical": 24, "Consumer Discretionary": 24,
    "Consumer Defensive": 24, "Consumer Staples": 24,
    "Healthcare": 19, "Financial Services": 14, "Financials": 14,
    "Industrials": 22, "Basic Materials": 16, "Real Estate": 20,
    "Utilities": 18, "Energy": 12, "Energie": 12,
}
DEFAULT_SECTOR_AVG_PER = 22
RISK_SECTOR_UP = {"Technology", "Tech", "Biotechnology", "Biotech"}
RISK_SECTOR_DOWN = {"Utilities", "Energy", "Energie"}

# Pays dont le siege social ouvre droit au PEA (Union europeenne + Islande,
# Norvege, Liechtenstein - accord EEE). Le Royaume-Uni n'y figure plus depuis
# le Brexit (janvier 2021).
PEA_ELIGIBLE_COUNTRIES = {
    "France", "Germany", "Netherlands", "Belgium", "Luxembourg", "Italy", "Spain",
    "Portugal", "Ireland", "Austria", "Finland", "Sweden", "Denmark", "Poland",
    "Czech Republic", "Slovakia", "Hungary", "Slovenia", "Croatia", "Greece",
    "Cyprus", "Malta", "Estonia", "Latvia", "Lithuania", "Romania", "Bulgaria",
    "Iceland", "Norway", "Liechtenstein",
}

# Devise probable d'apres le suffixe boursier du ticker (Yahoo/FMP), utilisee
# UNIQUEMENT en dernier recours quand ni FMP ni Yahoo (yfinance) n'ont pu
# fournir de devise explicite - notamment pour les valeurs Euronext hors
# couverture du plan FMP gratuit (cf. fetch_company_profile). Sans ca, ces
# valeurs retombaient a tort sur "USD" par defaut (ex. Alstom affiche en $
# au lieu d'euros), meme si le prix affiche restait numeriquement correct.
TICKER_SUFFIX_DEVISE = {
    ".PA": "EUR", ".AS": "EUR", ".BR": "EUR", ".MC": "EUR", ".MI": "EUR",
    ".DE": "EUR", ".F": "EUR", ".LS": "EUR", ".VI": "EUR", ".HE": "EUR",
    ".IR": "EUR", ".L": "GBP", ".SW": "CHF", ".ST": "SEK", ".CO": "DKK",
    ".OL": "NOK", ".TO": "CAD", ".V": "CAD", ".HK": "HKD", ".T": "JPY",
    ".AX": "AUD", ".NZ": "NZD",
}


def _guess_devise_from_ticker(ticker: str) -> str:
    """Repli final (pas de FMP/Yahoo profile) : devine la devise a partir du
    suffixe de place boursiere. Sans suffixe reconnu, on suppose une valeur US
    (comportement precedent, inchange)."""
    upper = ticker.upper()
    for suffix, devise in TICKER_SUFFIX_DEVISE.items():
        if upper.endswith(suffix):
            return devise
    return "USD"


# Repli local pour l'affichage du nom (pas la source de verite des donnees
# de marche - uniquement utilise quand ni FMP ni Yahoo n'ont pu identifier la
# societe, ce qui est frequent pour Euronext Paris avec le plan FMP gratuit,
# cf. fetch_company_profile). Couvre les valeurs francaises les plus
# courantes (CAC 40 et quelques autres) a titre de confort d'affichage
# uniquement - une valeur absente de cette liste continue d'afficher son
# ticker brut, sans que cela affecte le calcul (cours, indicateurs...).
KNOWN_TICKER_NAMES = {
    "ALO.PA": "Alstom", "MC.PA": "LVMH", "TTE.PA": "TotalEnergies",
    "OR.PA": "L'Oreal", "SAN.PA": "Sanofi", "AIR.PA": "Airbus",
    "BNP.PA": "BNP Paribas", "AI.PA": "Air Liquide", "SU.PA": "Schneider Electric",
    "BN.PA": "Danone", "DG.PA": "Vinci", "EL.PA": "EssilorLuxottica",
    "RMS.PA": "Hermes", "CS.PA": "AXA", "SGO.PA": "Saint-Gobain",
    "ENGI.PA": "Engie", "VIE.PA": "Veolia", "CAP.PA": "Capgemini",
    "STLAP.PA": "Stellantis", "KER.PA": "Kering", "PUB.PA": "Publicis Groupe",
    "LR.PA": "Legrand", "ML.PA": "Michelin", "RI.PA": "Pernod Ricard",
    "GLE.PA": "Societe Generale", "ACA.PA": "Credit Agricole",
    "DSY.PA": "Dassault Systemes", "HO.PA": "Thales", "SW.PA": "Sodexo",
    "URW.PA": "Unibail-Rodamco-Westfield", "WLN.PA": "Worldline",
    "TEP.PA": "Teleperformance", "ORA.PA": "Orange", "EN.PA": "Bouygues",
    "STMPA.PA": "STMicroelectronics",
}


def _guess_name_from_ticker(ticker: str) -> str:
    """Repli final d'affichage : nom lisible pour les valeurs francaises les
    plus courantes ; sinon le ticker brut (comportement precedent, inchange)."""
    return KNOWN_TICKER_NAMES.get(ticker.upper(), ticker)


def _fmp_key() -> Optional[str]:
    """Cle FMP : priorite au coffre chiffre (secrets_util), repli sur la
    variable d'environnement FMP_API_KEY pour le confort en developpement."""
    try:
        key = secrets_util.get_secret("FMP_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("FMP_API_KEY")


class UpstreamUnavailable(Exception):
    """Aucune source de donnees (ni Yahoo, ni FMP) n'a pu repondre."""


# ---------------------------------------------------------------------------
# Recuperation des donnees (avec cache + logs + bascule tracee)
# ---------------------------------------------------------------------------

# Stooq (stooq.com) : source de cours historiques gratuite, sans cle API,
# documentee et utilisee notamment par la bibliotheque pandas-datareader.
# Testee ici comme alternative a l'API non officielle de Yahoo Finance pour
# les places europeennes, ou Yahoo s'est montre parfois peu fiable (cours
# fige/perime constate sur Alstom). Best-effort : en cas d'echec (place non
# couverte, Stooq injoignable, format inattendu), on renvoie une liste vide
# et fetch_yahoo_history bascule silencieusement sur Yahoo - aucune casse
# possible pour les tickers deja fonctionnels (US notamment).
STOOQ_COUNTRY_SUFFIX = {
    ".PA": "fr", ".DE": "de", ".L": "uk", ".MI": "it", ".AS": "nl",
    ".BR": "be", ".LS": "pt", ".SW": "ch",
}
# Nombre de seances approximatif par plage demandee (Stooq renvoie tout
# l'historique disponible en un seul appel ; on le tronque a une fenetre
# comparable a ce que Yahoo aurait renvoye pour ne pas fausser les
# indicateurs techniques, qui sont sensibles a la longueur de la serie
# fournie - cf. rsi14/macd, calcules sur l'integralite de `closes`).
STOOQ_RANGE_DAYS = {"5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "9mo": 189, "1y": 252, "2y": 504}


def _stooq_symbol(ticker: str) -> Optional[str]:
    """Convertit un ticker au format Yahoo (ex. ALO.PA) vers le format Stooq
    (ex. alo.fr). None si la place n'est pas dans notre liste couverte."""
    upper = ticker.upper()
    for suffix, country in STOOQ_COUNTRY_SUFFIX.items():
        if upper.endswith(suffix):
            return f"{upper[:-len(suffix)].lower()}.{country}"
    return None


def fetch_stooq_history(ticker: str, range_: str = "9mo") -> list[dict]:
    symbol = _stooq_symbol(ticker)
    if not symbol:
        return []
    try:
        r = requests.get("https://stooq.com/q/d/l/", params={"s": symbol, "i": "d"},
                          headers=HEADERS, timeout=10)
        r.raise_for_status()
        text = r.text.strip()
        if not text or "," not in text or text.lower().startswith("no data"):
            logger.warning("stooq_history_empty ticker=%s symbol=%s", ticker, symbol)
            return []
        lines = text.splitlines()
        header = lines[0].split(",")
        rows = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) != len(header):
                continue
            row = dict(zip(header, parts))
            try:
                rows.append({
                    "date": row["Date"],
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                    "volume": int(float(row["Volume"])) if row.get("Volume") else 0,
                })
            except (KeyError, ValueError):
                continue
        if not rows:
            return []
        days = STOOQ_RANGE_DAYS.get(range_, 189)
        rows = rows[-days:]
        logger.info("stooq_history_ok ticker=%s symbol=%s rows=%d", ticker, symbol, len(rows))
        return rows
    except Exception as e:
        logger.warning("stooq_history_failed ticker=%s symbol=%s error=%s", ticker, symbol, e)
        return []


def fetch_yahoo_history(ticker: str, range_: str = "9mo") -> list[dict]:
    def _do_fetch():
        stooq_rows = fetch_stooq_history(ticker, range_)
        if stooq_rows:
            return stooq_rows
        url = YAHOO_CHART_URL.format(ticker=ticker)
        t0 = time.time()
        r = requests.get(url, params={"interval": "1d", "range": range_}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        ts = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        rows = [
            {"date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
             "open": quote["open"][i], "high": quote["high"][i],
             "low": quote["low"][i], "close": quote["close"][i], "volume": quote["volume"][i]}
            for i, t in enumerate(ts) if quote["close"][i] is not None
        ]
        logger.info("yahoo_history_ok ticker=%s rows=%d duration_ms=%d", ticker, len(rows),
                    int((time.time() - t0) * 1000))
        return rows

    value, from_cache = cache.get_or_set(f"yahoo_hist:{ticker}:{range_}", TTL_HISTORIQUE, _do_fetch)
    logger.debug("yahoo_history cache_hit=%s ticker=%s", from_cache, ticker)
    return value


def fetch_batch_history(tickers: list[str], range_: str = "9mo") -> dict[str, list[dict]]:
    """Regroupe plusieurs tickers en un seul appel (yfinance) plutot que N
    appels separes - reduit fortement le nombre de requetes API sortantes."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance absent, repli sur des appels individuels pour %s", tickers)
        return {t: fetch_yahoo_history(t, range_) for t in tickers}

    def _do_fetch():
        t0 = time.time()

        def _download():
            return yf.download(" ".join(tickers), period=range_, interval="1d",
                                group_by="ticker", progress=False, threads=True)

        try:
            raw = _YF_EXECUTOR.submit(_download).result(timeout=YFINANCE_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning("yahoo_batch_timeout tickers=%s timeout_s=%s", tickers, YFINANCE_TIMEOUT_SECONDS)
            return {t: fetch_yahoo_history(t, range_) for t in tickers}
        out: dict[str, list[dict]] = {}
        for t in tickers:
            try:
                df = raw[t] if len(tickers) > 1 else raw
                rows = [
                    {"date": idx.strftime("%Y-%m-%d"), "open": float(r["Open"]), "high": float(r["High"]),
                     "low": float(r["Low"]), "close": float(r["Close"]), "volume": int(r["Volume"])}
                    for idx, r in df.dropna().iterrows()
                ]
                out[t] = rows
            except Exception as e:
                logger.warning("batch_history_partial_failure ticker=%s error=%s", t, e)
                out[t] = fetch_yahoo_history(t, range_)
        logger.info("yahoo_batch_history_ok tickers=%s duration_ms=%d", tickers, int((time.time() - t0) * 1000))
        return out

    cache_key = "yahoo_batch:" + ",".join(sorted(tickers)) + f":{range_}"
    value, from_cache = cache.get_or_set(cache_key, TTL_HISTORIQUE, _do_fetch)
    logger.debug("batch_history cache_hit=%s tickers=%s", from_cache, tickers)
    return value


def fmp_get(endpoint: str, symbol: str, **params) -> Optional[list | dict]:
    key = _fmp_key()
    if not key:
        logger.error("fmp_key_missing endpoint=%s symbol=%s", endpoint, symbol)
        return None
    params = {"symbol": symbol, "apikey": key, **params}

    def _do_fetch():
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            logger.warning("fmp_error endpoint=%s symbol=%s status=%d", endpoint, symbol, r.status_code)
            return None
        return r.json()

    value, from_cache = cache.get_or_set(f"fmp:{endpoint}:{symbol}:{params}", TTL_HISTORIQUE, _do_fetch)
    return value


def fetch_price_snapshot(ticker: str) -> tuple[dict, bool]:
    """Cours/quote en 'temps reel' (TTL court). Tente FMP en priorite (donnee
    deja pretraitee : priceAvg50/200 inclus) ; bascule vers Yahoo si FMP echoue,
    et journalise explicitement la bascule (cahier des charges, section C)."""

    def _do_fetch():
        fmp_quote = fmp_get("quote", ticker)
        if fmp_quote:
            return {"source": "fmp", "data": fmp_quote[0]}
        logger.warning("api_fallback from=fmp to=yahoo ticker=%s reason=fmp_unavailable", ticker)
        hist = fetch_yahoo_history(ticker, range_="5d")
        if not hist:
            raise UpstreamUnavailable(f"Ni FMP ni Yahoo Finance n'ont repondu pour {ticker}")
        last = hist[-1]
        return {"source": "yahoo_fallback", "data": {
            "price": last["close"], "open": last["open"], "dayLow": last["low"], "dayHigh": last["high"],
            "previousClose": hist[-2]["close"] if len(hist) > 1 else last["open"],
        }}

    value, from_cache = cache.get_or_set(f"snapshot:{ticker}", TTL_REALTIME, _do_fetch)
    bascule = value["source"] != "fmp"
    return value["data"], bascule


# ---------------------------------------------------------------------------
# Indicateurs techniques (identique a stock_analysis_engine.py)
# ---------------------------------------------------------------------------

def ema_series(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi14(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0)); losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd(closes: list[float]) -> Optional[dict]:
    if len(closes) < 35:
        return None
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    line = [a - b for a, b in zip(e12, e26)]
    signal = ema_series(line, 9)
    return {"macd": line[-1], "signal": signal[-1], "histogram": line[-1] - signal[-1],
            "bullish_cross": line[-1] > signal[-1]}


def trend_over_n_sessions(closes: list[float], n: int = 7) -> dict:
    if len(closes) < 2 * n:
        n = max(1, len(closes) // 2)
    last, prev = closes[-n:], closes[-2 * n:-n]
    avg_last, avg_prev = sum(last) / len(last), sum(prev) / len(prev) if prev else (sum(last) / len(last))
    variation = (avg_last - avg_prev) / avg_prev * 100 if avg_prev else 0
    trend = "Stable" if abs(variation) < 1 else ("Hausse" if variation > 0 else "Baisse")
    return {"variation_pct": variation, "tendance": trend}


def annualized_volatility_pct(closes: list[float]) -> float:
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return (var ** 0.5) * (252 ** 0.5) * 100


def buy_sell_volume(hist: list[dict], n: int = 14) -> tuple[int, int]:
    recent = hist[-n:]
    buy = sum(r["volume"] for i, r in enumerate(recent) if i > 0 and r["close"] >= recent[i - 1]["close"])
    sell = sum(r["volume"] for i, r in enumerate(recent) if i > 0 and r["close"] < recent[i - 1]["close"])
    return buy, sell


def day_trend(open_: float, close: float) -> tuple[str, float]:
    variation = (close - open_) / open_ * 100 if open_ else 0
    if abs(variation) < 0.5:
        return "Stable", variation
    return ("Hausse" if variation > 0 else "Baisse"), variation


# ---------------------------------------------------------------------------
# Sentiment (NewsAPI reel si cle presente, sinon degrade proprement)
# ---------------------------------------------------------------------------

def get_news_sentiment(ticker: str, company_name: str) -> dict:
    positive_kw = ["beat", "surge", "record", "growth", "upgrade", "partnership", "hausse",
                   "croissance", "bénéfice", "relève", "raise", "strong", "buy"]
    negative_kw = ["miss", "plunge", "scandal", "lawsuit", "downgrade", "recall", "baisse",
                   "chute", "amende", "fraud", "sell-off", "cuts", "warning", "probe", "strike", "grève"]
    try:
        newsapi_key = secrets_util.get_secret("NEWSAPI_KEY")
    except Exception:
        # APP_MASTER_KEY absent (ex. deploiement sans coffre chiffre configure) :
        # on degrade proprement sur la variable d'environnement en clair plutot
        # que de faire planter l'analyse.
        newsapi_key = None
    newsapi_key = newsapi_key or os.environ.get("NEWSAPI_KEY")
    if not newsapi_key:
        logger.info("newsapi_key_missing ticker=%s -> sentiment neutre par defaut", ticker)
        return {"tonalite": "Neutre", "resume": "Cle NewsAPI non configuree.", "score_lexical": 0}

    def _do_fetch():
        r = requests.get("https://newsapi.org/v2/everything",
                          params={"q": f'{ticker} OR "{company_name}"', "language": "en",
                                  "sortBy": "publishedAt", "pageSize": 15, "apiKey": newsapi_key}, timeout=10)
        return r.json().get("articles", [])

    articles, _ = cache.get_or_set(f"news:{ticker}", TTL_REALTIME, _do_fetch)
    score, headlines = 0, []
    for a in (articles or [])[:15]:
        title = (a.get("title") or "").lower()
        headlines.append(a.get("title"))
        score += sum(1 for kw in positive_kw if kw in title)
        score -= sum(1 for kw in negative_kw if kw in title)
    tonalite = "Positive" if score > 1 else ("Negative" if score < -1 else "Neutre")
    return {"tonalite": tonalite, "resume": " / ".join(h for h in headlines[:3] if h), "score_lexical": score}


# ---------------------------------------------------------------------------
# Scoring & decisions (regles litterales du cahier des charges)
# ---------------------------------------------------------------------------

def score_technique(rsi, macd_bull, price, sma200, buy_vol, sell_vol) -> float:
    score = 5.0
    if rsi is not None:
        if rsi > 70: score -= 3
        elif rsi < 30: score += 3
    if sma200 is not None:
        score += 2 if price > sma200 else -2
    if buy_vol is not None and sell_vol is not None:
        score += 2 if buy_vol > sell_vol else -2
    if macd_bull is not None:
        score += 1 if macd_bull else -1
    return max(0, min(10, score))


def score_fondamental(per, per_secteur_moyen, debt_to_equity, revenue_growth) -> float:
    score = 5.0
    if per is not None:
        score += 2 if per < per_secteur_moyen else -1
    if debt_to_equity is not None:
        score += 2 if debt_to_equity < 1 else -1
    if revenue_growth is not None:
        if revenue_growth > 0.10: score += 2
        elif revenue_growth < 0: score -= 2
    return max(0, min(10, score))


def score_sentiment(tonalite, buy_ratio) -> float:
    score = 5.0
    if tonalite == "Negative": score -= 3
    elif tonalite == "Positive": score += 3
    if buy_ratio is not None:
        if buy_ratio > 0.6: score += 2
        elif buy_ratio < 0.4: score -= 2
    return max(0, min(10, score))


def decision_conseil(score_global, rsi, tonalite) -> str:
    rsi = 50 if rsi is None else rsi
    if score_global >= 7 and rsi < 30 and tonalite == "Positive":
        return "Acheter"
    if score_global <= 3 and rsi > 70 and tonalite == "Negative":
        return "Vendre"
    return "Conserver"


def niveau_risque(volatilite_pct, secteur, debt_to_equity, market_cap, tonalite) -> dict:
    slope_vol = (8 - 3) / (30 - 15)
    r_vol = 3 + (volatilite_pct - 15) * slope_vol
    r_secteur = 5 + 2 if secteur in RISK_SECTOR_UP else (5 - 1 if secteur in RISK_SECTOR_DOWN else 5)
    if debt_to_equity is None: r_dette = 5
    elif debt_to_equity <= 0.5: r_dette = 2
    elif debt_to_equity >= 2: r_dette = 9
    else: r_dette = 2 + (debt_to_equity - 0.5) * (9 - 2) / (2 - 0.5)
    if market_cap is None: r_taille = 5
    elif market_cap < 2e9: r_taille = 7
    elif market_cap > 100e9: r_taille = 4
    else:
        lo, hi = math.log10(2e9), math.log10(100e9)
        frac = (math.log10(market_cap) - lo) / (hi - lo)
        r_taille = 7 + frac * (4 - 7)
    r_actus = 5 + 3 if tonalite == "Negative" else (5 - 1 if tonalite == "Positive" else 5)
    total = 0.30 * r_vol + 0.20 * r_secteur + 0.20 * r_dette + 0.15 * r_taille + 0.15 * r_actus
    return {"final": max(1, min(10, round(total))),
            "detail": {"volatilite_annualisee_pct": round(volatilite_pct, 1), "score_volatilite": round(max(1, min(10, r_vol)), 2),
                       "score_secteur": r_secteur, "score_dette_equite": round(r_dette, 2),
                       "score_taille": round(r_taille, 2), "score_actualites": r_actus}}


def fetch_company_profile(ticker: str) -> dict:
    """Profil societe (nom, secteur, pays du siege social). FMP en priorite ;
    repli sur Yahoo (yfinance) si FMP echoue - notamment pour les valeurs hors
    couverture du plan API FMP actuel (Euronext Paris et autres marches non
    US, cf. README section 'Marche europeen'). Le pays sert aussi a estimer
    l'eligibilite PEA (voir eligibilite_pea ci-dessous)."""
    def _do_fetch():
        fmp_profile = (fmp_get("profile", ticker) or [{}])[0]
        if fmp_profile.get("companyName"):
            return {"source": "fmp", "company_name": fmp_profile.get("companyName"),
                    "sector": fmp_profile.get("sector"), "country": fmp_profile.get("country"),
                    "currency": fmp_profile.get("currency") or _guess_devise_from_ticker(ticker)}
        def _yahoo_profile_lookup():
            import yfinance as yf
            return yf.Ticker(ticker).info or {}

        try:
            info = _YF_EXECUTOR.submit(_yahoo_profile_lookup).result(timeout=YFINANCE_TIMEOUT_SECONDS)
            if info.get("longName") or info.get("shortName"):
                logger.info("profile_fallback from=fmp to=yahoo ticker=%s reason=fmp_unavailable", ticker)
                return {"source": "yahoo_fallback", "company_name": info.get("longName") or info.get("shortName"),
                        "sector": info.get("sector"), "country": info.get("country"),
                        "currency": info.get("currency") or _guess_devise_from_ticker(ticker)}
        except concurrent.futures.TimeoutError:
            logger.warning("yahoo_profile_timeout ticker=%s timeout_s=%s", ticker, YFINANCE_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning("yahoo_profile_failed ticker=%s error=%s", ticker, e)
        return {"source": "none", "company_name": _guess_name_from_ticker(ticker), "sector": None, "country": None,
                "currency": _guess_devise_from_ticker(ticker)}

    value, from_cache = cache.get_or_set(f"profile:{ticker}", TTL_HISTORIQUE, _do_fetch)
    return value


def eligibilite_pea(country: Optional[str], sector: Optional[str]) -> dict:
    """Estimation automatique - PAS une verification officielle : il n'existe
    pas de liste publique d'eligibilite au PEA consultable par API (ni AMF, ni
    Euronext, ni Bercy). Regle generale (code monetaire et financier, art.
    L221-31) : la societe doit avoir son siege social dans un Etat de l'Union
    europeenne, ou en Islande/Norvege/Liechtenstein (EEE), et etre soumise a
    l'impot sur les societes. Cas particuliers NON couverts par cette
    estimation : foncieres cotees a statut SIIC (generalement exclues du PEA
    meme francaises), structures de holding complexes, societes recemment
    radiees/nationalisees (ex. EDF, retiree d'Euronext en 2023). A verifier
    aupres de votre courtier avant tout achat en PEA - ceci n'est pas un
    conseil fiscal ou juridique."""
    if not country:
        return {"estime": None, "pays_siege": None,
                "avertissement": "Pays du siège social non identifié — éligibilité PEA indéterminable automatiquement."}
    eligible = country in PEA_ELIGIBLE_COUNTRIES
    avertissement = None
    if country == "United Kingdom":
        avertissement = "Le Royaume-Uni n'est plus éligible au PEA depuis le Brexit (janvier 2021)."
    elif eligible and sector and "real estate" in sector.lower():
        avertissement = ("Secteur immobilier détecté : les foncières cotées à statut SIIC sont "
                          "généralement exclues du PEA même lorsqu'elles sont françaises — à vérifier "
                          "avant tout achat.")
    return {"estime": eligible, "pays_siege": country, "avertissement": avertissement}


def strategie_achat(risque: int, prix_actuel: float) -> dict:
    if risque <= 4:
        return {"type": "Achat en une fois au cours actuel",
                "paliers": [{"pct_capital": 100, "condition": "Immediat au cours actuel", "prix": round(prix_actuel, 2)}]}
    if risque <= 7:
        return {"type": "Achats fractionnes avec seuils de baisse",
                "paliers": [{"pct_capital": 50, "condition": "Immediat au cours actuel", "prix": round(prix_actuel, 2)},
                            {"pct_capital": 30, "condition": "Si baisse de 5%", "prix": round(prix_actuel * 0.95, 2)},
                            {"pct_capital": 20, "condition": "Si baisse de 10%", "prix": round(prix_actuel * 0.90, 2)}]}
    return {"type": "Achats fractionnes avec stops et ventes partielles",
            "paliers": [{"pct_capital": 40, "condition": "Immediat au cours actuel", "prix": round(prix_actuel, 2)},
                        {"pct_capital": 30, "condition": "Si baisse de 8%", "prix": round(prix_actuel * 0.92, 2)},
                        {"pct_capital": 30, "condition": "Si baisse de 15%", "prix": round(prix_actuel * 0.85, 2)}]}


# ---------------------------------------------------------------------------
# Fonction principale exposee a l'API
# ---------------------------------------------------------------------------

def analyze_stock(ticker: str) -> dict:
    t0 = time.time()
    logger.info("analyze_stock_start ticker=%s", ticker)

    hist = fetch_yahoo_history(ticker)
    if not hist:
        logger.error("analyze_stock_failed ticker=%s reason=no_history", ticker)
        raise UpstreamUnavailable(f"Aucune donnee historique disponible pour {ticker}")
    closes = [r["close"] for r in hist]
    today = hist[-1]

    snapshot, bascule = fetch_price_snapshot(ticker)
    price = snapshot.get("price", today["close"])
    sma50 = snapshot.get("priceAvg50")
    sma200 = snapshot.get("priceAvg200")
    market_cap = snapshot.get("marketCap")

    r14 = rsi14(closes)
    m = macd(closes)
    t7 = trend_over_n_sessions(closes, 7)
    vol_pct = annualized_volatility_pct(closes)
    buy_vol, sell_vol = buy_sell_volume(hist, 14)
    tj_label, tj_pct = day_trend(today["open"], today["close"])

    ratios = (fmp_get("ratios-ttm", ticker) or [{}])[0]
    key_metrics = (fmp_get("key-metrics-ttm", ticker) or [{}])[0]
    growth = (fmp_get("income-statement-growth", ticker) or [{}])[0]
    target = (fmp_get("price-target-consensus", ticker) or [{}])[0]
    grades = (fmp_get("grades-consensus", ticker) or [{}])[0]
    dividends = fmp_get("dividends", ticker, limit=8) or []
    profile = fetch_company_profile(ticker)

    company_name = profile.get("company_name", ticker)
    secteur = profile.get("sector")
    devise = profile.get("currency") or "USD"
    pea = eligibilite_pea(profile.get("country"), secteur)
    per = ratios.get("priceToEarningsRatioTTM")
    debt_eq = ratios.get("debtToEquityRatioTTM")
    div_yield = ratios.get("dividendYieldTTM")
    rev_growth = growth.get("growthRevenue")
    eps_ttm = key_metrics.get("netIncomePerShareTTM") or ratios.get("netIncomePerShareTTM")
    per_secteur_moyen = SECTOR_AVG_PER.get(secteur, DEFAULT_SECTOR_AVG_PER)

    news = get_news_sentiment(ticker, company_name)

    total_grades = sum(grades.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    buy_ratio = ((grades.get("strongBuy", 0) + grades.get("buy", 0)) / total_grades) if total_grades else None

    sc_tech = score_technique(r14, m["bullish_cross"] if m else None, price, sma200, buy_vol, sell_vol)
    sc_fond = score_fondamental(per, per_secteur_moyen, debt_eq, rev_growth)
    sc_sent = score_sentiment(news["tonalite"], buy_ratio)
    score_global = round(sc_tech * 0.5 + sc_fond * 0.3 + sc_sent * 0.2, 2)
    conseil = decision_conseil(score_global, r14, news["tonalite"])
    risque = niveau_risque(vol_pct, secteur, debt_eq, market_cap, news["tonalite"])

    # Chacune des trois composantes de l'objectif retombe sur `price` (le
    # cours actuel) quand la donnee source lui manque (SMA50/200, EPS, ou
    # consensus analystes FMP - frequemment indisponibles hors US avec le
    # plan gratuit, cf. README). Sans ce suivi, un ticker qui n'a AUCUNE de
    # ces trois donnees affichait un "objectif 3 mois" qui n'etait en realite
    # que le cours actuel recopie trois fois - une fausse precision a corriger
    # cote affichage plutot que cote calcul (les calculs eux-memes restent
    # corrects avec les donnees disponibles).
    technique_disponible = bool(sma50 and sma200)
    obj_technique = sma50 * (1 + (sma50 - sma200) / sma200 * 0.5) if technique_disponible else price
    per_cible = min(per, 30) if per else 20
    fondamental_disponible = bool(eps_ttm)
    obj_fondamental = eps_ttm * per_cible * 1.05 if fondamental_disponible else price
    consensus_disponible = target.get("targetConsensus") is not None
    obj_consensus = target.get("targetConsensus", price)
    objectif_3m = round(obj_technique * 0.3 + obj_fondamental * 0.3 + obj_consensus * 0.4, 2)
    nb_composantes_dispo = sum([technique_disponible, fondamental_disponible, consensus_disponible])

    div_annuel = round(sum(d.get("dividend", 0) for d in dividends[:4]), 4) if dividends else 0

    result = {
        "ticker": ticker, "nom": company_name, "secteur": secteur, "devise": devise,
        "date_analyse": datetime.now().isoformat(timespec="seconds"),
        "source_bascule_api": bascule,
        "eligibilite_pea": pea,
        "seance": {"open": today["open"], "close": price, "high": today["high"], "low": today["low"],
                   "tendance_jour": tj_label, "variation_jour_pct": round(tj_pct, 2),
                   "tendance_7_seances": t7["tendance"], "variation_7_seances_pct": round(t7["variation_pct"], 2)},
        "technique": {"rsi14": round(r14, 1) if r14 is not None else None, "macd": m,
                      "sma50": round(sma50, 2) if sma50 else None, "sma200": round(sma200, 2) if sma200 else None,
                      "volume_acheteur_14j": buy_vol, "volume_vendeur_14j": sell_vol},
        "fondamental": {"per": per, "per_secteur_moyen": per_secteur_moyen, "roe": key_metrics.get("returnOnEquityTTM"),
                        "dette_equite": debt_eq, "croissance_ca_yoy": rev_growth, "eps_ttm": eps_ttm,
                        "market_cap": market_cap},
        "sentiment": {"tonalite_actualites": news["tonalite"], "resume": news["resume"],
                      "consensus_analystes": grades.get("consensus"), "pct_buy": buy_ratio},
        "scoring": {"technique": sc_tech, "fondamental": sc_fond, "sentiment": sc_sent, "global": score_global},
        "conseil": conseil,
        "niveau_risque": risque["final"], "niveau_risque_detail": risque["detail"],
        "objectif_3_mois": {"retenu": objectif_3m,
                            "potentiel_rendement_pct": round((objectif_3m - price) / price * 100, 2),
                            "fiable": nb_composantes_dispo > 0,
                            "composantes_disponibles": nb_composantes_dispo,
                            "avertissement": (
                                None if nb_composantes_dispo > 0 else
                                "Aucune donnee de reference disponible (moyennes mobiles, resultat par "
                                "action, consensus analystes) pour cette valeur avec le plan API actuel "
                                "- cet objectif n'est pas significatif, il ne fait que refleter le cours "
                                "actuel. A ignorer pour cette valeur."
                            )},
        "dividendes": {"annuel_en_cours": div_annuel,
                      "rendement_pct": round(div_yield * 100, 2) if div_yield else None,
                      "prochaine_date_versement": dividends[0].get("paymentDate") if dividends else None},
        "strategie_achat": {**strategie_achat(risque["final"], price), "objectif_vise": objectif_3m},
    }
    logger.info("analyze_stock_done ticker=%s duration_ms=%d conseil=%s bascule=%s",
                ticker, int((time.time() - t0) * 1000), conseil, bascule)
    return result
