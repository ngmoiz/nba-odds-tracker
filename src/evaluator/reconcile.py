"""Appariement d'un match suivi (The Odds API) avec son résultat (balldontlie).

Les deux sources ont des identifiants différents : on apparie par **noms d'équipes
normalisés** + **proximité de date**. La date de match côté balldontlie est une date
calendaire US (fuseau de la ligue), alors que le tip-off est stocké en UTC : on
convertit d'abord le tip-off dans le fuseau du calendrier, puis on tolère un écart
d'un jour pour absorber les cas limites de fuseau.

Fonctions pures (aucune base ni réseau), testables directement.

Contrat de clé d'équipe (chantier B5, lot 3)
--------------------------------------------
**La clé canonique du projet est le nom porté par `matches`, c'est-à-dire celui de The
Odds API.** C'est lui que verra le read path du modèle, qui interroge `team_ratings` par
**égalité stricte** (`db.get_team_rating`). Tout producteur de notes — le backfill de
saison comme, plus tard, le write path de l'évaluateur — doit donc dériver sa clé par
`alias ∘ normalize_team`, via `build_canonical_resolver`. Deux producteurs qui
normaliseraient différemment écriraient deux lignes pour la même équipe, et
`UNIQUE(sport, team)` n'y pourrait rien : une note scindée en deux, donc fausse des deux
côtés, sans aucun signal.

L'alias existe parce que balldontlie renvoie un `city` **vide** pour les franchises
créées en 2026 (`full_name` = 'Fire', 'Tempo' au lieu de 'Portland Fire',
'Toronto Tempo' — vérifié par appel réel le 2026-08-09). `teams_match` absorbe déjà ce
cas côté évaluateur, **volontairement** et de longue date (cf. sa docstring), mais par
une comparaison d'inclusion : cette tolérance ne se propage pas à une égalité stricte,
et c'est précisément ce que le contrat ci-dessus vient combler.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common.results_api_client import GameResult


def normalize_team(name: str) -> str:
    """Normalise un nom d'équipe pour la comparaison (casse et espaces)."""
    return " ".join(name.strip().lower().split())


def teams_match(name1: str, name2: str) -> bool:
    """Vérifie si deux noms d'équipes correspondent (exact ou partiel).
    
    Accepte une correspondance si :
    - Les noms normalisés sont identiques (ex: "Boston Celtics" == "boston celtics")
    - Un nom contient l'autre (ex: "Toronto Tempo" contient "Tempo")
    
    Cette flexibilité gère les incohérences entre The Odds API et balldontlie
    (ex: "Toronto Tempo" vs "Tempo", "Portland Fire" vs "Fire").
    """
    n1, n2 = normalize_team(name1), normalize_team(name2)
    return n1 == n2 or n1 in n2 or n2 in n1


class TeamContractError(Exception):
    """Le contrat de clé d'équipe est rompu : nom non résoluble, ou alias mort."""


