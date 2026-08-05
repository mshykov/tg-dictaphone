"""Асинхронна обгортка над tmux.

Ключові гарантії безпеки:
  - сесія матчиться ТОЧНО (`=name`), без префіксного збігу;
  - усі операції йдуть у ПРИВ'ЯЗАНУ панель (immutable pane id %N),
    а не в "активну панель сесії", яка може змінитись;
  - текст вставляється bracketed paste-ом (load-buffer + paste-buffer -p):
    безпечно для '-' на початку, перенесень рядків і будь-якого юнікоду;
  - складене надсилання (текст + Enter) — під замком, щоб конкурентні
    хендлери не перемішували ввід;
  - кожен виклик tmux має таймаут і не блокує event loop.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("dictaphone.tmux")

SESSION = "agents"  # виставляє main.setup() при старті
PANE: dict[str, str | None] = {"id": None}
SEND_LOCK = asyncio.Lock()


def setup(session: str) -> None:
    global SESSION
    SESSION = session


def _target() -> str:
    return f"={SESSION}"  # '=' — точний збіг імені сесії


async def run(
    *args: str, timeout: float = 5.0, input_bytes: bytes | None = None
) -> tuple[bool, str]:
    """Запускає tmux <args>. Повертає (ok, stdout|stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "tmux не знайдено в PATH"
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_bytes), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"tmux {' '.join(args[:2])}: таймаут {timeout}s"
    text = ((out or err) or b"").decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("tmux %s → rc=%s %s", " ".join(args), proc.returncode, text)
    return proc.returncode == 0, text


async def session_alive() -> bool:
    ok, _ = await run("has-session", "-t", _target())
    return ok


async def bind_pane() -> tuple[str | None, str]:
    """Прив'язує активну панель активного вікна сесії.

    Повертає (pane_id | None, людський опис/причина відмови).
    """
    ok, out = await run(
        "list-panes", "-s", "-t", _target(), "-F",
        "#{pane_id}\t#{window_active}\t#{pane_active}\t#{pane_current_command}",
    )
    if not ok:
        PANE["id"] = None
        return None, out or f"сесії '{SESSION}' немає"
    for line in out.splitlines():
        cells = (line.split("\t") + ["", "", "", ""])[:4]
        pid, w_act, p_act, cmd = cells
        if w_act == "1" and p_act == "1":
            PANE["id"] = pid
            note = f"{pid} ({cmd})"
            log.info("Прив'язано панель %s", note)
            return pid, note
    PANE["id"] = None
    return None, "не знайшов активної панелі"


async def pane_alive() -> bool:
    if not PANE["id"]:
        return False
    ok, out = await run("display-message", "-p", "-t", PANE["id"], "#{pane_id}")
    return ok and out == PANE["id"]


async def ensure_pane() -> tuple[str | None, str]:
    """pane_id або (None, причина). Якщо панель зникла — переприв'язується
    до нової активної панелі й повідомляє про це в note."""
    if await pane_alive():
        return PANE["id"], ""
    pid, note = await bind_pane()
    if pid:
        return pid, f"переприв'язано: {note}"
    return None, note


async def pane_command() -> str:
    if not PANE["id"]:
        return "?"
    ok, out = await run(
        "display-message", "-p", "-t", PANE["id"], "#{pane_current_command}"
    )
    return out if ok else "?"


async def capture(lines: int = 80) -> str | None:
    """Знімок екрану. None = НЕ ВДАЛОСЯ прочитати (це не «порожньо»).
    -J склеює перенесені рядки, щоб питання/варіанти не різались."""
    pid, _ = await ensure_pane()
    if not pid:
        return None
    ok, out = await run("capture-pane", "-p", "-J", "-t", pid, "-S", f"-{lines}")
    return out if ok else None


async def send_keys(*keys: str) -> tuple[bool, str]:
    """Надсилає іменовані клавіші ('Enter', 'Escape', 'C-c', цифри…).
    '--' відсікає парсинг опцій tmux."""
    pid, note = await ensure_pane()
    if not pid:
        return False, note
    ok, err = await run("send-keys", "-t", pid, "--", *keys)
    return ok, (note if ok else err)


async def paste_text(text: str) -> tuple[bool, str]:
    """Вставляє текст bracketed paste-ом, без інтерпретації по дорозі."""
    pid, note = await ensure_pane()
    if not pid:
        return False, note
    ok, err = await run("load-buffer", "-b", "tgbot", "-", input_bytes=text.encode())
    if not ok:
        return False, err or "load-buffer не вдався"
    ok, err = await run("paste-buffer", "-p", "-d", "-b", "tgbot", "-t", pid)
    return ok, (note if ok else err or "paste-buffer не вдався")


async def send_prompt(text: str) -> tuple[bool, str]:
    """Вставити текст і підтвердити Enter-ом — атомарно відносно інших
    надсилань. Повертає (ok, note|помилка); note може містити
    'переприв'язано: …', якщо панель змінилась."""
    async with SEND_LOCK:
        ok, msg = await paste_text(text)
        if not ok:
            return False, f"вставка не вдалась: {msg}"
        await asyncio.sleep(0.35)
        ok, err = await send_keys("Enter")
        if not ok:  # одна повторна спроба — і чесний звіт, якщо не вийшло
            ok, err = await send_keys("Enter")
        if not ok:
            return False, "текст УЖЕ в полі вводу, але Enter не пройшов — надішли ⏎ вручну (/key Enter)"
        return True, msg
