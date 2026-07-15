"""Chargement centralisé de la configuration.

Deux sources bien distinctes :
- `config.yaml` : paramètres non secrets et versionnés (seuils des règles, planning,
  budget quota, fuseau d'affichage).
- variables d'environnement (fichier `.env`) : secrets (clés API, token Telegram) et
  paramètres propres à la machine (chemin de la base, niveau de log).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Racine du projet = deux niveaux au-dessus de ce fichier (src/common/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "nba_odds.db"


@dataclass(frozen=True)
class Settings:
    """Secrets et paramètres machine, issus des variables d'environnement.

    Immuable (frozen) : la configuration ne change pas en cours d'exécution.
    """

    odds_api_key: str
    balldontlie_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    database_path: Path
    log_level: str


def load_settings() -> Settings:
    """Charge le fichier `.env` puis lit les variables d'environnement."""
    load_dotenv(PROJECT_ROOT / ".env")
    database_path = os.getenv("DATABASE_PATH") or str(DEFAULT_DATABASE_PATH)
    return Settings(
        odds_api_key=os.getenv("ODDS_API_KEY", ""),
        balldontlie_api_key=os.getenv("BALLDONTLIE_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        database_path=Path(database_path),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Charge et renvoie le contenu de `config.yaml` sous forme de dictionnaire."""
    config_path = path or DEFAULT_CONFIG_PATH
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)
