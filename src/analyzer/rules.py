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

from analyzer.preprocessing import ConsensusPoint, MatchData

# Tolérance pour distinguer un vrai mouvement du bruit numérique (probas, lignes).
_EPS = 1e-9

# Seuils métier de « mouvement négligeable » (quasi stable) : en dessous, un mouvement
# est réel (au-dessus du bruit flottant _EPS) mais pas actionnable — c'est du bruit
# d'équilibrage de book, pas de l'argent informé. La conclusion « l'argent va vers »
# n'a de sens que pour un mouvement significatif. Configurables dans config.yaml.
_NEGLIGIBLE_PROBA = 0.002   # 0,2 pt de proba (h2h/totals)
_NEGLIGIBLE_LINE = 0.25     # 0,25 pt de ligne (spreads)


@dataclass(frozen=True)
class RuleResult:
    """Résultat d'une règle : déclenchée ou non, points apportés, détail lisible."""

    rule: str            # identifiant : "R1", "R2"…
    triggered: bool
    points: int          # points ajoutés au score (0 si non déclenchée)
    detail: str          # justificatif lisible (pour l'alerte / le verdict)
    orientation: str = "signal"  # "signal" ou "anomaly" (R6/R7)
    state_key: str = ""  # clé de déduplication (sélection + direction) pour les alertes


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


# ─────────────────── Helpers de formatage des mouvements ───────────────────
# Les données existent dans les séries de consensus (ConsensusPoint) : ces helpers
# ne font que du **formatage** — aucun nouveau calcul de règle. Ils enrichissent
# le `detail` de chaque règle, qui alimente à la fois les alertes (format_alert)
# et le justificatif du verdict (_rationale).

def _fr_num(value: float, decimals: int = 1) -> str:
    """Formate un nombre à la française (virgule décimale)."""
    return f"{value:.{decimals}f}".replace(".", ",")


def _fr_signed(value: float, decimals: int = 1) -> str:
    """Formate un nombre signé à la française (ex. '+2,0', '-5,0')."""
    return f"{value:+.{decimals}f}".replace(".", ",")


def _opponent(data: MatchData, market: str, selection: str) -> str | None:
    """L'autre issue d'un marché à deux issues (équipe adverse ou Over/Under)."""
    others = [s for s in data.selections(market) if s != selection]
    return others[0] if others else None


def _state_key(market: str, selection: str, before: ConsensusPoint,
                after: ConsensusPoint, amplitude: str = "") -> str:
    """Clé de déduplication canonique (format machine, point décimal).

    Format : ``market/selection|signe|ampleur`` où ampleur est spécifique à la règle :
    - R1 : ligne arrondie à 1 décimale (ex. ``-5.0``) ;
    - R2 : palier de proba en % entier (ex. ``58``) ;
    - R4 : nombre de books synchronisés (ex. ``9``).

    Deux alertes de la même règle sur le même match avec le même `state_key` sont
    considérées comme le même état → la seconde est supprimée. Un changement
    d'ampleur (approfondissement de ligne, renforcement de synchro) change la clé
    → nouvelle alerte émise.
    """
    if market == "spreads" and before.line is not None and after.line is not None:
        sign = _direction(after.line - before.line)
    else:
        sign = _direction(after.prob - before.prob)
    return f"{market}/{selection}|{sign}|{amplitude}"


def parse_state_key(state_key: str) -> dict:
    """Parse un `state_key` canonique → dict {market, selection, sign, amplitude}.

    Round-trip avec `_state_key` : ``parse_state_key(_state_key(...))`` retrouve les
    composantes. La sélection peut contenir des espaces (ex. ``Portland Fire``).
    """
    # Format : market/selection|sign|amplitude — mais selection peut contenir des /
    # et des espaces. On split sur | (qui n'apparaît pas dans market/selection).
    parts = state_key.split("|")
    if len(parts) < 3:
        return {"market": "", "selection": "", "sign": 0, "amplitude": ""}
    market_selection, sign_str, amplitude = parts[0], parts[1], parts[2]
    # market_selection = "market/selection" — split sur le premier /
    slash_idx = market_selection.find("/")
    if slash_idx == -1:
        return {"market": "", "selection": "", "sign": 0, "amplitude": ""}
    return {
        "market": market_selection[:slash_idx],
        "selection": market_selection[slash_idx + 1:],
        "sign": int(sign_str) if sign_str.lstrip("-").isdigit() else 0,
        "amplitude": amplitude,
    }


