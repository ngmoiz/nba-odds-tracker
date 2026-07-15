"""Décodage du `callback_data` des boutons et contrôle d'autorisation.

Contrat défini par le notificateur (étape 1.4, `formatting.build_position_buttons`) :
- `pos:{verdict_id}`  → l'utilisateur se positionne ('take') ;
- `skip:{verdict_id}` → l'utilisateur passe ('pass').

Fonctions pures, sans dépendance Telegram, donc testables directement.
"""
from __future__ import annotations

from listener.positions import PASS, TAKE

# Préfixe de callback → action métier.
_PREFIX_TO_ACTION = {"pos": TAKE, "skip": PASS}


def parse_callback(data: str | None) -> tuple[str, int] | None:
    """Décode `pos:{id}` / `skip:{id}` en `(action, verdict_id)`, ou None si invalide."""
    if not data or ":" not in data:
        return None
    prefix, _, raw_id = data.partition(":")
    action = _PREFIX_TO_ACTION.get(prefix)
    if action is None:
        return None
    try:
        verdict_id = int(raw_id)
    except ValueError:
        return None
    return action, verdict_id


def is_authorized(chat_id: object, allowed_chat_id: str) -> bool:
    """Vrai si le clic provient bien de la conversation autorisée.

    Sans ce filtre, n'importe qui connaissant l'@ du bot pourrait injecter des
    décisions dans la base. La comparaison se fait sur des chaînes (le chat_id
    Telegram est un entier, `TELEGRAM_CHAT_ID` une chaîne d'environnement).
    """
    return bool(allowed_chat_id) and str(chat_id) == str(allowed_chat_id)
