#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moteur d'alertes (cahier des charges, section A.5 : "Alerte : RSI de TSLA > 70").
====================================================================================

Une regle est un triplet (metrique, operateur, seuil), evaluee contre le
resultat d'analyse produit par analysis.analyze_stock(). Les regles sont
volontairement simples (pas de DSL complexe) pour rester lisibles et faciles
a stocker cote utilisateur (ex: en base, ou en local storage cote frontend).
"""

from __future__ import annotations
import operator
from dataclasses import dataclass
from typing import Any

OPS = {
    ">": operator.gt, "<": operator.lt, ">=": operator.ge,
    "<=": operator.le, "==": operator.eq, "!=": operator.ne,
}

# Chemin (cle pointee) vers la valeur dans le dict retourne par analyze_stock()
METRIC_PATHS = {
    "rsi": "technique.rsi14",
    "risque": "niveau_risque",
    "score_global": "scoring.global",
    "conseil": "conseil",
    "potentiel_pct": "objectif_3_mois.potentiel_rendement_pct",
    "prix": "seance.close",
    "variation_jour_pct": "seance.variation_jour_pct",
    "dividende_rendement_pct": "dividendes.rendement_pct",
}

DEFAULT_RULES = [
    # Les regles "conviction forte" du cahier des charges, prêtes à l'emploi.
    {"metric": "rsi", "op": ">", "value": 70, "label": "RSI en surchauffe (>70)"},
    {"metric": "rsi", "op": "<", "value": 30, "label": "RSI en zone de sous-évaluation (<30)"},
    {"metric": "risque", "op": ">=", "value": 8, "label": "Niveau de risque élevé (>=8/10)"},
    {"metric": "conseil", "op": "==", "value": "Acheter", "label": "Signal d'achat déclenché"},
    {"metric": "conseil", "op": "==", "value": "Vendre", "label": "Signal de vente déclenché"},
]


@dataclass
class AlertRule:
    metric: str
    op: str
    value: Any
    label: str = ""

    def describe(self, ticker: str) -> str:
        return self.label or f"{ticker}: {self.metric} {self.op} {self.value}"


def _get_path(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate_rules(ticker: str, analysis: dict, rules: list[dict] | None = None) -> list[dict]:
    """Retourne la liste des alertes declenchees pour cette analyse.
    `rules` : liste de dicts {"metric", "op", "value", "label"} ; a defaut,
    utilise DEFAULT_RULES (les seuils "de conviction" du cahier des charges)."""
    rules = rules or DEFAULT_RULES
    triggered = []
    for r in rules:
        rule = AlertRule(**r)
        path = METRIC_PATHS.get(rule.metric, rule.metric)
        current_value = _get_path(analysis, path)
        if current_value is None:
            continue
        op_fn = OPS.get(rule.op)
        if op_fn is None:
            continue
        try:
            is_triggered = op_fn(current_value, rule.value)
        except TypeError:
            continue
        if is_triggered:
            triggered.append({
                "ticker": ticker,
                "message": f"Alerte : {rule.describe(ticker)} (valeur actuelle : {current_value})",
                "metric": rule.metric, "value": current_value,
                "operator": rule.op, "threshold": rule.value,
            })
    return triggered
