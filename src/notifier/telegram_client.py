"""Client d'envoi Telegram (section 7.1).

Envoi = **requêtes HTTP simples** via `httpx` (déjà une dépendance du projet). On
n'utilise volontairement PAS `python-telegram-bot` : cette bibliothèque sera
introduite à l'étape 1.5, uniquement pour la **réception** des clics (mode polling).

Le client est minimal : il POST sur l'API Bot et signale les échecs. Toute la mise
en forme des messages vit dans `notifier.formatting`.

Sécurité : le token du bot apparaît dans l'URL des requêtes. `logging_config` réduit
déjà `httpx`/`httpcore` au niveau WARNING, donc les URLs (et le token) ne sont jamais
écrites dans les logs applicatifs.
"""
from __future__ import annotations

from typing import Any

import httpx

from common.logging_config import get_logger

logger = get_logger("notifier")

BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT = 20.0  # secondes


class TelegramError(Exception):
    """Échec d'envoi d'un message Telegram (réseau ou réponse d'erreur de l'API)."""


class TelegramClient:
    """Client synchrone pour l'API Bot Telegram (envoi de messages).

    `transport` permet d'injecter un faux transport httpx dans les tests (aucun
    appel réseau réel), comme pour le client The Odds API.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    @property
    def is_configured(self) -> bool:
        """Vrai si token et chat_id sont renseignés (sinon aucun envoi possible)."""
        return bool(self._token and self._chat_id)

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def send_message(
        self, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Envoie un message texte. Lève `TelegramError` en cas d'échec.

        - `parse_mode=HTML` : la mise en forme utilise des balises HTML simples.
        - `disable_web_page_preview` : pas d'aperçu de lien parasite.
        - `reply_markup` (optionnel) : clavier inline (boutons de position).
        """
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            response = self._client.post(f"/bot{self._token}/sendMessage", json=payload)
        except httpx.RequestError as exc:
            raise TelegramError(f"Erreur réseau vers Telegram : {exc}") from exc

        if response.status_code != 200:
            raise TelegramError(f"HTTP {response.status_code} : {response.text[:200]}")

        return response.json()
