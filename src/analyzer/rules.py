"""Moteur de règles de détection (section 6.2).

Chaque règle est une **fonction pure** : `(MatchData prétraité, config) -> RuleResult`.
Aucune écriture en base, aucun effet de bord : ainsi chaque règle se teste sur des
relevés simulés, de façon déterministe. Les seuils viennent tous de `config.yaml`.

Cette couche implémente R1–R4 (règles « mouvement »). R5–R7 (cohérence croisée,
divergence, incohérence) arrivent en couche C.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from analyzer.preprocessing import MatchData

# Tolérance pour distinguer un vrai mouvement du bruit numérique (probas, lignes).
_EPS = 1e-9


@dataclass(frozen=True)
class RuleResult:
    """Résultat d'une règle : déclenchée ou non, points apportés, détail lisible."""

    rule: str            # identifiant : "R1", "R2"…
    triggered: bool
    points: int          # points ajoutés au score (0 si non déclenchée)
    detail: str          # justificatif lisible (pour l'alerte / le verdict)
    orientation: str = "signal"  # "signal" ou "anomaly" (R6/R7)


def _parse_time(value: str) -> datetime:
    """Convertit un instant ISO ('...Z' ou '+00:00') en datetime UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _direction(delta: float) -> int:
    """Signe d'un mouvement : +1 (hausse), -1 (baisse), 0 (stable)."""
    if delta > _EPS:
        return 1
    if delta < -_EPS:
        return -1
    return 0


def _longest_monotonic_run(values: list[float]) -> int:
    """Longueur (en nombre de relevés) de la plus longue série strictement monotone."""
    if len(values) < 2:
        return len(values)
    best = current = 1
    previous_dir = 0
    for k in range(1, len(values)):
        direction = _direction(values[k] - values[k - 1])
        if direction == 0:
            current, previous_dir = 1, 0
        elif direction == previous_dir:
            current += 1
        else:
            current, previous_dir = 2, direction
        best = max(best, current)
    return best


# ─────────────────────────────── Règles ───────────────────────────────

def evaluate_r1(data: MatchData, config: dict) -> RuleResult:
    """R1 — Mouvement de ligne spread depuis l'ouverture (≥ seuil points)."""
    params = config["rules"]["R1_spread_line_move"]
    threshold, score = params["threshold_points"], params["score"]

    best_move, best_selection = 0.0, None
    for selection in data.selections("spreads"):
        series = data.consensus_series("spreads", selection)
        if len(series) < 2 or series[0].line is None or series[-1].line is None:
            continue
        move = abs(series[-1].line - series[0].line)
        if move > best_move:
            best_move, best_selection = move, selection

    triggered = best_move >= threshold
    if best_selection is None:
        detail = "aucune donnée spread exploitable"
    else:
        detail = f"spread {best_selection} : {best_move:.1f} pt de mouvement depuis l'ouverture"
    return RuleResult("R1", triggered, score if triggered else 0, detail)


def evaluate_r2(data: MatchData, config: dict) -> RuleResult:
    """R2 — Steam move : variation de proba dé-margée ≥ seuil sur une fenêtre ≤ N h."""
    params = config["rules"]["R2_steam_move"]
    threshold = params["threshold_prob_pct"] / 100.0
    window = timedelta(hours=params["window_hours"])
    score = params["score"]

    for market in data.markets():
        for selection in data.selections(market):
            series = data.consensus_series(market, selection)
            for i in range(len(series)):
                for j in range(i + 1, len(series)):
                    elapsed = _parse_time(series[j].snapshot_at) - _parse_time(series[i].snapshot_at)
                    move = abs(series[j].prob - series[i].prob)
                    if elapsed <= window and move >= threshold:
                        detail = (
                            f"steam {market}/{selection} : Δproba {move:.1%} "
                            f"en {elapsed} (≤ {params['window_hours']} h)"
                        )
                        return RuleResult("R2", True, score, detail)
    return RuleResult("R2", False, 0, "aucun steam move détecté")


def evaluate_r3(data: MatchData, config: dict) -> RuleResult:
    """R3 — Tendance soutenue : mouvement même sens sur ≥ N relevés consécutifs."""
    params = config["rules"]["R3_sustained_trend"]
    need, score = params["min_consecutive_snapshots"], params["score"]

    for market in data.markets():
        # En spreads le signal est dans la ligne ; ailleurs, dans la probabilité.
        metric = "line" if market == "spreads" else "prob"
        for selection in data.selections(market):
            series = data.consensus_series(market, selection)
            values = [getattr(point, metric) for point in series]
            if any(value is None for value in values):
                continue
            run = _longest_monotonic_run(values)
            if run >= need:
                detail = f"tendance {market}/{selection} : {run} relevés consécutifs même sens"
                return RuleResult("R3", True, score, detail)
    return RuleResult("R3", False, 0, "aucune tendance soutenue")


def evaluate_r4(data: MatchData, config: dict) -> RuleResult:
    """R4 — Synchronisation : ≥ N bookmakers bougent dans le même sens (ouverture→dernier)."""
    params = config["rules"]["R4_multi_bookmaker_sync"]
    need, score = params["min_bookmakers"], params["score"]

    for market in data.markets():
        metric = "line" if market == "spreads" else "prob"
        for selection in data.selections(market):
            up = down = 0
            for bookmaker in data.bookmakers(market, selection):
                series = data.book_series(market, selection, bookmaker)
                values = [getattr(quote, metric) for quote in series]
                if len(values) < 2 or any(value is None for value in values):
                    continue
                direction = _direction(values[-1] - values[0])
                if direction > 0:
                    up += 1
                elif direction < 0:
                    down += 1
            synced = max(up, down)
            if synced >= need:
                detail = f"synchro {market}/{selection} : {synced} bookmakers dans le même sens"
                return RuleResult("R4", True, score, detail)
    return RuleResult("R4", False, 0, "pas de synchronisation multi-bookmakers")


# Règles « mouvement » de la couche B, dans l'ordre. R5–R7 s'ajouteront en couche C.
MOVEMENT_RULES = [evaluate_r1, evaluate_r2, evaluate_r3, evaluate_r4]
