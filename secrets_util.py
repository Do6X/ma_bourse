#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chiffrement des cles API au repos (cahier des charges, section D - Securite).
==============================================================================

Principe :
  - Une cle maitre (MASTER_KEY) vit UNIQUEMENT dans une variable d'environnement
    (jamais dans le code, jamais commitee). Elle sert a chiffrer/dechiffrer les
    cles API tierces (FMP, NewsAPI...) stockees dans un fichier `.secrets.enc`
    a cote de l'application.
  - Les cles API en clair ne sont donc jamais presentes ni dans le code source,
    ni dans les logs, ni dans le fichier stocke sur disque.

Usage :
    # 1) Generer une cle maitre une seule fois et la mettre de cote (gestionnaire
    #    de secrets, variable d'environnement du serveur de deploiement, etc.) :
    python secrets_util.py generate-master-key

    # 2) Chiffrer une cle API et l'ecrire dans .secrets.enc :
    export APP_MASTER_KEY="<cle generee a l'etape 1>"
    python secrets_util.py set FMP_API_KEY "votre_cle_fmp_en_clair"
    python secrets_util.py set NEWSAPI_KEY "votre_cle_newsapi_en_clair"

    # 3) A l'execution, le backend appelle get_secret("FMP_API_KEY") qui
    #    dechiffre a la volee (la cle en clair ne touche jamais le disque).

IMPORTANT : toute cle FMP qui a pu transiter en clair (chat, capture
d'ecran, etc.) doit etre consideree comme compromise -> generez une nouvelle
cle FMP depuis votre tableau de bord FMP et chiffrez UNIQUEMENT la nouvelle
cle avec cet outil. Ne collez JAMAIS la valeur d'une cle API en clair dans ce
fichier ni dans aucun commentaire du code source (meme temporairement).
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("Ce module necessite 'cryptography' : pip install cryptography", file=sys.stderr)
    raise

SECRETS_FILE = Path(__file__).parent / ".secrets.enc"
MASTER_KEY_ENV = "APP_MASTER_KEY"


def _get_fernet() -> Fernet:
    master = os.environ.get(MASTER_KEY_ENV)
    if not master:
        raise RuntimeError(
            f"Variable d'environnement {MASTER_KEY_ENV} absente. "
            "Generez-en une avec: python secrets_util.py generate-master-key"
        )
    return Fernet(master.encode())


def _load_store() -> dict:
    if not SECRETS_FILE.exists():
        return {}
    return json.loads(SECRETS_FILE.read_text())


def _save_store(store: dict) -> None:
    SECRETS_FILE.write_text(json.dumps(store, indent=2))
    os.chmod(SECRETS_FILE, 0o600)  # lecture/ecriture proprietaire uniquement


def set_secret(name: str, plaintext_value: str) -> None:
    f = _get_fernet()
    store = _load_store()
    store[name] = f.encrypt(plaintext_value.encode()).decode()
    _save_store(store)


def get_secret(name: str, default: str | None = None) -> str | None:
    """Dechiffre une cle a la demande. Retourne `default` si absente/illisible
    (permet un degrade propre plutot qu'un crash au demarrage)."""
    f = _get_fernet()
    store = _load_store()
    token = store.get(name)
    if not token:
        return default
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        return default


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "generate-master-key":
        print(Fernet.generate_key().decode())
    elif cmd == "set" and len(sys.argv) == 4:
        set_secret(sys.argv[2], sys.argv[3])
        print(f"Cle '{sys.argv[2]}' chiffree et enregistree dans {SECRETS_FILE}")
    elif cmd == "get" and len(sys.argv) == 3:
        print(get_secret(sys.argv[2]) or "(absente)")
    else:
        print(__doc__)
        sys.exit(1)
