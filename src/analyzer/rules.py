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


def _quote_at(data: MatchData, market: str, selection: str, bookmaker: str, snapshot_at: str):
    """Relevé d'un book pour une issue à un instant précis, ou None."""
    for quote in data.book_series(market, selection, bookmaker):
        if quote.snapshot_at == snapshot_at:
            return quote
    return None


def evaluate_r5(data: MatchData, config: dict) -> RuleResult:
    """R5 — Cohérence croisée : depuis l'ouverture, le spread ET le moneyline du
    consensus bougent dans le même sens (règle de confirmation)."""
    params = config["rules"]["R5_cross_market_coherence"]
    min_spread = params["min_spread_move_points"]
    min_prob = params["min_prob_move_pct"] / 100.0
    score = params["score"]

    h2h_selections = set(data.selections("h2h"))
    for selection in data.selections("spreads"):
        if selection not in h2h_selections:
            continue
        spread = data.consensus_series("spreads", selection)
        moneyline = data.consensus_series("h2h", selection)
        if len(spread) < 2 or len(moneyline) < 2:
            continue
        if spread[0].line is None or spread[-1].line is None:
            continue
        d_line = spread[-1].line - spread[0].line   # < 0 : l'équipe devient plus favorite
        d_prob = moneyline[-1].prob - moneyline[0].prob  # > 0 : proba en hausse
        # Cohérent = spread plus négatif ET proba en hausse (ou l'inverse) → signes opposés.
        coherent = _direction(d_line) != 0 and _direction(d_line) != _direction(d_prob)
        if abs(d_line) >= min_spread and abs(d_prob) >= min_prob and coherent:
            detail = (
                f"cohérence {selection} : spread {d_line:+.1f} pt et "
                f"moneyline {d_prob:+.1%} confirment le même sens"
            )
            return RuleResult("R5", True, score, detail)
    return RuleResult("R5", False, 0, "pas de confirmation croisée")


def evaluate_r6(data: MatchData, config: dict) -> RuleResult:
    """R6 — Divergence bookmaker : un book s'écarte de ≥ seuil du consensus (→ ANOMALIE)."""
    params = config["rules"]["R6_bookmaker_divergence"]
    threshold, score = params["threshold_prob_pct"] / 100.0, params["score"]

    times = data.times()
    if not times:
        return RuleResult("R6", False, 0, "aucune donnée", orientation="anomaly")
    latest = times[-1]

    for market in data.markets():
        for selection in data.selections(market):
            consensus = data.consensus_at(market, selection, latest)
            if consensus is None:
                continue
            for bookmaker in data.bookmakers(market, selection):
                quote = _quote_at(data, market, selection, bookmaker, latest)
                if quote is None:
                    continue
                gap = abs(quote.prob - consensus.prob)
                if gap >= threshold:
                    detail = f"divergence {bookmaker} sur {market}/{selection} : {gap:.1%} vs consensus"
                    return RuleResult("R6", True, score, detail, orientation="anomaly")
    return RuleResult("R6", False, 0, "aucune divergence bookmaker", orientation="anomaly")


def evaluate_r7(data: MatchData, config: dict) -> RuleResult:
    """R7 — Incohérence spread/moneyline chez un même book : le favori selon le spread
    contredit le favori selon le moneyline (V1 : contradiction de favori, → ANOMALIE)."""
    params = config["rules"]["R7_spread_moneyline_inconsistency"]
    min_gap = params["min_prob_gap_pct"] / 100.0
    min_abs_spread = params["min_abs_spread"]
    score = params["score"]

    times = data.times()
    if not times:
        return RuleResult("R7", False, 0, "aucune donnée", orientation="anomaly")
    latest = times[-1]

    common = [s for s in data.selections("h2h") if s in data.selections("spreads")]
    if len(common) < 2:
        return RuleResult("R7", False, 0, "marchés h2h/spreads incomplets", orientation="anomaly")
    team_a, team_b = common[0], common[1]

    for bookmaker in data.bookmakers("h2h", team_a):
        quote_a = _quote_at(data, "h2h", team_a, bookmaker, latest)
        quote_b = _quote_at(data, "h2h", team_b, bookmaker, latest)
        spread_a = _quote_at(data, "spreads", team_a, bookmaker, latest)
        if quote_a is None or quote_b is None or spread_a is None or spread_a.line is None:
            continue

        # Favori selon le spread = équipe à ligne négative.
        if spread_a.line < 0:
            spread_favorite, abs_spread = team_a, -spread_a.line
        else:
            spread_favorite, abs_spread = team_b, spread_a.line
        # Favori selon le moneyline = proba la plus haute.
        moneyline_favorite = team_a if quote_a.prob >= quote_b.prob else team_b
        gap = abs(quote_a.prob - quote_b.prob)

        if moneyline_favorite != spread_favorite and gap >= min_gap and abs_spread >= min_abs_spread:
            detail = (
                f"incohérence {bookmaker} : favori spread={spread_favorite} "
                f"mais favori moneyline={moneyline_favorite} (écart {gap:.1%}, |spread|={abs_spread:.1f})"
            )
            return RuleResult("R7", True, score, detail, orientation="anomaly")
    return RuleResult("R7", False, 0, "spread et moneyline cohérents", orientation="anomaly")


# Toutes les règles (couche B + C), dans l'ordre.
MOVEMENT_RULES = [evaluate_r1, evaluate_r2, evaluate_r3, evaluate_r4]
ALL_RULES = MOVEMENT_RULES + [evaluate_r5, evaluate_r6, evaluate_r7]

# Règles qui déclenchent une alerte temps réel après chaque collecte (section 6.3).
ALERT_RULES = {"R1", "R2", "R4"}
