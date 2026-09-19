#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cache applicatif (cahier des charges, section E - Performance).
==================================================================

But : eviter de re-interroger Yahoo Finance / FMP a chaque requete utilisateur.
  - TTL_REALTIME (5 min)   : cours, indicateurs techniques, conseil/risque.
  - TTL_HISTORIQUE (1h)    : historique de prix utilise pour RSI/MACD/SMA,
                              ratios fondamentaux, dividendes.

Implementation : utilise Redis si REDIS_URL est configuree (recommande en
production - partage le cache entre plusieurs instances du backend) ; sinon
degrade automatiquement vers un cache fichier local (JSON + horodatage), ce
qui suffit pour un usage prototype / mono-instance sans dependance externe.
"""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

TTL_REALTIME = 5 * 60        # 5 minutes - donnees "temps reel"
TTL_HISTORIQUE = 60 * 60     # 1 heure - donnees historiques / fondamentales

_LOCAL_CACHE_DIR = Path(__file__).parent / ".cache"


class Cache:
    """Interface unique ; bascule Redis <-> fichier local de maniere transparente."""

    def __init__(self, redis_url: Optional[str] = None):
        self.backend = "file"
        self._redis = None
        redis_url = redis_url or os.environ.get("REDIS_URL")
        if redis_url:
            try:
                import redis  # pip install redis
                self._redis = redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                self.backend = "redis"
            except Exception:
                # Redis indisponible -> on continue avec le cache fichier,
                # sans faire echouer le demarrage du service.
                self._redis = None
                self.backend = "file"
        if self.backend == "file":
            _LOCAL_CACHE_DIR.mkdir(exist_ok=True)

    # -- API commune -------------------------------------------------------
    def get(self, key: str) -> Optional[Any]:
        if self.backend == "redis":
            raw = self._redis.get(key)
            return json.loads(raw) if raw else None
        path = _LOCAL_CACHE_DIR / f"{_safe(key)}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        if payload["expires_at"] < time.time():
            path.unlink(missing_ok=True)
            return None
        return payload["value"]

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if self.backend == "redis":
            self._redis.setex(key, ttl_seconds, json.dumps(value))
            return
        path = _LOCAL_CACHE_DIR / f"{_safe(key)}.json"
        path.write_text(json.dumps({"expires_at": time.time() + ttl_seconds, "value": value}))

    def get_or_set(self, key: str, ttl_seconds: int, compute_fn):
        cached = self.get(key)
        if cached is not None:
            return cached, True  # (valeur, etait_en_cache)
        value = compute_fn()
        self.set(key, value, ttl_seconds)
        return value, False


def _safe(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)


# Instance partagee par l'application (voir app.py)
cache = Cache()
