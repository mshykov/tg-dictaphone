"""Точка входу: python -m tg_dictaphone

Стартові перевірки залежностей, конкурентна обробка апдейтів,
drop_pending_updates (кліки, зроблені поки бот лежав, НЕ виконуються).
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import threading

from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

from . import config, handlers, tmuxio
from . import transcribe as stt

log = logging.getLogger("dictaphone")


def preflight() -> list[str]:
    problems = []
    if not shutil.which("tmux"):
        problems.append("tmux не знайдено в PATH — бот марний без нього")
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg не знайдено в PATH — голосові не працюватимуть")
    if importlib.util.find_spec("mlx_whisper") is None:
        problems.append("mlx_whisper не встановлено — голосові не працюватимуть")
    return problems


async def post_init(app: Application) -> None:
    cfg = config.CFG
    pid, note = await tmuxio.bind_pane()
    if pid:
        text = f"🚀 Бот запущено.\n📌 Панель: {note}"
    else:
        text = f"🚀 Бот запущено.\n⚠️ {note} — зроби /bind, коли сесія буде."
    try:
        await app.bot.send_message(cfg.owner_id, text)
    except Exception:
        log.warning("Не зміг надіслати привітання власнику")
    threading.Thread(target=stt.warmup, daemon=True).start()


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = config.init()
    handlers.setup(cfg)
    tmuxio.setup(cfg.session)
    for problem in preflight():
        log.warning("PREFLIGHT: %s", problem)
    log.info("Старт. Сесія: %s | доступ лише user_id %s", cfg.session, cfg.owner_id)

    app = (
        Application.builder()
        .token(cfg.token)
        .concurrent_updates(True)   # ⛔️ Перервати працює навіть під час довгих задач
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], handlers.on_help))
    app.add_handler(CommandHandler("status", handlers.on_status))
    app.add_handler(CommandHandler("peek", handlers.on_peek))
    app.add_handler(CommandHandler("screen", handlers.on_screen))
    app.add_handler(CommandHandler("key", handlers.on_key))
    app.add_handler(CommandHandler("bind", handlers.on_bind))
    app.add_handler(CommandHandler("dump", handlers.on_dump))
    app.add_handler(CallbackQueryHandler(handlers.on_button))
    media = filters.VOICE | filters.AUDIO | filters.Document.ALL
    app.add_handler(MessageHandler(media & ~filters.UpdateType.EDITED, handlers.on_voice))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED,
            handlers.on_text,
        )
    )
    app.add_error_handler(handlers.on_error)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
