"""Bot d'écoute Telegram (étape 1.5) : réception des clics sur les boutons inline.

Contrairement au collecteur/analyseur/notificateur (lancés par cron puis s'arrêtent),
ce composant est un **processus long-running** : il interroge Telegram en continu
(**polling**) et réagit aux clics. Le webhook (Telegram pousse vers une URL publique)
sera introduit en phase 4 (AWS).

Ce module concentre la glue `python-telegram-bot` (asynchrone). Toute la logique
métier — décodage du callback, autorisation, cote au clic, enregistrement idempotent —
vit dans des modules purs (`callbacks`, `odds`, `positions`) testables sans Telegram.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from common.config import Settings
from common.db import get_connection
from common.logging_config import get_logger
from listener.callbacks import is_authorized, parse_callback
from listener.handling import ClickResult, handle_click
from listener.positions import TAKE

logger = get_logger("listener")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fr_odds(odds: float | None) -> str:
    """Cote à la française (virgule), ou tiret si inconnue."""
    return f"{odds:.2f}".replace(".", ",") if odds is not None else "—"


def _confirmation_line(result: ClickResult, tz_name: str) -> str:
    """Ligne de confirmation ajoutée au message (persistante) après un clic retenu."""
    heure = datetime.now(ZoneInfo(tz_name)).strftime("%d/%m %H:%M")
    cote = _fr_odds(result.odds_at_click)
    if result.status == "duplicate":
        deja = "Positionné" if result.action == TAKE else "Passé"
        return f"• Décision déjà enregistrée ({deja})."
    if result.action == TAKE:
        return f"✅ Positionné @ {cote} — {heure}"
    return f"➖ Passé @ {cote} — {heure}"


def _toast(result: ClickResult) -> str:
    """Notification courte (toast) affichée à l'utilisateur au moment du clic."""
    if result.status == "stale":
        return "Ce verdict a été remplacé par une version plus récente."
    if result.status == "unknown":
        return "Verdict introuvable."
    if result.status == "duplicate":
        return "Décision déjà enregistrée pour ce verdict."
    cote = _fr_odds(result.odds_at_click)
    return (
        f"Position enregistrée @ {cote}."
        if result.action == TAKE
        else f"Passe enregistrée @ {cote}."
    )


def build_application(settings: Settings, config: dict) -> Application:
    """Construit l'`Application` python-telegram-bot avec le handler de clics."""
    allowed_chat_id = settings.telegram_chat_id
    db_path = settings.database_path
    tz_name = config["display"]["timezone"]

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        chat_id = query.message.chat_id if query.message else None
        if not is_authorized(chat_id, allowed_chat_id):
            logger.warning("Clic ignoré d'une conversation non autorisée : %s", chat_id)
            await query.answer()
            return

        parsed = parse_callback(query.data)
        if parsed is None:
            logger.warning("callback_data non reconnu : %r", query.data)
            await query.answer()
            return

        action, verdict_id = parsed
        callback_message_id = query.message.message_id if query.message else None
        conn = get_connection(db_path)
        try:
            result = handle_click(
                conn,
                verdict_id=verdict_id,
                callback_message_id=callback_message_id,
                action=action,
                clicked_at=_now_iso(),
            )
        finally:
            conn.close()

        await query.answer(text=_toast(result))
        # On ne finalise (édition du message) que si le clic a été retenu : un clic
        # périmé ou sur un verdict inconnu ne touche pas au message.
        if result.status in ("recorded", "duplicate"):
            await _finalize_message(query, _confirmation_line(result, tz_name))

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CallbackQueryHandler(on_callback))
    return application


async def _finalize_message(query, line: str) -> None:
    """Retire les boutons et ajoute la ligne de confirmation au message d'origine."""
    if query.message is None:
        return
    original = query.message.text_html or ""
    try:
        await query.edit_message_text(
            text=f"{original}\n\n{line}",
            parse_mode="HTML",
            reply_markup=None,
        )
    except BadRequest as exc:
        # Message déjà finalisé (double clic sur un ancien message) : sans gravité.
        logger.info("Message non modifié : %s", exc)
