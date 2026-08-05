"""Розбір знімка екрану tmux. Чисті функції без I/O — покриті тестами.

Головні принципи (винесені з розбору помилок v1):
  - нумерований блок вважається ДІАЛОГОМ лише з контекстом: курсор ❯ на
    одному з рядків, ask-маркер або рядок-питання одразу над блоком.
    Просто нумерований список у відповіді агента — НЕ діалог;
  - нумерація має бути 1..N — інакше це проза;
  - маркери роботи шукаються ДО фільтра шуму (рядок "esc to interrupt"
    ховається лише з показу, не від детектора);
  - чекбокси рахуються тільки всередині діалогу (markdown-списки
    "- [x] ..." у виводі не вмикають клавіатуру);
  - capture=None → стан "unknown", а не удаваний "idle";
  - рядок-питання (закінчується на «?») у самому хвості → waiting
    із вільною відповіддю — будь-якою мовою.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SCAN_LINES = 34     # вікно для маркерів роботи / інструмента
ASK_TAIL = 14       # хвіст, де живе суть питання
QUESTION_TAIL = 6   # рядок-питання має бути зовсім унизу
OPTION_TAIL = 20    # де шукати блок варіантів
BLOCK_SKIP = 5      # скільки не-опційних рядків знизу можна перескочити

CURSOR_RE = re.compile(r"^\s*[❯›»]")
OPTION_RE = re.compile(r"^\s*[❯›»>]?\s*[☐☑☒◉◯]?\s*(\d{1,2})[.)]\s+(\S.*)$")
CHECKBOX_RE = re.compile(r"\[[ xX✓]\]|[☐☑☒◉◯]")
# реальні tool-рядки Claude Code: "⏺ Bash(ls -la)", "● Read(main.go)",
# "mcp__server__tool(...)" — ім'я з великої або mcp__
TOOL_RE = re.compile(r"^\s*[⏺●○◆•*]?\s*((?:mcp__[\w.\-]+|[A-Z][\w.\-]{1,40}))\((.{1,160})")
# рядок-питання: кінчається на ? (можливо, в лапках/дужках) або y/n-підказкою
QUESTION_LINE_RE = re.compile(
    r"""\?[\s"'»”’)\]]*$|\((?:y/n|yes/no)\)\s*$|\[(?:y/n|yes/no)\]\s*$""",
    re.IGNORECASE,
)

WORKING_MARKERS = ("esc to interrupt", "thinking…", "thinking...")
ASK_MARKERS = ("do you want", "would you like", "select an option",
               "choose an option", "which would you")
# шум ховаємо лише з ПОКАЗУ; для визначення стану ці рядки потрібні
DISPLAY_NOISE = ("esc to", "tab to", "ctrl+", "shift+", "? for shortcuts")


@dataclass
class ScreenInfo:
    state: str  # waiting | working | idle | unknown
    options: list[tuple[str, str]] = field(default_factory=list)
    checkboxes: bool = False
    question: str = ""
    tool: str = ""


def _clean(line: str) -> str:
    line = re.sub(r"[│┃║╎┆┊|]", " ", line)
    line = re.sub(r"[─━═╌┄┈_]{4,}", "", line)
    return re.sub(r"\s+", " ", line).strip()


def useful_lines(screen: str, limit: int, *, display: bool = False) -> list[str]:
    """Останні `limit` ЗМІСТОВНИХ рядків: порожні/рамки не з'їдають бюджет.
    display=True додатково ховає хінти клавіш (тільки для показу)."""
    out: list[str] = []
    for raw in reversed(screen.splitlines()):
        c = _clean(raw)
        if not c:
            continue
        if display and c.lower().startswith(DISPLAY_NOISE):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def _option_block(tail: list[str]) -> tuple[list[tuple[str, str]], bool, int]:
    """Нижній суцільний блок нумерованих рядків.

    Повертає (опції, чи є курсор ❯, індекс першого рядка блоку в tail).
    Порожній результат, якщо блоку немає або нумерація не 1..N.
    """
    i = len(tail) - 1
    skipped = 0
    while i >= 0 and not OPTION_RE.match(tail[i]):
        skipped += 1
        if skipped > BLOCK_SKIP:
            return [], False, -1
        i -= 1
    if i < 0:
        return [], False, -1
    end = i
    while i >= 0 and OPTION_RE.match(tail[i]):
        i -= 1
    start = i + 1
    opts: list[tuple[str, str]] = []
    cursor = False
    for line in tail[start:end + 1]:
        m = OPTION_RE.match(line)
        opts.append((m.group(1), m.group(2)[:48]))
        cursor = cursor or bool(CURSOR_RE.match(line))
    nums = [int(n) for n, _ in opts]
    if nums != list(range(1, len(nums) + 1)):  # проза, а не меню
        return [], False, -1
    return opts, cursor, start


