#!/usr/bin/env python3
"""Simulation Lot 2 — preuve de bout en bout de l'ordonnancement auto (collecteur).

Outil de NON-RÉGRESSION (pas un jetable) : à rejouer après toute modification de
l'ordonnancement (cibles, tick, vagues, gardes). Sort avec un code non nul si un
invariant casse.

Cadre :
- 6 matchs WNBA déjà EN BASE au tick 0 (statut DECOUVERT).
- Départ AVANT (premier tip-off − 7 h) : tous les matchs à venir au tick 0.
- Assez de ticks pour couvrir le dernier tip-off + marge (ici : + un 2e matin J+1,
  qui éprouve le chemin matin de la garde post-tip-off sur des matchs CLOS).
- Verdicts insérés AU FIL DU DÉROULÉ, à l'entrée en fenêtre de décision de chaque
  match (now >= tip-off − 2 h), via insert_verdict + passage en DECIDE, pour que la
  cible closing (marché du verdict) devienne due.

Fidélité production :
- Tick */20 → phase :00/:20/:40 UTC (ancrage UTC fixe, déterministe, rejouable).
- Cibles réelles de config.yaml (matin, h6, h3, verdict, redecision, closing 0.4).
- Coût API = nombre de marchés demandés (modèle réel The Odds API : marchés × régions).
- Stress garde post-tip-off (correctif f9b0a0c) : dès que le tip-off DB est passé, le
  client renvoie le match avec un commence_time FUTUR (re-listing / reprogrammation /
  live) — la garde autoritaire base doit l'exclure de TOUS les chemins de stockage.

Invariants vérifiés (échec = exit 1) :
  (a) 6 clôtures, une par match, à < 25 min de son tip-off, sur son marché de verdict ;
  (b) zéro snapshot postérieur à un tip-off (garde f9b0a0c) ;
  (c) zéro doublon dans collection_log ;
  (e) crédits loggés == crédits réels des chemins tracés (pas de sur-comptage).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from collector.collector import run_collection, validate_collector_config  # noqa: E402
from common import db  # noqa: E402
from common.odds_api_client import Bookmaker, Market, OddsEvent, Outcome  # noqa: E402

UTC = timezone.utc

# ── Ancrage temporel fixe (phase de tick production :00/:20/:40) ──────────────
START = datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC)  # tick 0

# 6 matchs → 3 vagues de 2 (seuil 45 min) : gaps 30/60/30/60/30 min.
# Tip-offs le lendemain tôt UTC (= soirée US, créneau WNBA réel).
# Verdicts alternés h2h / spreads pour vérifier le marché de la clôture.
MATCHES = [
    # match_id, home, away, tipoff, marché du verdict, ligne
    ("m1", "Sky", "Storm", datetime(2026, 7, 21, 0, 0, tzinfo=UTC), "h2h", None),
    ("m2", "Aces", "Sparks", datetime(2026, 7, 21, 0, 30, tzinfo=UTC), "spreads", -2.5),
    ("m3", "Liberty", "Sun", datetime(2026, 7, 21, 1, 30, tzinfo=UTC), "h2h", None),
    ("m4", "Mystics", "Fever", datetime(2026, 7, 21, 2, 0, tzinfo=UTC), "spreads", -2.5),
    ("m5", "Wings", "Dream", datetime(2026, 7, 21, 3, 0, tzinfo=UTC), "h2h", None),
    ("m6", "Mercury", "Lynx", datetime(2026, 7, 21, 3, 30, tzinfo=UTC), "spreads", -2.5),
]

N_TICKS = 90          # 30 h → couvre le dernier tip-off (03:30) + le 2e matin (J+1 09:00)
TICK = timedelta(minutes=20)

# Config = cibles réelles de config.yaml (closing à 0.4 après correctif).
CONFIG = {
    "api": {"markets": ["h2h", "spreads", "totals"]},
    "quota": {"reserve": 50},
    "collector": {
        "tick_interval_minutes": 20,
        "wave_grouping_minutes": 45,
        "targets": [
            {"name": "morning", "hours_before": None, "markets": ["h2h", "spreads", "totals"], "priority": 2},
            {"name": "h6", "hours_before": 6.0, "markets": ["h2h", "spreads", "totals"], "priority": 2},
            {"name": "h3", "hours_before": 3.0, "markets": ["h2h", "spreads", "totals"], "priority": 3},
            {"name": "verdict", "hours_before": 2.0, "markets": ["h2h", "spreads", "totals"], "priority": 1},
            {"name": "redecision", "hours_before": 1.0, "markets": ["h2h", "spreads", "totals"], "priority": 1},
            {"name": "closing", "hours_before": 0.4, "markets": "dynamic", "priority": 1, "per_match": True},
        ],
    },
}


class SimClient:
    """Client simulé fidèle au modèle de coût The Odds API.

    - get_odds() renvoie les 6 matchs, coût = nombre de marchés demandés (1 région).
    - Stress f9b0a0c : un match dont le tip-off d'origine est passé est renvoyé avec
      un commence_time FUTUR (now + 3 h), simulant un re-listing API / live.
    """

    def __init__(self, now: datetime):
        self.now = now
        self.credits_remaining = "500"   # jamais sous la réserve : garde neutre
        self.last_request_cost = "3"
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def get_odds(self, markets: list[str] | None = None) -> list[OddsEvent]:
        cost = len(markets) if markets else 3
        self.last_request_cost = str(cost)
        self.calls.append((tuple(markets) if markets else ("h2h", "spreads", "totals"), cost))

        events = []
        for mid, home, away, tipoff, _mkt, _line in MATCHES:
            eff_commence = tipoff if self.now < tipoff else self.now + timedelta(hours=3)
            all_markets = [
                Market("h2h", [Outcome(home, 1.74, None), Outcome(away, 2.14, None)]),
                Market("spreads", [Outcome(home, 1.93, -2.5), Outcome(away, 1.89, 2.5)]),
                Market("totals", [Outcome("Over", 1.91, 165.5), Outcome("Under", 1.91, 165.5)]),
            ]
            if markets is not None:
                all_markets = [m for m in all_markets if m.key in markets]
            events.append(OddsEvent(
                id=mid, sport_key="basketball_wnba",
                commence_time=eff_commence.isoformat(),
                home_team=home, away_team=away,
                bookmakers=[Bookmaker(key="draftkings", title="DraftKings",
                                      last_update=self.now.isoformat(), markets=all_markets)],
            ))
        return events


def main() -> int:
    # Vérifie d'abord que la config d'ordonnancement est valide (garde de démarrage).
    validate_collector_config(CONFIG)

    db_path = Path("/tmp/nba_sim_lot2_final.db")
    if db_path.exists():
        db_path.unlink()
    db.init_db(db_path)
    conn = db.get_connection(db_path)

    # ── Pré-insertion : les 6 matchs EN BASE au tick 0 (DECOUVERT) ────────────
    for mid, home, away, tipoff, _mkt, _line in MATCHES:
        db.insert_match(conn, match_id=mid, sport="basketball_wnba",
                        home_team=home, away_team=away,
                        tipoff_utc=tipoff.isoformat(), status="DECOUVERT",
                        created_at=START.isoformat())
    conn.commit()

    first_tipoff = min(m[3] for m in MATCHES)
    last_tipoff = max(m[3] for m in MATCHES)
    print("=" * 88)
    print("SIMULATION LOT 2 — 6 matchs WNBA, 3 vagues, 90 ticks (30 h)")
    print("=" * 88)
    print(f"Départ (tick 0)   : {START:%Y-%m-%d %H:%M UTC}")
    print(f"1er tip-off       : {first_tipoff:%Y-%m-%d %H:%M UTC}  (départ = tip-off − "
          f"{(first_tipoff - START).total_seconds()/3600:.0f} h, donc avant H-7 ✅)")
    print(f"Dernier tip-off   : {last_tipoff:%Y-%m-%d %H:%M UTC}")
    print(f"Fin (tick {N_TICKS-1})     : {START + (N_TICKS-1)*TICK:%Y-%m-%d %H:%M UTC}")
    print("\nMatchs / vagues (gaps 30/60/30/60/30 → V1{m1,m2} V2{m3,m4} V3{m5,m6}) :")
    for mid, home, away, tipoff, mkt, line in MATCHES:
        ln = f" {line}" if line is not None else ""
        print(f"  {mid} {away} @ {home:8s} tip {tipoff:%H:%M}  verdict={mkt}{ln}")
    print()

    verdicts_done: set[str] = set()
    total_credits = 0        # crédits réels (tous appels get_odds)
    morning_credits = 0      # crédits réels des appels du matin (non tracés)
    active_ticks = 0

    for t in range(N_TICKS):
        now = START + t * TICK
        client = SimClient(now)
        summary = run_collection(conn, client, "basketball_wnba", CONFIG, force=False, now=now)

        tick_credits = sum(c for _, c in client.calls)
        total_credits += tick_credits
        # Le matin fire seul sur ses ticks (0 et J+1) : attribuer ses crédits à part.
        if summary.get("morning_collected"):
            morning_credits += tick_credits

        if not summary.get("skipped"):
            active_ticks += 1
            flags = []
            if summary.get("morning_collected"):
                flags.append("MATIN")
            if summary.get("closed"):
                flags.append(f"clos+{summary['closed']}")
            print(f"tick {t:2d} {now:%m-%d %H:%M} | vagues {summary.get('waves',0)} | "
                  f"cibles {summary.get('targets_collected',0)} | "
                  f"snaps {summary.get('snapshots',0)} | "
                  f"appels {len(client.calls)} crédits +{tick_credits} {' '.join(flags)}")

        # ── Verdicts au fil de l'eau : à l'entrée en fenêtre de décision (H-2) ──
        for mid, home, away, tipoff, mkt, line in MATCHES:
            if mid in verdicts_done:
                continue
            if now >= tipoff - timedelta(hours=2) and now < tipoff:
                m = db.get_match(conn, mid)
                if m is None or m["status"] not in ("DECOUVERT", "SUIVI", "DECIDE"):
                    continue
                db.update_match_status(conn, mid, "DECIDE")
                sel = home if mkt in ("h2h", "spreads") else "Over"
                odds = 1.74 if mkt == "h2h" else 1.93
                db.insert_verdict(conn, match_id=mid, verdict="SIGNAL", selection=sel,
                                  market=mkt, line=line, odds_at_verdict=odds,
                                  signal_score=8, rules_triggered="[]", rationale="sim",
                                  decided_at=now.isoformat(),
                                  logic_version=db.DECISION_LOGIC_VERSION)
                conn.commit()
                verdicts_done.add(mid)
                print(f"        · verdict {mid} inséré (DECIDE, marché {mkt}) @ {now:%H:%M} "
                      f"[H-{(tipoff-now).total_seconds()/3600:.2f}]")

    # ═══════════════════════════ ANALYSE ═══════════════════════════
    print("\n" + "=" * 88)
    print("RÉSULTATS")
    print("=" * 88)

    # (d) Cibles servies par match
    print("\n[d] Cibles servies par match (collection_log) :")
    rows = conn.execute(
        "SELECT match_id, target_name, target_hours, markets, credits_used, collected_at "
        "FROM collection_log ORDER BY match_id, collected_at").fetchall()
    by_match: dict[str, list] = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(r)
    for mid, _h, _a, tipoff, mkt, _l in MATCHES:
        served = by_match.get(mid, [])
        names = [r["target_name"] for r in served]
        print(f"  {mid} (tip {tipoff:%H:%M}, verdict {mkt}) : {len(served)} cibles → {names}")
        for r in served:
            ct = datetime.fromisoformat(r["collected_at"])
            print(f"       {r['target_name']:11s} @ {ct:%m-%d %H:%M} "
                  f"[H-{(tipoff-ct).total_seconds()/3600:5.2f}] "
                  f"marchés={r['markets']:16s} crédits={r['credits_used']}")

    # (a) 6 clôtures, une/match, < 25 min du tip-off, marché du verdict
    print("\n[a] Clôtures (attendu : 6, une/match, < 25 min du tip-off, marché du verdict) :")
    closing = conn.execute(
        "SELECT match_id, markets, collected_at FROM collection_log "
        "WHERE target_name='closing' ORDER BY match_id").fetchall()
    closing_by_match: dict[str, list] = {}
    for r in closing:
        closing_by_match.setdefault(r["match_id"], []).append(r)
    a_ok = True
    print(f"    → {len(closing)} lignes closing")
    for mid, _h, _a, tipoff, mkt, _l in MATCHES:
        cs = closing_by_match.get(mid, [])
        if len(cs) != 1:
            a_ok = False
            print(f"    {mid} : {len(cs)} clôture(s) ❌ (attendu 1)")
            continue
        r = cs[0]
        ct = datetime.fromisoformat(r["collected_at"])
        dmin = (tipoff - ct).total_seconds() / 60
        good = (r["markets"] == mkt) and (0 < dmin < 25)
        a_ok = a_ok and good
        print(f"    {mid} @ {ct:%H:%M} [{dmin:4.0f} min avant tip] marché={r['markets']} "
              f"(verdict={mkt}) {'✅' if good else '❌'}")

    # (b) Zéro snapshot post-tip-off (garde f9b0a0c)
    print("\n[b] Snapshots post-tip-off (attendu : 0 — garde f9b0a0c) :")
    post = conn.execute("""
        SELECT o.match_id, m.tipoff_utc, o.snapshot_at
        FROM odds_snapshots o JOIN matches m ON o.match_id = m.match_id
        WHERE datetime(replace(o.snapshot_at,'Z','+00:00')) > datetime(replace(m.tipoff_utc,'Z','+00:00'))
        ORDER BY o.match_id, o.snapshot_at""").fetchall()
    b_ok = len(post) == 0
    if post:
        print(f"    ❌ {len(post)} snapshots APRÈS tip-off :")
        for r in post:
            print(f"       {r['match_id']} snap {r['snapshot_at']} > tip {r['tipoff_utc']}")
    else:
        print("    ✅ Aucun snapshot post-tip-off (garde autoritaire base OK).")
    print("    Dernier snapshot vs tip-off par match :")
    for mid, _h, _a, tipoff, _m, _l in MATCHES:
        row = conn.execute("SELECT COUNT(*) c, MAX(snapshot_at) last FROM odds_snapshots "
                           "WHERE match_id=?", (mid,)).fetchone()
        last = row["last"]
        marge = ""
        if last:
            marge = f" (dernier à H-{(tipoff-datetime.fromisoformat(last)).total_seconds()/3600:.2f})"
        print(f"       {mid} : {row['c']} snaps, dernier {last}{marge}")

    # (c) Zéro doublon
    print("\n[c] Doublons collection_log (attendu : 0) :")
    dups = conn.execute(
        "SELECT match_id, target_name, COUNT(*) c FROM collection_log "
        "GROUP BY match_id, target_name HAVING c > 1").fetchall()
    c_ok = len(dups) == 0
    if dups:
        print(f"    ❌ {len(dups)} doublons :")
        for d in dups:
            print(f"       {d['match_id']} / {d['target_name']} : {d['c']}×")
    else:
        print(f"    ✅ Aucun doublon ({len(rows)} entrées, toutes (match_id,target_name) uniques).")

    # (e) Crédits loggés == crédits réels des chemins tracés
    print("\n[e] Crédits :")
    logged = conn.execute("SELECT COALESCE(SUM(credits_used),0) s FROM collection_log").fetchone()["s"]
    logged_path_real = total_credits - morning_credits
    e_ok = logged == logged_path_real
    print(f"    Crédits RÉELS totaux (tous appels get_odds)              : {total_credits}")
    print(f"    dont matin (non tracé dans collection_log, chantier connu) : {morning_credits}")
    print(f"    Crédits réels des chemins TRACÉS (total − matin)          : {logged_path_real}")
    print(f"    Crédits LOGGÉS (SUM collection_log.credits_used)          : {logged}")
    print(f"    → loggés == réels tracés : {'✅' if e_ok else '❌'} "
          f"(plus de sur-comptage par match)")
    print("    Ventilation crédits loggés par cible :")
    for r in conn.execute("SELECT target_name, COUNT(*) n, SUM(credits_used) s "
                          "FROM collection_log GROUP BY target_name ORDER BY MIN(target_hours) DESC"):
        print(f"       {r['target_name']:11s} : {r['n']} lignes, {r['s']} crédits")
    print(f"    Ticks actifs (≥1 appel) : {active_ticks} / {N_TICKS}")

    conn.close()

    # ── Verdict global ──
    print("\n" + "=" * 88)
    checks = [("(a) 6 clôtures conformes", a_ok), ("(b) 0 snapshot post-tip-off", b_ok),
              ("(c) 0 doublon", c_ok), ("(e) crédits loggés == réels tracés", e_ok)]
    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    all_ok = all(ok for _, ok in checks)
    print("=" * 88)
    print("RÉSULTAT :", "✅ TOUS LES INVARIANTS TENUS" if all_ok else "❌ RÉGRESSION DÉTECTÉE")
    print("=" * 88)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
