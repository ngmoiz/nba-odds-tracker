"""Configuration du logging structuré (règle 0.4.3).

Format des logs : `horodatage_UTC | composant | niveau | message`.
Chaque composant récupère son logger nommé via `get_logger("collector")`, etc.
On n'utilise jamais `print` : tout passe par le module standard `logging`.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

# Format commun à tous les composants (horodatage, composant, niveau, message).
LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"

# Garde-fou pour ne configurer le logging racine qu'une seule fois par processus.
_CONFIGURED = False


class UTCFormatter(logging.Formatter):
    """Formatte les horodatages en UTC ISO 8601 (la base est toujours en UTC)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.isoformat(timespec="seconds")


def configure_logging(level: str = "INFO") -> None:
    """Configure le logger racine (handler + format) une seule fois.

    À appeler une fois au démarrage de chaque composant, avec le niveau
    issu de la variable d'environnement LOG_LEVEL.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(UTCFormatter(LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)

    _CONFIGURED = True


def get_logger(component: str) -> logging.Logger:
    """Renvoie le logger nommé d'un composant (ex. "collector", "db")."""
    return logging.getLogger(component)
