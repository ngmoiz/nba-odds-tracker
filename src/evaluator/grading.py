"""Évaluation d'un verdict contre le résultat réel (section 6, DoD étape 1.6).

Cœur critique : détermine si la sélection du verdict aurait **gagné**, **perdu** ou
fait **push** (remboursement). L'issue est un **état explicite** (`won`/`lost`/`push`),
jamais portée par un NULL : on ne confond pas « remboursé » (métier) avec « impossible
à noter » (`None`, sélection incohérente avec le match). Le push est ensuite exclu du
dénominateur du taux de réussite. Vaut aussi pour les `NO_BET` (la sélection
*pressentie* aurait-elle couvert ? → faux négatifs).

Fonctions pures. Convention des lignes de spread : la ligne est celle **de la
sélection** (négative pour un favori). « Couvrir » = marge réelle + ligne > 0.
"""
from __future__ import annotations

# Issues possibles d'une évaluation (états métier explicites).
WON = "won"
LOST = "lost"
PUSH = "push"

# Tolérance numérique pour détecter un push (écart exactement nul sur ligne entière).
_EPS = 1e-9


def _sign_to_outcome(value: float) -> str:
    """+ → 'won', − → 'lost', 0 → 'push'."""
    if value > _EPS:
        return WON
    if value < -_EPS:
        return LOST
    return PUSH


def grade_h2h(selection: str, home_team: str, away_team: str,
              home_score: int, away_score: int) -> str | None:
    """Moneyline : la sélection a-t-elle gagné le match ? (pas de nul en basket)."""
    if selection == home_team:
        return _sign_to_outcome(home_score - away_score)
    if selection == away_team:
        return _sign_to_outcome(away_score - home_score)
    return None  # sélection incohérente avec le match → non notable


def grade_spread(selection: str, line: float, home_team: str, away_team: str,
                 home_score: int, away_score: int) -> str | None:
    """Handicap : marge de la sélection + sa ligne. > 0 couvert, < 0 perdu, = 0 push."""
    if selection == home_team:
        margin = home_score - away_score
    elif selection == away_team:
        margin = away_score - home_score
    else:
        return None
    return _sign_to_outcome(margin + line)


def grade_total(selection: str, line: float, home_score: int, away_score: int) -> str | None:
    """Over/Under : total du match vs ligne. Push si égal."""
    total = home_score + away_score
    if selection.strip().lower() == "over":
        return _sign_to_outcome(total - line)
    if selection.strip().lower() == "under":
        return _sign_to_outcome(line - total)
    return None


def grade_verdict(
    *,
    market: str,
    selection: str,
    line: float | None,
    home_team: str,
    away_team: str,
    home_score: int,
    away_score: int,
) -> str | None:
    """Aiguille vers la règle du marché. Renvoie 'won'/'lost'/'push', ou None si non notable."""
    if market == "h2h":
        return grade_h2h(selection, home_team, away_team, home_score, away_score)
    if market == "spreads" and line is not None:
        return grade_spread(selection, line, home_team, away_team, home_score, away_score)
    if market == "totals" and line is not None:
        return grade_total(selection, line, home_score, away_score)
    return None
