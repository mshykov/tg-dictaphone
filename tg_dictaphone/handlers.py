"""Telegram-хендлери.

Безпекова модель:
  - лише власник (TG_OWNER_ID) і лише приватний чат — у групі бот мовчить,
    щоб екран термінала не світився стороннім;
  - будь-який текст іде в термінал ТІЛЬКИ після кнопки підтвердження;
  - fail-closed: якщо в панелі не агент (AGENT_HINTS) — надсилання
    блокується, потрібне явне «Все одно надіслати»;
  - клавіатурні кнопки прив'язані до покоління панелі й до стану екрана —
    застаріла кнопка нічого не надсилає;
  - callback-дії — тільки з білого списку; невідомі відповідаються
    і ігноруються (НЕ трактуються як підтвердження).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import screen as scr
from . import tmuxio, views, watcher
from . import transcribe as stt
from .config import Cfg
from .views import clip

log = logging.getLogger("dictaphone")

CFG: Cfg = None  # type: ignore[assignment]  # виставляє main.setup()


def setup(cfg: Cfg) -> None:
    global CFG
    CFG = cfg


# токен підтвердження -> {"text": str, "ts": float}
PENDING: dict[str, dict] = {}

# куди /dump складає знімки екрана для тестових фікстур
FIXTURES_DIR = Path(
    os.environ.get(
        "FIXTURES_DIR",
        Path(__file__).resolve().parents[1] / "tests" / "fixtures",
    )
)
_DUMP_LABELS = ("waiting", "working", "idle", "unknown", "unverified")

# клавіші, після яких агент, найпевніше, почне щось робити
_ACTION_KEYS = {"Enter", "Escape"} | {str(d) for d in range(1, 10)}
# навігаційні клавіші для /key — без довгого стеження
_NAV_KEYS = {"Up", "Down", "Left", "Right", "Space", "Tab", "S-Tab", "BTab",
             "Home", "End", "PageUp", "PageDown"}


# ------------------------------------------------------------------ helpers
def _prune_pending() -> None:
    now = time.monotonic()
    expired = [t for t, e in PENDING.items() if now - e["ts"] > CFG.pending_ttl]
    for t in expired:
        PENDING.pop(t, None)
    while len(PENDING) > 64:
        PENDING.pop(next(iter(PENDING)), None)


def owner_only(fn):
    async def wrap(u: Update, c: ContextTypes.DEFAULT_TYPE):
        user, chat, q = u.effective_user, u.effective_chat, u.callback_query
        allowed = (
            user is not None
            and user.id == CFG.owner_id
            and (chat is None or chat.type == "private")
        )
        if not allowed:
            if q:  # завжди відповідаємо на callback, щоб не крутився спінер
                try:
                    await q.answer()
                except Exception:
                    pass
            if user and user.id != CFG.owner_id:
                log.warning("ВІДКИНУТО: user_id=%s @%s", user.id, user.username)
            elif chat and chat.type != "private":
                log.warning("ВІДКИНУТО не-приватний чат %s (%s)", chat.id, chat.type)
            return
        return await fn(u, c)

    return wrap


@asynccontextmanager
async def typing(bot, chat_id: int):
    """«Друкує…», що не гасне через 5 секунд."""
    async def loop() -> None:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


def _looks_like_agent(cmd: str) -> bool:
    return any(h in cmd.lower() for h in CFG.agent_hints)


async def confirm(msg, text: str) -> None:
    _prune_pending()
    token = uuid.uuid4().hex[:12]
    PENDING[token] = {"text": text, "ts": time.monotonic()}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Надіслати агенту", callback_data=f"go:{token}"),
        InlineKeyboardButton("✖️ Скасувати", callback_data=f"no:{token}"),
    ]])
    sent = None
    try:
        sent = await msg.reply_text(clip(text, 3900), reply_markup=kb)
    finally:
        if sent is None:  # echo не пішов — не лишаємо токен-сироту
            PENDING.pop(token, None)


# ------------------------------------------------------------------ commands
@owner_only
async def on_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.effective_message.reply_text(
        "<b>Як це працює</b>\n\n"
        "Текст або голосове → промт на підтвердження → іде в прив'язану "
        "tmux-панель на Mac. Бот стежить за реакцією агента у фоні й "
        "покаже суть питання з кнопками — навіть якщо задача йде довго.\n\n"
        "<b>Захист</b>\n"
        "• якщо в панелі не агент — надсилання блокується (є явний override)\n"
        "• застарілі кнопки нічого не надсилають\n"
        "• працює лише в цьому приватному чаті\n\n"
        "<b>Команди</b>\n"
        "/peek — панель зі станом\n"
        "/screen — з останніми 60 рядками екрана\n"
        "/status — сесія, панель, розпізнаний стан\n"
        "/bind — переприв'язати активну панель tmux\n"
        "/key &lt;клавіші&gt; — вручну: <code>/key C-c</code>, <code>/key S-Tab</code>\n"
        "/dump [waiting|working|idle] — зберегти екран як тестову фікстуру, "
        "коли парсер помилився зі станом\n\n"
        f"Доступ лише для user_id <code>{CFG.owner_id}</code>.",
        parse_mode="HTML",
    )


@owner_only
async def on_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.effective_message
    _, sessions = await tmuxio.run("list-sessions")
    alive = await tmuxio.session_alive()
    pid, _ = await tmuxio.ensure_pane()
    cmd = await tmuxio.pane_command()
    info = scr.analyze(await tmuxio.capture())
    agent = "агент" if _looks_like_agent(cmd) else "⚠️ не схоже на агента"
    await msg.reply_text(
        f"{'✅' if alive else '❌'} сесія <b>{html.escape(CFG.session)}</b>\n"
        f"панель: <code>{html.escape(pid or '—')}</code> · "
        f"<code>{html.escape(cmd)}</code> ({agent})\n"
        f"стан: <b>{info.state}</b> · варіантів: {len(info.options)} · "
        f"чекбокси: {'так' if info.checkboxes else 'ні'}\n\n"
        f"<pre>{html.escape(sessions or '(жодної сесії)')}</pre>",
        parse_mode="HTML",
    )


@owner_only
async def on_bind(u: Update, c: ContextTypes.DEFAULT_TYPE):
    pid, note = await tmuxio.bind_pane()
    await u.effective_message.reply_text(f"📌 {note}" if pid else f"⚠️ {note}")


@owner_only
async def on_dump(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Знімок екрана → tests/fixtures/<мітка>_<час>.txt.

    Це петля супроводу парсера: бот показав не той стан → /dump <як мало
    бути> → фікстура вже лежить у репозиторії → полагодь screen.py,
    прожени тести. Без мітки зберігає як unverified (тест лише перевіряє,
    що парсер не падає)."""
    msg = u.effective_message
    label = c.args[0].lower() if c.args else "unverified"
    if label not in _DUMP_LABELS:
        await msg.reply_text(
            "Мітка — очікуваний стан: /dump waiting|working|idle|unknown "
            "(без мітки → unverified)"
        )
        return
    raw = await tmuxio.capture()
    if raw is None:
        await msg.reply_text("⚠️ Не зміг зняти екран — нема що зберігати.")
        return
    info = scr.analyze(raw)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{label}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    (FIXTURES_DIR / name).write_text(raw)
    verdict = info.state + (f" · варіантів {len(info.options)}" if info.options else "")
    hint = ""
    if label != "unverified" and label != info.state:
        hint = "\n❗ Мітка не збігається з парсером — тести це впіймають."
    await msg.reply_text(
        f"💾 tests/fixtures/{name}\nПарсер зараз каже: {verdict}{hint}\n"
        "⚠️ У знімку — реальний вміст термінала: переглянь/зачисти його "
        "перед комітом у публічний репозиторій."
    )


