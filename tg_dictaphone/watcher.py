"""Фоновий нагляд за реакцією агента.

Замінює блокуючий wait_until_settled() з v1:
  - живе окремою задачею — бот лишається чутливим (⛔️ Перервати працює);
  - «активність» = маркери роботи АБО зміна відбитка екрану (ловить
    агентів без маркерів; цифри/спінери у відбитку прибрані, щоб
    таймер не читався як активність);
  - довгі задачі: heartbeat «ще працює · N хв» замість тиші,
    сповіщення після завершення навіть через 20 хвилин;
  - стеля стеження конфігурується (WATCH_MAX_SECONDS), після неї бот
    чесно каже, що перестав стежити.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import screen as scr
from . import tmuxio, views

log = logging.getLogger("dictaphone.watch")

SETTLE_FIRST = 2.0
POLL = 2.5
CALM_POLLS = 2  # скільки поспіль «тихих» опитувань = агент завершив

_TASKS: dict[int, asyncio.Task] = {}


def start(bot, chat_id: int, cfg, status_mid: int | None = None) -> None:
    """Запускає (перезапускає) нагляд для чату. status_mid — повідомлення
    «⏳ …», яке можна оновлювати heartbeat-ами."""
    old = _TASKS.pop(chat_id, None)
    if old and not old.done():
        old.cancel()
    _TASKS[chat_id] = asyncio.create_task(_watch(bot, chat_id, cfg, status_mid))


async def _edit_status(bot, chat_id: int, mid: int | None, text: str) -> None:
    if mid is None:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=mid)
    except Exception:
        pass  # "not modified", застаріле повідомлення тощо — не критично


async def _watch(bot, chat_id: int, cfg, status_mid: int | None) -> None:
    try:
        await asyncio.sleep(SETTLE_FIRST)
        started = time.monotonic()
        last_beat = started
        prev_fp: str | None = None
        calm = 0
        while True:
            raw = await tmuxio.capture()
            info = scr.analyze(raw)
            fp = scr.fingerprint(raw)
            active = info.state == "working" or (
                prev_fp is not None and fp != prev_fp
            )
            prev_fp = fp
            if active:
                calm = 0
            else:
                calm += 1
                if calm >= CALM_POLLS:
                    break
            now = time.monotonic()
            if now - started > cfg.watch_max_seconds:
                mins = int(cfg.watch_max_seconds / 60)
                await _edit_status(
                    bot, chat_id, status_mid,
                    f"⏱ Минуло {mins} хв — перестаю стежити. /peek покаже стан.",
                )
                await views.push_view(bot, chat_id)
                return
            if status_mid and now - last_beat >= cfg.watch_heartbeat:
                last_beat = now
                await _edit_status(
                    bot, chat_id, status_mid,
                    f"⚙️ Агент ще працює · {int((now - started) / 60)} хв",
                )
            await asyncio.sleep(POLL)
        await _edit_status(bot, chat_id, status_mid, "✅ Агент відреагував:")
        await views.push_view(bot, chat_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("watcher впав")
        try:
            await views.push_view(bot, chat_id, note="нагляд впав — деталі в лозі")
        except Exception:
            pass
