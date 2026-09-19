#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backend FastAPI - Conseil d'investissement (cahier des charges, sections A-E).
=================================================================================

Endpoints :
  GET  /api/advice?symbol=AAPL            -> analyse complete d'une valeur
                                              (structure imbriquee, endpoint
                                              de reference)
  GET  /api/actions/{ticker}              -> meme analyse, alias en JSON
                                              plat conforme au contrat decrit
                                              dans la synthese de specifications
                                              (nom, cours_ouverture, rsi, ...)
  GET  /api/advice/batch?symbols=AAPL,MSFT,TSLA
                                            -> analyse groupee (une seule
                                               requete Yahoo pour tous les
                                               tickers, cf. section E)
  GET  /api/alerts?symbol=AAPL             -> alertes declenchees pour une
                                               valeur (regles par defaut ou
                                               regles personnalisees en JSON)
  GET  /api/health                         -> etat du service + cache

Lancement local :
    pip install -r requirements.txt
    export APP_MASTER_KEY=$(python secrets_util.py generate-master-key)
    python secrets_util.py set FMP_API_KEY "votre_nouvelle_cle_fmp"
    uvicorn app:app --reload --port 8000

Deploiement : ce processus doit tourner derriere un reverse proxy TLS
(Caddy/Nginx/Traefik, ou une plateforme qui termine le HTTPS pour vous -
Render, Fly.io, Railway...). Cette session ne peut pas provisionner de
certificat ni heberger un service en continu ; c'est a faire cote deploiement.
"""

from __future__ import annotations
import logging
import logging.handlers
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import analysis
import alerts as alerts_module
from cache import cache

# ---------------------------------------------------------------------------
# Logging (cahier des charges, section C) : console + fichier tournant.
# Toute bascule d'API (Yahoo <-> FMP) et toute erreur amont y est tracee.
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_DIR / "backend.log", maxBytes=2_000_000, backupCount=5),
    ],
)
logger = logging.getLogger("advice.api")

app = FastAPI(title="API Conseil d'Investissement", version="1.0")

# CORS : a restreindre au(x) domaine(s) reel(s) du frontend en production
# (ne JAMAIS laisser allow_origins=["*"] avec allow_credentials=True en prod).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled_error path=%s", request.url.path)
        return JSONResponse(status_code=500, content={
            "error": "Erreur interne du serveur.",
            "message": "Veuillez réessayer plus tard ou vérifier votre connexion internet.",
        })
    logger.info("request path=%s status=%d duration_ms=%d",
                request.url.path, response.status_code, int((time.time() - t0) * 1000))
    return response


@app.get("/api/health")
def health():
    return {"status": "ok", "cache_backend": cache.backend}


def _to_actions_contract(result: dict) -> dict:
    """Aplatit le resultat riche de analyze_stock() vers le contrat GET
    /api/actions/{ticker} demande dans la synthese de specifications (forme
    JSON plate). /api/advice reste l'endpoint de reference (structure
    imbriquee, plus complete) ; cette route existe pour s'aligner
    litteralement sur l'exemple de requete/reponse fourni."""
    paliers = result["strategie_achat"]["paliers"]
    strat_resume = " ; ".join(f"{p['pct_capital']}% {p['condition'].lower()}" for p in paliers)
    return {
        "nom": result["nom"],
        "devise": result["devise"],
        "cours_ouverture": result["seance"]["open"],
        "cours_cloture": result["seance"]["close"],
        "plus_haut": result["seance"]["high"],
        "plus_bas": result["seance"]["low"],
        "tendance_jour": result["seance"]["tendance_jour"],
        "tendance_7j": result["seance"]["tendance_7_seances"],
        "rsi": result["technique"]["rsi14"],
        "per": result["fondamental"]["per"],
        "dividende_annuel": result["dividendes"]["annuel_en_cours"],
        "conseil": result["conseil"],
        "niveau_risque": result["niveau_risque"],
        "strategie_achat": strat_resume,
        "eligible_pea": result["eligibilite_pea"]["estime"],
        "eligibilite_pea_detail": result["eligibilite_pea"],
    }


@app.get("/api/actions/{ticker}")
def get_action(ticker: str):
    """Contrat plat GET /api/actions/{ticker} tel que decrit dans la
    synthese de specifications. Alias de /api/advice avec une reponse
    aplatie ; utilisez /api/advice si vous avez besoin du detail complet
    (technique/fondamental/sentiment/risque decompose)."""
    ticker = ticker.upper().strip()
    try:
        result = analysis.analyze_stock(ticker)
    except analysis.UpstreamUnavailable:
        raise HTTPException(status_code=503, detail={
            "error": "Données indisponibles.",
            "message": f"Impossible de récupérer les données pour {ticker}. "
                       "Vérifiez votre connexion internet ou réessayez plus tard.",
        })
    except Exception:
        logger.exception("actions_failed ticker=%s", ticker)
        raise HTTPException(status_code=500, detail={
            "error": "Erreur d'analyse.",
            "message": "Une erreur inattendue est survenue pendant l'analyse. Réessayez plus tard.",
        })
    return _to_actions_contract(result)


@app.get("/api/advice")
def get_advice(symbol: str = Query(..., min_length=1, max_length=10)):
    symbol = symbol.upper().strip()
    try:
        result = analysis.analyze_stock(symbol)
    except analysis.UpstreamUnavailable as e:
        logger.error("upstream_unavailable symbol=%s error=%s", symbol, e)
        raise HTTPException(status_code=503, detail={
            "error": "Données indisponibles.",
            "message": f"Impossible de récupérer les données pour {symbol} auprès de Yahoo Finance ou "
                       "Financial Modeling Prep. Vérifiez votre connexion internet ou réessayez plus tard.",
        })
    except Exception:
        logger.exception("advice_failed symbol=%s", symbol)
        raise HTTPException(status_code=500, detail={
            "error": "Erreur d'analyse.",
            "message": "Une erreur inattendue est survenue pendant l'analyse. Réessayez plus tard.",
        })
    if result.get("source_bascule_api"):
        logger.warning("api_fallback_notified symbol=%s", symbol)
    return result


@app.get("/api/advice/batch")
def get_advice_batch(symbols: str = Query(..., description="Symboles separes par des virgules, ex: AAPL,MSFT,TSLA")):
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="Aucun symbole fourni.")
    if len(tickers) > 25:
        raise HTTPException(status_code=400, detail="Maximum 25 symboles par appel groupé.")

    # Pre-charge l'historique de tous les tickers en UN seul appel (yfinance),
    # au lieu d'un aller-retour reseau par valeur (cahier des charges, section E).
    analysis.fetch_batch_history(tickers)

    results, errors = {}, {}
    for t in tickers:
        try:
            results[t] = analysis.analyze_stock(t)
        except analysis.UpstreamUnavailable as e:
            errors[t] = str(e)
        except Exception as e:
            logger.exception("batch_item_failed symbol=%s", t)
            errors[t] = "Erreur inattendue."
    return {"results": results, "errors": errors}


@app.get("/api/alerts")
def get_alerts(symbol: str = Query(...)):
    symbol = symbol.upper().strip()
    try:
        result = analysis.analyze_stock(symbol)
    except analysis.UpstreamUnavailable as e:
        raise HTTPException(status_code=503, detail={
            "error": "Données indisponibles.",
            "message": "Impossible de vérifier les alertes : données de marché inaccessibles pour le moment.",
        })
    triggered = alerts_module.evaluate_rules(symbol, result)
    return {"symbol": symbol, "alerts": triggered, "checked_at": result["date_analyse"]}