def extract_question(screen: str) -> str:
    lines = useful_lines(screen, ASK_TAIL, display=True)
    anchor = None
    for i in range(len(lines) - 1, -1, -1):
        if OPTION_RE.match(lines[i]):
            continue
        low = lines[i].lower()
        if QUESTION_LINE_RE.search(lines[i]) or any(m in low for m in ASK_MARKERS):
            anchor = i
            break
    if anchor is None:
        return ""
    block = [
        l for l in lines[max(0, anchor - 3):anchor + 1] if not OPTION_RE.match(l)
    ]
    return "\n".join(block)[:600]


def extract_tool(screen: str) -> str:
    for line in reversed(useful_lines(screen, SCAN_LINES)):
        m = TOOL_RE.match(line)
        if m:
            return f"{m.group(1)}({m.group(2)}"[:180]
    return ""


def fingerprint(screen: str | None) -> str:
    """Відбиток хвоста для детекції активності. Цифри й спінер-гліфи
    прибрані, щоб тикання таймера не читалось як зміна екрану."""
    if screen is None:
        return "<none>"
    tail = useful_lines(screen, ASK_TAIL)
    return re.sub(r"[\d✳✶✻✽·•*⏺]", "", " ".join(tail))


def analyze(screen: str | None) -> ScreenInfo:
    if screen is None:
        return ScreenInfo(state="unknown")
    detect = useful_lines(screen, SCAN_LINES)
    if not detect:
        return ScreenInfo(state="idle")
    low_all = "\n".join(detect).lower()
    tail = detect[-OPTION_TAIL:]

    opts, cursor, start = _option_block(tail)
    dialog = False
    if opts:
        # діалог живий, лише якщо ПІД ним немає нової роботи/tool-виклику —
        # інакше це залишки вже відповіданого діалогу вище по екрану
        below = tail[start + len(opts):]
        below_low = "\n".join(below).lower()
        stale = any(m in below_low for m in WORKING_MARKERS) or any(
            TOOL_RE.match(l) for l in below
        )
        if not stale:
            ctx = tail[max(0, start - 4):start]
            ctx_low = "\n".join(ctx).lower()
            dialog = (
                cursor
                or any(m in ctx_low for m in ASK_MARKERS)
                or any(QUESTION_LINE_RE.search(l) for l in ctx)
            )

    boxes = 0
    if dialog:
        around = tail[max(0, start - 4):start + len(opts)]
        boxes = sum(len(CHECKBOX_RE.findall(l)) for l in around)
    checkbox_dialog = False
    if not dialog:
        # мультиселект без нумерації: курсор ❯ + чекбокси поруч у хвості
        t10 = tail[-10:]
        if any(CURSOR_RE.match(l) for l in t10) and any(
            CHECKBOX_RE.search(l) for l in t10
        ):
            checkbox_dialog = True

    question = extract_question(screen)
    tool = extract_tool(screen)

    if dialog or checkbox_dialog:
        return ScreenInfo(
            state="waiting",
            options=opts if dialog else [],
            checkboxes=(boxes >= 1) or checkbox_dialog,
            question=question,
            tool=tool,
        )
    if any(m in low_all for m in WORKING_MARKERS):
        return ScreenInfo(state="working", tool=tool)
    # питання вільною формою — лише в самому низу екрана
    qlines = useful_lines(screen, QUESTION_TAIL, display=True)
    if any(QUESTION_LINE_RE.search(l) and not OPTION_RE.match(l) for l in qlines):
        return ScreenInfo(state="waiting", question=question, tool=tool)
    return ScreenInfo(state="idle", tool=tool)