@dataclass(frozen=True)
class TeamContractReport:
    """Constat de conformité entre les noms d'une source et les équipes suivies.

    `errors` est bloquant, `warnings` est informatif. La distinction est tout l'intérêt :
    un alias devenu inutile (la source a corrigé son nom) ne doit pas arrêter un
    backfill, un nom d'équipe non résoluble si.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def team_aliases(config: dict, sport: str) -> dict[str, str]:
    """Alias déclarés pour une ligue : clé normalisée de la source → nom canonique.

    Lus dans `backfill.team_aliases[<sport>]` (règle 0.4.7 : rien en dur). Une ligue
    sans alias déclaré rend un dictionnaire vide — l'absence d'alias est le cas normal,
    pas une anomalie.
    """
    declared = ((config.get("backfill") or {}).get("team_aliases") or {}).get(sport) or {}
    return {normalize_team(source): canonical for source, canonical in declared.items()}


def canonical_team(name: str, aliases: dict[str, str]) -> str:
    """Nom canonique (forme d'affichage) correspondant à un nom de source."""
    return aliases.get(normalize_team(name), name)


def canonical_team_key(name: str, aliases: dict[str, str]) -> str:
    """Clé d'écriture d'une équipe : `normalize_team` appliqué au nom canonique.

    C'est **la seule** façon dont une note doit être clée. Toute autre dérivation
    rouvrirait la divergence que le contrat referme.
    """
    return normalize_team(canonical_team(name, aliases))


def check_team_contract(
    *,
    source_names: Iterable[str],
    aliases: dict[str, str],
    known_teams: Iterable[str],
) -> TeamContractReport:
    """Confronte l'inventaire complet d'une source aux équipes réellement suivies.

    Trois familles de défaut, dont deux bloquantes, contrôlées **dans les deux sens** :

    1. **Alias mort** (bloquant) — un alias qui vise une équipe absente de `matches`.
       Une faute de frappe dans `config.yaml` produirait sinon une clé orpheline que
       rien ne rattraperait ;
    2. **Nom non résoluble** (bloquant) — un nom de la source qui, après aliasing, ne
       correspond à aucune équipe suivie. C'est le cas 'Fire'/'Tempo' d'aujourd'hui ;
       à la bascule NBA, d'autres divergences de `city` apparaîtront et doivent être
       attrapées par le même filet, sans qu'on ait à y penser ;
    3. **Équipe jamais produite** (bloquant) — une équipe suivie qu'aucun nom de la
       source ne produit. Soit la source la nomme autrement (divergence à aliaser),
       soit l'échantillon est trop court pour conclure — dans les deux cas le contrat
       n'est pas démontré, et un contrat non démontré ne vaut rien.

    Un alias dont la source n'apparaît jamais est un simple **avertissement** : c'est ce
    qui arrivera le jour où balldontlie renseignera enfin la ville.
    """
    known = {normalize_team(team): team for team in known_teams}
    report = TeamContractReport()

    for source_key, canonical in sorted(aliases.items()):
        if normalize_team(canonical) not in known:
            report.errors.append(
                f"Alias mort : {source_key!r} → {canonical!r}, or aucune équipe suivie "
                f"ne porte ce nom. Corriger backfill.team_aliases."
            )

    produced: dict[str, str] = {}
    for name in source_names:
        key = canonical_team_key(name, aliases)
        if key in known:
            produced[key] = name
        else:
            report.errors.append(
                f"Nom non résoluble : {name!r} → clé {key!r}, absente des équipes "
                f"suivies. Déclarer un alias dans backfill.team_aliases."
            )

    seen_sources = {normalize_team(name) for name in source_names}
    for source_key, canonical in sorted(aliases.items()):
        if source_key not in seen_sources:
            report.warnings.append(
                f"Alias inutilisé : {source_key!r} → {canonical!r} — la source ne "
                f"produit plus ce nom (corrigé en amont ?)."
            )

    for key, team in sorted(known.items()):
        if key not in produced:
            report.errors.append(
                f"Équipe jamais produite par la source : {team!r}. Soit la source la "
                f"nomme autrement (alias à déclarer), soit l'échantillon est trop "
                f"court pour le démontrer — élargir la plage."
            )

    return report


def build_canonical_resolver(
    *,
    aliases: dict[str, str],
    known_teams: Iterable[str],
) -> Callable[[str], str]:
    """Fonction de clé à injecter dans le rejeu des notes (`analyzer.ratings.replay`).

    Valide les alias **à la construction** — un alias mort lève ici, il n'est jamais
    ignoré — puis lève sur tout nom non résoluble au moment de l'appel. Aucune équipe
    n'est devinée : une note écrite sous une clé approximative serait invisible au read
    path, donc pire qu'absente.

    Volontairement plus stricte que `teams_match` : cette dernière apparie deux noms
    pour retrouver *un match*, ce qui autorise l'inclusion. Ici on fabrique la *clé
    primaire* d'une note, et une clé primaire ne se déduit pas d'une comparaison floue.
    """
    known = {normalize_team(team) for team in known_teams}
    for source_key, canonical in sorted(aliases.items()):
        if normalize_team(canonical) not in known:
            raise TeamContractError(
                f"Alias mort : {source_key!r} → {canonical!r}, or aucune équipe suivie "
                f"ne porte ce nom. Corriger backfill.team_aliases avant d'écrire."
            )

    def resolve(name: str) -> str:
        key = canonical_team_key(name, aliases)
        if key not in known:
            raise TeamContractError(
                f"Nom d'équipe non résoluble : {name!r} → clé {key!r}, absente des "
                f"équipes suivies. Déclarer un alias plutôt que d'écrire une note "
                f"sous une clé que le read path ne retrouvera jamais."
            )
        return key

    return resolve


def tipoff_calendar_date(tipoff_utc: str, calendar_tz: str) -> date:
    """Date calendaire du match dans le fuseau de la ligue (US), depuis le tip-off UTC."""
    dt = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(calendar_tz)).date()


def find_result(
    games: list[GameResult],
    *,
    home_team: str,
    away_team: str,
    tipoff_utc: str,
    calendar_tz: str,
    max_day_gap: int = 1,
) -> GameResult | None:
    """Trouve le résultat correspondant au match, ou None.

    Critères : noms d'équipes correspondants (exact ou partiel) et date balldontlie à
    ±`max_day_gap` jour de la date calendaire du tip-off. En cas de plusieurs candidats,
    on prend le plus proche en date.
    
    Le matching flexible gère les incohérences entre APIs (ex: "Toronto Tempo" vs "Tempo").
    """
    target = tipoff_calendar_date(tipoff_utc, calendar_tz)

    best: GameResult | None = None
    best_gap = timedelta(days=max_day_gap + 1)
    for game in games:
        # Matching flexible : accepte correspondance exacte ou partielle
        if not (teams_match(game.home_team, home_team) and teams_match(game.away_team, away_team)):
            continue
        gap = abs(date.fromisoformat(game.game_date) - target)
        if gap <= timedelta(days=max_day_gap) and gap < best_gap:
            best, best_gap = game, gap
    return best