@owner_only
async def on_peek(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await views.push_view(c.bot, u.effective_chat.id, full=False)


@owner_only
async def on_screen(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await views.push_view(c.bot, u.effective_chat.id, full=True)


@owner_only
async def on_key(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.effective_message
    if not c.args:
        await msg.reply_text(
            "Напр.: /key Enter, /key Escape, /key C-c, /key Space, /key S-Tab"
        )
        return
    ok, detail = await tmuxio.send_keys(*c.args)
    if not ok:
        await msg.reply_text(f"⚠️ {detail}")
        return
    if all(a in _NAV_KEYS for a in c.args):  # навігація — без довгого стеження
        await asyncio.sleep(0.7)
        await views.push_view(c.bot, msg.chat_id)
        return
    status = await msg.reply_text("⏳ Надіслано — стежу за реакцією…")
    watcher.start(c.bot, msg.chat_id, CFG, status.message_id)


# ------------------------------------------------------------------ input
@owner_only
async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.effective_message
    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("Порожньо — нема що надсилати.")
        return
    if len(text) > CFG.max_prompt_chars:
        await msg.reply_text(f"Задовгий промт (>{CFG.max_prompt_chars} символів).")
        return
    log.info("Текст від власника: %d символів", len(text))
    await confirm(msg, text)


@owner_only
async def on_voice(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.effective_message
    media = msg.voice or msg.audio or msg.document
    if media is None:
        return
    mime = getattr(media, "mime_type", "") or ""
    if msg.document is not None and not mime.startswith("audio/"):
        await msg.reply_text("Це не аудіо-файл — надішли голосове або текст.")
        return
    duration = getattr(media, "duration", None)
    if duration and duration > CFG.max_voice_seconds:
        await msg.reply_text(
            f"Задовге аудіо ({duration}s > {CFG.max_voice_seconds}s)."
        )
        return
    size = getattr(media, "file_size", None)
    if size and size > CFG.max_file_mb * 1024 * 1024:
        await msg.reply_text(
            f"Завеликий файл (> {CFG.max_file_mb} МБ — ліміт Bot API)."
        )
        return

    log.info("Аудіо від власника (%ss, %s байт)", duration, size)
    async with typing(c.bot, msg.chat_id):
        try:
            f = await media.get_file()
        except Exception as e:
            log.exception("get_file не вдався")
            await msg.reply_text(f"Не зміг отримати файл: {e.__class__.__name__}")
            return
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "voice.bin"
            try:
                await f.download_to_drive(str(src))
            except Exception as e:
                log.exception("download не вдався")
                await msg.reply_text(f"Не зміг завантажити файл: {e.__class__.__name__}")
                return
            try:
                text = await asyncio.to_thread(stt.transcribe, src, CFG.model)
            except stt.TranscribeError as e:
                await msg.reply_text(f"Не розпізнав: {e}")
                return
            except Exception:
                log.exception("Транскрипція впала")
                await msg.reply_text("Не розпізнав (деталі в лозі).")
                return
    text = text.strip()
    if not text:
        await msg.reply_text("Порожня транскрипція.")
        return
    log.info("Транскрипція: %d символів", len(text))
    if len(text) > CFG.max_prompt_chars:
        text = text[: CFG.max_prompt_chars]
        await msg.reply_text("⚠️ Транскрипція задовга — обрізав до ліміту.")
    await confirm(msg, text)


# ------------------------------------------------------------------ buttons
async def _send_confirmed(q, c, token: str, entry: dict, *, forced: bool) -> None:
    chat_id = q.message.chat.id if q.message else CFG.owner_id
    text = entry["text"]

    # fail-closed: дивимось, ЩО зараз у панелі, ДО надсилання
    pid, bind_note = await tmuxio.ensure_pane()
    if not pid:
        await q.edit_message_text(f"⚠️ НЕ надіслано: {bind_note}")
        return
    cmd = await tmuxio.pane_command()
    agent = _looks_like_agent(cmd)
    if not agent and not forced:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Все одно надіслати", callback_data=f"gf:{token}"),
            InlineKeyboardButton("✖️ Скасувати", callback_data=f"no:{token}"),
        ]])
        await q.edit_message_text(
            f"⛔️ У панелі зараз <code>{html.escape(cmd)}</code> — не схоже на "
            f"агента. Текст піде туди як ввід (у shell — виконається!).\n"
            f"Надіслати все одно?\n\n{html.escape(clip(text, 3000))}",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    PENDING.pop(token, None)
    ok, detail = await tmuxio.send_prompt(text)
    if not ok:
        log.error("Не надіслано: %s", detail)
        await q.edit_message_text(clip(f"⚠️ НЕ надіслано: {detail}\n\n{text}", 3900))
        return

    lines = [f"→ надіслано в {html.escape(tmuxio.PANE['id'] or '?')}"]
    if not agent:
        lines.append(f"⚠️ панель: <code>{html.escape(cmd)}</code> (надіслано примусово)")
    if detail:  # напр., «переприв'язано: %5 (claude)»
        lines.append(f"ℹ️ {html.escape(detail)}")
    lines.append("")
    lines.append(html.escape(clip(text, 3400)))
    log.info("Надіслано %d символів у %s", len(text), tmuxio.PANE["id"])
    try:
        await q.edit_message_text("\n".join(lines), parse_mode="HTML")
    except BadRequest:
        pass
    status = await c.bot.send_message(chat_id, "⏳ Стежу за реакцією агента…")
    watcher.start(c.bot, chat_id, CFG, status.message_id)


@owner_only
async def on_button(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    chat_id = q.message.chat.id if q.message else CFG.owner_id
    parts = (q.data or "").split(":")
    action = parts[0] if parts else ""

    # -------- панельні дії: k / r / v — з перевіркою покоління
    if action in ("k", "r", "v"):
        try:
            gen = int(parts[1])
        except (IndexError, ValueError):
            await q.answer("Не зрозумів кнопку")
            return
        if not views.is_current(chat_id, gen):
            await q.answer("Панель застаріла — ось свіжа")
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await views.push_view(c.bot, chat_id)
            return

        if action == "r":
            await q.answer("Оновлено")
            await views.edit_view(
                c.bot, chat_id, q.message.message_id, full=views.panel_full(gen)
            )
            return

        if action == "v":
            await q.answer()
            await views.edit_view(
                c.bot, chat_id, q.message.message_id,
                full=(len(parts) > 2 and parts[2] == "full"),
            )
            return

        # action == "k" — інжекція клавіші
        key = parts[2] if len(parts) > 2 else ""
        if key not in views.KEY_ALLOW:
            await q.answer("Цю клавішу надіслати не можна")
            return
        if key != "Escape":  # Escape (перервати) має працювати завжди
            info = scr.analyze(await tmuxio.capture())
            if info.state != "waiting":
                await q.answer("Агент вже не чекає — оновлюю панель")
                await views.edit_view(
                    c.bot, chat_id, q.message.message_id, full=views.panel_full(gen)
                )
                return
        ok, detail = await tmuxio.send_keys(key)
        if not ok:
            await q.answer("Помилка", show_alert=True)
            try:
                await q.edit_message_text(f"⚠️ {html.escape(detail)}", parse_mode="HTML")
            except BadRequest:
                pass
            return
        log.info("Клавіша '%s' → %s", key, tmuxio.PANE["id"])
        await q.answer(f"→ {key}")

        if key in ("Space", "Up", "Down"):  # навігація в діалозі
            await asyncio.sleep(0.7)
            await views.edit_view(
                c.bot, chat_id, q.message.message_id, full=views.panel_full(gen)
            )
            return

        # Enter / Escape / цифра — агент почне реагувати
        views.ACTIVE_VIEW.pop(chat_id, None)
        try:
            await q.edit_message_text(f"⏳ Надіслано «{key}» — стежу за реакцією…")
        except BadRequest:
            pass
        watcher.start(
            c.bot, chat_id, CFG, q.message.message_id if q.message else None
        )
        return

    # -------- підтвердження промта: go / gf / no
    if action in ("go", "gf", "no"):
        token = parts[1] if len(parts) > 1 else ""
        await q.answer()
        if action == "no":
            PENDING.pop(token, None)
            await q.edit_message_text("Скасовано.")
            return
        _prune_pending()
        entry = PENDING.get(token)
        if entry is None:
            await q.edit_message_text(
                "Це підтвердження застаріло або вже використане — нічого не надіслано."
            )
            return
        await _send_confirmed(q, c, token, entry, forced=(action == "gf"))
        return

    # -------- невідома дія: явно ігноруємо, НІКОЛИ не трактуємо як «так»
    await q.answer()
    log.warning("Невідома callback-дія: %r", q.data)


# ------------------------------------------------------------------ errors
_last_err_notify = 0.0


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    global _last_err_notify
    log.exception("Помилка в хендлері", exc_info=context.error)
    now = time.monotonic()
    if now - _last_err_notify < 10:  # без штормів повідомлень
        return
    _last_err_notify = now
    try:
        await context.bot.send_message(
            CFG.owner_id,
            clip(f"⚠️ Помилка бота: {context.error!r}. Деталі в лозі.", 1000),
        )
    except Exception:
        pass
