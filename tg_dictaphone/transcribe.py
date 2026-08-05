"""ffmpeg → mlx-whisper. Блокуюча робота — викликати через asyncio.to_thread.

Помилки для чату загорнуті в TranscribeError (без сирих шляхів/трейсбеків);
діагностика ffmpeg (хвіст stderr) іде в лог.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("dictaphone.stt")


class TranscribeError(Exception):
    """Повідомлення, безпечне для показу користувачу."""


def transcribe(src: Path, model: str) -> str:
    wav = src.with_suffix(".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
            check=True, capture_output=True, timeout=120,
        )
    except FileNotFoundError:
        raise TranscribeError("ffmpeg не знайдено (brew install ffmpeg)")
    except subprocess.TimeoutExpired:
        raise TranscribeError("ffmpeg: таймаут конвертації")
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode(errors="replace").strip().splitlines()[-3:]
        log.error("ffmpeg: %s", " | ".join(tail))
        raise TranscribeError("ffmpeg не зміг конвертувати аудіо (деталі в лозі)")
    import mlx_whisper  # лінивий імпорт: перший виклик тягне модель

    out = mlx_whisper.transcribe(str(wav), path_or_hf_repo=model)
    wav.unlink(missing_ok=True)
    return (out.get("text") or "").strip()


def warmup() -> None:
    """Прогрів у фоновому потоці при старті: хоча б імпорт модуля,
    щоб перше голосове не платило й за нього."""
    try:
        import mlx_whisper  # noqa: F401
        log.info("mlx_whisper готовий (модель підтягнеться при першому голосовому)")
    except Exception as e:
        log.warning("mlx_whisper недоступний: %s — голосові не працюватимуть", e)
