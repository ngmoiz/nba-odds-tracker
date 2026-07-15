"""Rapport hebdomadaire (section 7.2 §4) — pure agrégation sur `evaluations`.

Aucune nouvelle donnée n'est produite : on lit les évaluations existantes (jointure
avec `verdicts`) et on agrège. Le rapport couvre les **7 derniers jours glissants**
sur `evaluated_at` (aucun trou entre deux rapports consécutifs).

Métriques :
- **Taux de réussite des SIGNAL** par marché et par règle déclenchante (pushes hors
  dénominateur, via le helper `success_rate` partagé avec le bilan quotidien).
- **CLV moyen** des SIGNAL (moyenne des `clv` non-None).
- **Performance des NO_BET pressentis** (faux négatifs : la sélection pressentie
  aurait-elle gagné ?).
- **Cumul d'évaluations** + rappel du garde-fou règle 11 tant que la **cohorte de
  calibration** (v2) est sous 50 évaluations.

Segregation `logic_version` : les verdicts pré-correction H-1 (v1) ne se mélangent
pas avec les verdicts propres (v2). Le rapport produit un bloc par cohorte présente ;
une cohorte vide est omise.

Multi-comptage par règle : un SIGNAL déclenché par R1+R5 est compté dans **chaque**
règle. C'est la sémantique naturelle de « performance par règle déclenchante » — une
règle qui contribue à des signaux perdants doit le voir dans ses stats, même
partagés. Une note explicite le rappelle dans le rapport.

Parsing défensif **non silencieux** : un `rules_triggered` illisible (JSON malformé)
est loggé en warning avec le `verdict_id` concerné, et une mention « ⚠️ N verdict(s)
à règles illisibles » apparaît dans la section par règle quand N > 0.

Fonctions **pures** : pas d'accès DB ici (les lignes brutes viennent de `db.py`),
pas d'effet de bord (le logging est un side-effect de qualité de données uniquement).
L'orchestration (lecture DB + envoi Telegram) est dans `evaluator.run_weekly_report`.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field

from common.logging_config import get_logger
from evaluator.grading import LOST, PUSH, WON
from evaluator.reporting import success_rate

logger = get_logger("evaluator")

# En dessous de ce nombre d'évaluations cumulées, les taux sont du bruit (section 11).
_MIN_EVALS_FOR_TRUST = 50


# ─────────────────────────── Dataclasses ───────────────────────────


@dataclass(frozen=True)
class SignalStats:
    """Agrégat d'un segment (logic_version × marché ou logic_version × règle)."""

    logic_version: int
    label: str               # "spreads" / "h2h" / "totals" OU "R1" / "R5" / ...
    won: int = 0
    lost: int = 0
    push: int = 0
    clv_values: tuple[float, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.won + self.lost + self.push

    @property
    def rate(self) -> float | None:
        """Taux de réussite hors push (réutilise le helper partagé)."""
        outcomes = [WON] * self.won + [LOST] * self.lost + [PUSH] * self.push
        return success_rate(outcomes)

    @property
    def avg_clv(self) -> float | None:
        """Moyenne des CLV non-None (en points de probabilité)."""
        if not self.clv_values:
            return None
        return sum(self.clv_values) / len(self.clv_values)


@dataclass(frozen=True)
class NoBetStats:
    """Agrégat des NO_BET pressentis (faux négatifs) par logic_version."""

    logic_version: int
    won: int = 0          # aurait gagné → faux négatif
    lost: int = 0
    push: int = 0

    @property
    def total(self) -> int:
        return self.won + self.lost + self.push

    @property
    def false_negative_rate(self) -> float | None:
        """Taux de faux négatifs = won / (won + lost), pushes hors dénominateur."""
        outcomes = [WON] * self.won + [LOST] * self.lost + [PUSH] * self.push
        return success_rate(outcomes)


# ─────────────────────────── Agrégation ───────────────────────────


def _parse_rules(rules_json: str | None, verdict_id: int | None = None) -> list[str]:
    """Parse le JSON `rules_triggered` de façon **non silencieuse**.

    Un JSON malformé ou absent renvoie [] **et** logge un warning avec le `verdict_id`
    concerné (si fourni) pour permettre le diagnostic.
    """
    if not rules_json:
        if verdict_id is not None:
            logger.warning(
                "rules_triggered vide pour le verdict %s — ignoré dans les stats par règle.",
                verdict_id,
            )
        return []
    try:
        parsed = json.loads(rules_json)
        if isinstance(parsed, list):
            return [str(r) for r in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    if verdict_id is not None:
        logger.warning(
            "rules_triggered illisible pour le verdict %s : %r — ignoré dans les stats par règle.",
            verdict_id, rules_json,
        )
    return []


def aggregate_signal_by_market(
    rows: list, *, logic_version: int
) -> list[SignalStats]:
    """Agrège les SIGNAL par marché pour une cohorte donnée.

    `rows` : lignes brutes avec colonnes `verdict_id`, `logic_version`, `market`,
    `rules_triggered`, `outcome`, `clv`.
    """
    buckets: dict[str, SignalStats] = {}
    for row in rows:
        if row["logic_version"] != logic_version:
            continue
        market = row["market"] or "n/d"
        clv = row["clv"]
        prev = buckets.get(market, SignalStats(logic_version=logic_version, label=market))
        buckets[market] = _accumulate(prev, row["outcome"], clv)
    return sorted(buckets.values(), key=lambda s: s.label)


def aggregate_signal_by_rule(
    rows: list, *, logic_version: int
) -> tuple[list[SignalStats], int]:
    """Agrège les SIGNAL par règle déclenchante (multi-comptage assumé).

    Un SIGNAL déclenché par R1+R5 est compté dans **chaque** règle.
    Renvoie `(stats, unreadable_count)` : `unreadable_count` est le nombre de verdicts
    dont le `rules_triggered` était illisible (JSON malformé ou vide).
    """
    buckets: dict[str, SignalStats] = {}
    unreadable = 0
    for row in rows:
        if row["logic_version"] != logic_version:
            continue
        verdict_id = row["verdict_id"] if "verdict_id" in row.keys() else None
        raw = row["rules_triggered"]
        rules = _parse_rules(raw, verdict_id)
        if not rules:
            unreadable += 1
            continue
        clv = row["clv"]
        for rule in rules:
            prev = buckets.get(rule, SignalStats(logic_version=logic_version, label=rule))
            buckets[rule] = _accumulate(prev, row["outcome"], clv)
    return sorted(buckets.values(), key=lambda s: s.label), unreadable


def aggregate_nobet(rows: list, *, logic_version: int) -> NoBetStats | None:
    """Agrège les NO_BET pressentis pour une cohorte. Renvoie None si vide."""
    won = lost = push = 0
    for row in rows:
        if row["logic_version"] != logic_version:
            continue
        if row["outcome"] == WON:
            won += 1
        elif row["outcome"] == LOST:
            lost += 1
        elif row["outcome"] == PUSH:
            push += 1
    if won + lost + push == 0:
        return None
    return NoBetStats(logic_version=logic_version, won=won, lost=lost, push=push)


def _accumulate(stats: SignalStats, outcome: str, clv: float | None) -> SignalStats:
    """Renvoie un nouveau SignalStats incrémenté d'une issue."""
    won = stats.won + (1 if outcome == WON else 0)
    lost = stats.lost + (1 if outcome == LOST else 0)
    push = stats.push + (1 if outcome == PUSH else 0)
    clv_values = stats.clv_values + ((clv,) if clv is not None else ())
    return SignalStats(
        logic_version=stats.logic_version,
        label=stats.label,
        won=won, lost=lost, push=push,
        clv_values=clv_values,
    )


# ─────────────────────────── Formatage ───────────────────────────


def _pct(rate: float | None) -> str:
    return "n/d" if rate is None else f"{rate * 100:.1f} %".replace(".", ",")


def _clv_label(avg: float | None) -> str:
    if avg is None:
        return "CLV n/d"
    signe = "+" if avg >= 0 else ""
    return f"CLV moy. {signe}{avg * 100:.1f} pts".replace(".", ",")


def _format_signal_section(
    title: str, stats: list[SignalStats], *, note: str | None = None,
    unreadable: int = 0,
) -> list[str]:
    """Formate une section SIGNAL (par marché ou par règle)."""
    lines = [f"  <b>{title}</b>"]
    if note:
        lines.append(f"  <i>{note}</i>")
    if unreadable > 0:
        lines.append(f"  ⚠️ {unreadable} verdict(s) à règles illisibles")
    if not stats:
        lines.append("  • aucune évaluation")
        return lines
    for s in stats:
        lines.append(
            f"  • {html.escape(s.label)} : {s.won} gagné(s), {s.lost} perdu(s), "
            f"{s.push} push — taux {_pct(s.rate)} (hors push) — {_clv_label(s.avg_clv)}"
        )
    return lines


def _format_nobet_section(stats: NoBetStats) -> list[str]:
    lines = ["  <b>NO_BET pressentis (faux négatifs)</b>"]
    lines.append(
        f"  • {stats.total} évalués — {stats.won} auraient gagné "
        f"({_pct(stats.false_negative_rate)}), {stats.lost} perdu(s), {stats.push} push"
    )
    return lines


def _cohort_label(logic_version: int) -> str:
    if logic_version == 2:
        return "Logique v2 (décision H-1)"
    if logic_version == 1:
        return "Logique v1 (pré-correction H-1)"
    return f"Logique v{logic_version}"


def _guardrail_footer(total_evals: int, v2_evals: int) -> str:
    """Pied de page : cumul global + cumul v2 + garde-fou sur la cohorte de calibration."""
    footer = f"{total_evals} évaluations cumulées, dont {v2_evals} en logique v2."
    if v2_evals < _MIN_EVALS_FOR_TRUST:
        footer += (
            f" ⚠️ En dessous de {_MIN_EVALS_FOR_TRUST} évaluations en logique v2, "
            "les taux restent du bruit statistique — aucun seuil ne doit être modifié "
            "(règle 11)."
        )
    return footer


def format_weekly_report(
    week_label: str,
    signal_rows: list,
    nobet_rows: list,
    total_evals: int,
    v2_evals: int,
) -> str:
    """Compose le rapport hebdomadaire (HTML léger pour Telegram).

    Args:
        week_label: libellé de la période (ex. « 7 derniers jours (08/07 → 15/07/2026) »).
        signal_rows: lignes brutes `get_weekly_signal_evals` (toutes cohortes).
        nobet_rows: lignes brutes `get_weekly_nobet_evals` (toutes cohortes).
        total_evals: cumul global d'évaluations (information).
        v2_evals: cumul d'évaluations en logique v2 (garde-fou règle 11).
    """
    header = f"📊 <b>Rapport hebdomadaire — {html.escape(week_label)}</b>"

    # Détermine les cohortes présentes (v2 d'abord, v1 ensuite, autres à la fin).
    versions = sorted(
        {r["logic_version"] for r in signal_rows} | {r["logic_version"] for r in nobet_rows},
        reverse=True,
    )

    if not versions:
        return f"{header}\nAucune évaluation sur la période.\n\n{_guardrail_footer(total_evals, v2_evals)}"

    body: list[str] = []
    for lv in versions:
        by_market = aggregate_signal_by_market(signal_rows, logic_version=lv)
        by_rule, unreadable = aggregate_signal_by_rule(signal_rows, logic_version=lv)
        nobet = aggregate_nobet(nobet_rows, logic_version=lv)

        body.append(f"<b>{_cohort_label(lv)}</b>")
        body.append("")
        body.extend(_format_signal_section(
            "Taux de réussite SIGNAL par marché", by_market
        ))
        body.append("")
        body.extend(_format_signal_section(
            "Taux de réussite SIGNAL par règle déclenchante",
            by_rule,
            note="⚠️ Un signal peut apparaître dans plusieurs règles (multi-comptage assumé).",
            unreadable=unreadable,
        ))
        if nobet is not None:
            body.append("")
            body.extend(_format_nobet_section(nobet))
        body.append("")

    return header + "\n" + "\n".join(body) + "\n" + _guardrail_footer(total_evals, v2_evals)