def _format_movement(
    data: MatchData,
    market: str,
    selection: str,
    before: ConsensusPoint,
    after: ConsensusPoint,
    temporal_ref: str,
    negligible_prob: float = _NEGLIGIBLE_PROBA,
    negligible_line: float = _NEGLIGIBLE_LINE,
) -> str:
    """Construit le détail lisible d'un mouvement de consensus (3 tiers).

    Affiche : direction explicite (📉 baisse / 📈 hausse sur la sélection observée),
    ligne (spreads) avant → après, cote médiane avant → après, variation de probabilité
    dé-margée en **points de proba** (pas % de cote), référence temporelle, et la
    conclusion « l'argent va vers [cible] ».

    Trois tiers de lisibilité (la conclusion directionnelle n'a de sens que pour un
    mouvement significatif) :
    - **stable** : |δ| < _EPS (bruit flottant, before == after) → « mouvement consensus négligeable » ;
    - **quasi stable** : mouvement réel mais sous le seuil métier (proba < 0,2 pt ou
      ligne < 0,25 pt) → « quasi stable » (ampleur affichée, pas de conclusion) ;
    - **directionnel** : au-dessus du seuil métier → « 📉 baisse / 📈 hausse, l'argent va vers… ».
    """
    parts: list[str] = []

    if market == "spreads" and before.line is not None and after.line is not None:
        delta_line = after.line - before.line
        abs_delta = abs(delta_line)
        if abs_delta < _EPS:
            emoji, direction, target = "→", "stable", None
        elif abs_delta < negligible_line:
            emoji, direction, target = "≈", "quasi stable", None
        elif delta_line < -_EPS:
            emoji, direction, target = "📉", "baisse", selection
        else:
            emoji, direction = "📈", "hausse"
            target = _opponent(data, market, selection)
        parts.append(
            f"{emoji} {direction} : ligne {_fr_signed(before.line)} → {_fr_signed(after.line)}"
        )
    else:
        delta_prob = after.prob - before.prob
        abs_delta = abs(delta_prob)
        if abs_delta < _EPS:
            emoji, direction, target = "→", "stable", None
        elif abs_delta < negligible_prob:
            emoji, direction, target = "≈", "quasi stable", None
        elif delta_prob > _EPS:
            emoji, direction, target = "📈", "hausse", selection
        else:
            emoji, direction = "📉", "baisse"
            target = _opponent(data, market, selection)
        parts.append(f"{emoji} {direction}")

    # Cote médiane avant → après (médiane des books US).
    parts.append(f"cote méd. {_fr_num(before.odds, 2)} → {_fr_num(after.odds, 2)}")

    # Variation de probabilité dé-margée en points de proba (pas % de cote).
    d_prob_pts = (after.prob - before.prob) * 100
    parts.append(f"Δproba {_fr_signed(d_prob_pts, 1)} pts")

    # Référence temporelle.
    parts.append(f"({temporal_ref})")

    # Conclusion directionnelle (sauf stable / quasi stable).
    if target is not None:
        if market == "totals":
            parts.append(f"l'argent va vers l'{target}")
        else:
            parts.append(f"l'argent va vers {target}")
    elif "quasi stable" in (p for p in parts):
        parts.append("mouvement consensus quasi stable")
    else:
        parts.append("mouvement consensus négligeable")

    return ", ".join(parts)


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
    state_key = ""
    if best_selection is None:
        detail = "aucune donnée spread exploitable"
    else:
        series = data.consensus_series("spreads", best_selection)
        negligible_line = config["rules"].get("movement_negligible_line", _NEGLIGIBLE_LINE)
        movement = _format_movement(
            data, "spreads", best_selection, series[0], series[-1], "depuis l'ouverture",
            negligible_line=negligible_line,
        )
        detail = f"spread {best_selection} : {movement}"
        amplitude = f"{series[-1].line:.1f}"
        state_key = _state_key("spreads", best_selection, series[0], series[-1], amplitude)
    return RuleResult("R1", triggered, score if triggered else 0, detail, state_key=state_key)


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
                        negligible_prob = config["rules"].get("movement_negligible_prob", _NEGLIGIBLE_PROBA)
                        movement = _format_movement(
                            data, market, selection, series[i], series[j],
                            f"fenêtre ≤ {params['window_hours']} h",
                            negligible_prob=negligible_prob,
                        )
                        detail = f"steam {market}/{selection} : {movement}"
                        amplitude = f"{round(series[j].prob * 100)}"
                        state_key = _state_key(market, selection, series[i], series[j], amplitude)
                        return RuleResult("R2", True, score, detail, state_key=state_key)
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
                negligible_prob = config["rules"].get("movement_negligible_prob", _NEGLIGIBLE_PROBA)
                negligible_line = config["rules"].get("movement_negligible_line", _NEGLIGIBLE_LINE)
                movement = _format_movement(
                    data, market, selection, series[0], series[-1], "depuis l'ouverture",
                    negligible_prob=negligible_prob, negligible_line=negligible_line,
                )
                detail = f"tendance {market}/{selection} : {movement}, {run} relevés consécutifs même sens"
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
                consensus = data.consensus_series(market, selection)
                if len(consensus) >= 2:
                    negligible_prob = config["rules"].get("movement_negligible_prob", _NEGLIGIBLE_PROBA)
                    negligible_line = config["rules"].get("movement_negligible_line", _NEGLIGIBLE_LINE)
                    movement = _format_movement(
                        data, market, selection, consensus[0], consensus[-1], "fenêtre récente",
                        negligible_prob=negligible_prob, negligible_line=negligible_line,
                    )
                else:
                    movement = "consensus indisponible"
                detail = f"synchro {market}/{selection} : {synced} bookmakers dans le même sens ; {movement}"
                if len(consensus) >= 2:
                    state_key = _state_key(market, selection, consensus[0], consensus[-1], str(synced))
                else:
                    state_key = ""
                return RuleResult("R4", True, score, detail, state_key=state_key)
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
