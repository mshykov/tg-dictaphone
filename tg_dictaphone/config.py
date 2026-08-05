"""Конфіг: читає ~/.config/tgbot.env (якщо є), потім ENV, валідує.

Файл env — прості пари KEY=VALUE, # — коментар. Значення з реального
оточення мають пріоритет над файлом (setdefault).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Cfg:
    token: str
    owner_id: int
    session: str
    model: str
    agent_hints: tuple[str, ...]
    max_prompt_chars: int
    max_voice_seconds: int
    max_file_mb: int        # Bot API getFile не віддає файли > 20 МБ
    watch_max_seconds: int  # скільки максимум стежити за довгою задачею
    watch_heartbeat: int    # як часто оновлювати "ще працює"
    pending_ttl: int        # життя кнопки «Надіслати агенту», сек


CFG: Cfg | None = None


def _load_env_file() -> Path:
    path = Path(os.environ.get("TGBOT_ENV", "~/.config/tgbot.env")).expanduser()
    if not path.is_file():
        return path
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return path


def init() -> Cfg:
    global CFG
    env_path = _load_env_file()
    missing = [k for k in ("TG_TOKEN", "TG_OWNER_ID") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Немає змінних {', '.join(missing)} — задай в оточенні або у {env_path}"
        )
    hints = tuple(
        h.strip().lower()
        for h in os.environ.get("AGENT_HINTS", "claude,node").split(",")
        if h.strip()
    )
    CFG = Cfg(
        token=os.environ["TG_TOKEN"],
        owner_id=int(os.environ["TG_OWNER_ID"]),
        session=os.environ.get("TMUX_SESSION", "agents"),
        model=os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
        agent_hints=hints or ("claude",),
        max_prompt_chars=int(os.environ.get("MAX_PROMPT_CHARS", "8000")),
        max_voice_seconds=int(os.environ.get("MAX_VOICE_SECONDS", "600")),
        max_file_mb=int(os.environ.get("MAX_FILE_MB", "19")),
        watch_max_seconds=int(os.environ.get("WATCH_MAX_SECONDS", "1800")),
        watch_heartbeat=int(os.environ.get("WATCH_HEARTBEAT", "180")),
        pending_ttl=int(os.environ.get("PENDING_TTL", "900")),
    )
    return CFG
