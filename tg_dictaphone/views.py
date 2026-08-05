"""Побудова панелей стану + реєстр поколінь клавіатур.

Кожна відмальована панель отримує нове «покоління» (gen). Кнопки несуть
gen у callback_data — натискання на застарілу панель нічого не надсилає
в термінал, а пропонує свіжу. Це закриває і кнопки з-перед рестарту,
і offline-кліки, і подвійні панелі.
"""

from __future__ import annotations

import html
import itertools
import logging
from collections import OrderedDict
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from . import screen as scr
from . import tmuxio

log = logging.getLogger("dictaphone.views")

# клавіші, які панель взагалі має право надіслати в термінал
KEY_ALLOW = {"Enter", "Escape", "Space", "Up", "Down"} | {str(d) for d in range(1, 10)}

_GEN = itertools.count(1)
PANELS: "OrderedDict[int, dict]" = OrderedDict()  # gen -> {"chat", "full"}
CURRENT: dict[int, int] = {}                       # chat -> актуальний gen
ACTIVE_VIEW: dict[int, int] = {}                   # chat -> message_id панелі


def _new_gen(chat_id: int, full: bool) -> int:
    gen = next(_GEN)
    PANELS[gen] = {"chat": chat_id, "full": full}
    while len(PANELS) > 64:
        PANELS.popitem(last=False)
    CURRENT[chat_id] = gen
    return gen


def is_current(chat_id: int, gen: int) -> bool:
    return CURRENT.get(chat_id) == gen


def panel_full(gen: int) -> bool:
    return PANELS.get(gen, {}).get("full", False)


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


async def build_view(
    chat_id: int, *, full: bool = False, note: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    stamp = datetime.now().strftime("%H:%M:%S")
    sess = html.escape(tmuxio.SESSION)

    if not await tmuxio.session_alive():
        gen = _new_gen(chat_id, full)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄", callback_data=f"r:{gen}")]]
        )
        return (
            f"❌ tmux-сесії <b>{sess}</b> не існує. Підніми її на Mac. · {stamp}",
            kb,
        )

    raw = await tmuxio.capture()
    info = scr.analyze(raw)
    gen = _new_gen(chat_id, full)
    rows: list[list[InlineKeyboardButton]] = []
    parts: list[str] = []
    if note:
        parts.append(f"ℹ️ <i>{html.escape(note)}</i>")

    def k(key: str) -> str:
        return f"k:{gen}:{key}"

    def add_tail(n: int) -> None:
        tail = scr.useful_lines(raw or "", n, display=True)
        if tail:
            body = clip(chr(10).join(tail), 1200)
            parts.append(f"\n<pre>{html.escape(body)}</pre>")

    if info.state == "unknown":
        parts.append(f"⚠️ <b>Не зміг прочитати екран</b> · {stamp}")
        parts.append("Сесія є, але capture-pane не вдався. Це НЕ означає, що панель вільна.")
        rows.append([InlineKeyboardButton("🔄 Спробувати ще", callback_data=f"r:{gen}")])

    elif info.state == "waiting":
        parts.append(f"⏸ <b>Агент чекає на тебе</b> · {stamp}")
        if info.tool:
            parts.append(f"\n🔧 <code>{html.escape(info.tool)}</code>")
        if info.question:
            parts.append(f"\n<blockquote>{html.escape(info.question)}</blockquote>")
        if not info.tool and not info.question:
            add_tail(8)  # нічого не розпізналось — покажи хоч екран

        shown = [o for o in info.options if o[0] in KEY_ALLOW][:8]
        for num, label in shown:
            rows.append(
                [InlineKeyboardButton(f"{num} · {label}", callback_data=k(num))]
            )
        hidden = len(info.options) - len(shown)
        if hidden > 0:
            parts.append(f"\n<i>…і ще {hidden} варіант(и) — обери цифрою текстом</i>")

        if info.checkboxes:
            rows.append([
                InlineKeyboardButton("↑", callback_data=k("Up")),
                InlineKeyboardButton("↓", callback_data=k("Down")),
                InlineKeyboardButton("␣ вибрати", callback_data=k("Space")),
            ])
            rows.append([InlineKeyboardButton("⏎ підтвердити", callback_data=k("Enter"))])
        elif not info.options:
            rows.append([InlineKeyboardButton("⏎ Enter", callback_data=k("Enter"))])

        rows.append([InlineKeyboardButton("🔄 Оновити", callback_data=f"r:{gen}")])
        parts.append("\n✍️ <i>Свій варіант — надішли текст або голосове.</i>")

    elif info.state == "working":
        parts.append(f"⚙️ <b>Агент працює</b> · {stamp}")
        add_tail(4)
        rows.append([
            InlineKeyboardButton("🔄 Оновити", callback_data=f"r:{gen}"),
            InlineKeyboardButton("⛔️ Перервати", callback_data=k("Escape")),
        ])

    else:  # idle
        parts.append(f"✅ <b>Панель вільна</b> · {stamp}")
        add_tail(10)
        rows.append([InlineKeyboardButton("🔄 Оновити", callback_data=f"r:{gen}")])

    if full and raw is not None:
        body = clip(chr(10).join(scr.useful_lines(raw, 60, display=True)), 2600)
        parts.append(f"\n<pre>{html.escape(body)}</pre>")
        rows.append([InlineKeyboardButton("🔽 Згорнути", callback_data=f"v:{gen}:short")])
    else:
        rows.append([
            InlineKeyboardButton("📜 Останні 60 рядків", callback_data=f"v:{gen}:full"),
            InlineKeyboardButton("Esc", callback_data=k("Escape")),
        ])

    return "\n".join(parts), InlineKeyboardMarkup(rows)


async def disarm_previous(bot, chat_id: int) -> None:
    mid = ACTIVE_VIEW.get(chat_id)
    if not mid:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=mid, reply_markup=None
        )
    except BadRequest:
        pass


async def push_view(bot, chat_id: int, *, full: bool = False, note: str = "") -> None:
    text, kb = await build_view(chat_id, full=full, note=note)
    await disarm_previous(bot, chat_id)
    msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    ACTIVE_VIEW[chat_id] = msg.message_id


async def edit_view(
    bot, chat_id: int, message_id: int, *, full: bool = False, note: str = ""
) -> None:
    text, kb = await build_view(chat_id, full=full, note=note)
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
        ACTIVE_VIEW[chat_id] = message_id
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
