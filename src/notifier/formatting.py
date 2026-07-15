"""Mise en forme des messages Telegram (section 7.2).

Fonctions **pures** (une ligne de base → une chaîne HTML), donc faciles à tester
sans réseau. Deux types de messages en V1.4 : l'alerte temps réel et le verdict H-1.

Règles respectées ici :
- affichage en heure locale (Europe/Paris) alors que la base stocke en UTC (section 5) ;
- noms d'équipes échappés en HTML (un « & » dans un nom casserait le parse_mode) ;
- le **justificatif du verdict est rendu tel quel** : le drapeau R6 (« ⚠ divergence
  bookmaker signalée ») est déjà rédigé par l'analyseur dans `rationale`. Le
  notificateur ne recompose pas cette logique métier, il la livre.
- la sélection du verdict est **traduite au format des bookmakers français** (style
  Betclic) en ligne principale, la notation US technique restant en ligne secondaire.
  Rappel affiché : la cote reste la **médiane des books US**, ce n'est pas une cote FR.
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# Libellés lisibles des règles d'alerte temps réel (R1/R2/R4, section 6.3).
RULE_LABELS = {
    "R1": "mouvement de ligne spread",
    "R2": "steam move",
    "R4": "synchronisation multi-bookmakers",
}

# Pastille de couleur par type de verdict.
VERDICT_EMOJI = {"SIGNAL": "🟢", "ANOMALIE": "🟠", "NO_BET": "⚪"}


def _local_time(tipoff_utc: str, tz_name: str) -> str:
    """Convertit un tip-off UTC en heure locale lisible (ex. '17/07 02:20')."""
    dt = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%d/%m %H:%M")


def _match_label(row: sqlite3.Row) -> str:
    """Libellé 'Away @ Home' avec noms échappés."""
    return f"{html.escape(row['away_team'])} @ {html.escape(row['home_team'])}"


# ─────────────────── Traduction FR au format bookmaker (style Betclic) ───────────────────
# Fonctions PURES (aucun accès base ni réseau) : la sélection technique (marché,
# équipe, ligne) est traduite en langage de bookmaker français. Testées directement.


def _fr_number(value: float, decimals: int | None = None) -> str:
    """Formate un nombre à la française (virgule décimale).

    - `decimals=None` : format naturel (entiers sans décimale, sinon décimales utiles).
    - `decimals=n` : nombre fixe de décimales (ex. cotes à 2 décimales).
    """
    brut = f"{value:.{decimals}f}" if decimals is not None else f"{value:g}"
    return brut.replace(".", ",")


def _traduire_spread(selection: str, line: float) -> str:
    """Traduit un handicap (spread) en formulation « écart de points » à la française.

    - ligne demi-point (-X,5) côté favori → « gagne de (X+1)+ » ;
      côté + (+X,5) → « ne perd pas ou perd de X max » ;
    - ligne entière (-X,0) : un écart exactement égal à X est remboursé (push).
      Favori → « gagne de (X+1)+ (remboursé si écart = X) ».
      Côté + (déduit par symétrie, non spécifié par le cahier des charges) →
      « ne perd pas ou perd de (X-1) max (remboursé si écart = X) ».
    """
    magnitude = abs(line)
    favori = line < 0
    est_entiere = abs(magnitude - round(magnitude)) < 1e-9

    if not est_entiere:
        # Ligne demi-point : X = partie entière (4,5 → X = 4).
        x = int(magnitude)
        if favori:
            return f"{selection} gagne de {x + 1}+"
        if x == 0:  # +0,5 : le plus petit avantage → l'équipe doit l'emporter
            return f"{selection} gagne le match"
        return f"{selection} ne perd pas ou perd de {x} max"

    # Ligne entière : push (remboursement) si l'écart vaut exactement X.
    x = int(round(magnitude))
    if favori:
        return f"{selection} gagne de {x + 1}+ (remboursé si écart = {x})"
    if x <= 1:
        return f"{selection} ne perd pas (remboursé si écart = {x})"
    return f"{selection} ne perd pas ou perd de {x - 1} max (remboursé si écart = {x})"


def _traduire_totals(selection: str, line: float) -> str:
    """Traduit un total (over/under) → « +/- de X,5 points dans le match »."""
    signe = "+" if selection.strip().lower() == "over" else "-"
    return f"{signe} de {_fr_number(line)} points dans le match"


def traduire_selection(market: str | None, selection: str, line: float | None) -> str:
    """Traduit une sélection au format des bookmakers français (style Betclic).

    Fonction pure et réutilisable. Retombe sur la sélection brute si le marché est
    inconnu ou si une ligne attendue est absente (repli défensif).
    """
    if market == "h2h":
        return f"{selection} — Vainqueur du match (prolongations incluses)"
    if market == "totals" and line is not None:
        return _traduire_totals(selection, line)
    if market == "spreads" and line is not None:
        return _traduire_spread(selection, line)
    return selection


def format_alert(row: sqlite3.Row, tz_name: str) -> str:
    """Message d'alerte temps réel : règle déclenchée + détail du mouvement."""
    heure = _local_time(row["tipoff_utc"], tz_name)
    label = RULE_LABELS.get(row["rule"], row["rule"])
    details = html.escape(row["details"] or "")
    return (
        f"⚠️ <b>Alerte {html.escape(row['rule'])}</b> — {label}\n"
        f"{_match_label(row)} (tip-off {heure})\n"
        f"{details}"
    )


def _format_us_selection(row: sqlite3.Row) -> str:
    """Ligne secondaire : notation US technique conservée + rappel « médiane des books US ».

    On y garde le marché brut et la ligne signée (repères habituels), et on précise
    que la cote est la médiane des bookmakers US — jamais une cote de bookmaker FR.
    """
    us = html.escape(row["selection"])
    if row["market"]:
        marche = html.escape(row["market"])
        if row["line"] is not None:
            marche += f" {_fr_number(row['line'])}"
        us += f" ({marche})"
    if row["odds_at_verdict"] is not None:
        cote = _fr_number(row["odds_at_verdict"], 2)
        us += f" @ {cote} (médiane des books US, pas une cote FR)"
    return us


def format_verdict(row: sqlite3.Row, tz_name: str) -> str:
    """Message de verdict H-1 : verdict, sélection traduite en FR, puis justificatif.

    La sélection apparaît d'abord au **format bookmaker français** (ligne principale),
    puis en **notation US** technique (ligne secondaire), avant le justificatif complet.
    """
    heure = _local_time(row["tipoff_utc"], tz_name)
    emoji = VERDICT_EMOJI.get(row["verdict"], "•")
    lines = [
        f"{emoji} <b>{html.escape(row['verdict'])}</b> — {_match_label(row)}",
        f"Tip-off {heure}",
    ]

    if row["selection"]:
        fr = traduire_selection(row["market"], row["selection"], row["line"])
        lines.append(f"Sélection pressentie : <b>{html.escape(fr)}</b>")
        lines.append(f"  ↳ US : {_format_us_selection(row)}")

    lines.append("")  # ligne vide avant le justificatif
    lines.append(html.escape(row["rationale"] or ""))
    return "\n".join(lines)


def build_position_buttons(verdict_id: int) -> dict[str, Any]:
    """Clavier inline de prise de position joint au verdict.

    Le `callback_data` encode l'identifiant du verdict. Son **traitement** (clic →
    table `positions`) relève du bot d'écoute (étape 1.5) : d'ici là les boutons
    sont envoyés mais inertes.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Je me positionne", "callback_data": f"pos:{verdict_id}"},
                {"text": "➖ Je passe", "callback_data": f"skip:{verdict_id}"},
            ]
        ]
    }
