"""Тести парсера екрану — фікстури з реальних граблів v1.

Запуск:  python tests/test_screen.py   (або pytest tests/)
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tg_dictaphone import screen as scr  # noqa: E402

PERMISSION_DIALOG = """\
⏺ Bash(rm -rf build/)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again this session
   3. No, and tell Claude what to do differently (esc)
"""

PROSE_NUMBERED_LIST = """\
Ось що я зробив:

1. Виправив баг у парсері
2. Додав тести
3. Оновив документацію

Готово — кажи, якщо треба ще щось.
"""

WORKING_LINE_START = """\
щось робиться далі
esc to interrupt
"""

WORKING_EMBEDDED = """\
✳ Thinking… (esc to interrupt · 12s · ↓ 1.2k tokens)
"""

MARKDOWN_CHECKLIST = """\
План виконано:
- [x] зробив перше
- [ ] друге в черзі
- [ ] третє потім
Все, чекаю на рев'ю.
"""

CHECKBOX_DIALOG = """\
 Which features do you want to enable?
 ❯ ◉ 1. Alpha
   ◯ 2. Beta
   ◯ 3. Gamma
"""

TRAILING_QUESTION_EN = """\
some earlier output here
May I edit the configuration file?
"""

TRAILING_QUESTION_UA = """\
довгий вивід агента
Можу відредагувати конфіг?
"""

QUOTED_MARKER_MID_TEXT = """\
Він спитав "Would you like tea?" і пішов далі робити своє.
Потім повернувся і доробив усе інше.
Кінець виводу.
"""

NON_SEQUENTIAL_NUMBERS = """\
7. пункт сім зі старого списку
9. пункт дев'ять звідти ж
"""

TOOL_LINE = """\
⏺ Read(main.go)
  далі якийсь вивід
останній рядок без питання
"""

STALE_DIALOG_ABOVE_WORK = """\
 Do you want to proceed?
 ❯ 1. Yes
   2. No

⏺ Bash(go test ./...)
✳ Running… (esc to interrupt · 3s)
"""


def test_permission_dialog_is_waiting_with_options():
    info = scr.analyze(PERMISSION_DIALOG)
    assert info.state == "waiting"
    assert [n for n, _ in info.options] == ["1", "2", "3"]
    assert info.tool.startswith("Bash(")
    assert "proceed" in info.question.lower()


def test_prose_numbered_list_is_idle():
    info = scr.analyze(PROSE_NUMBERED_LIST)
    assert info.state == "idle"
    assert info.options == []


def test_working_marker_at_line_start_not_eaten_by_noise_filter():
    assert scr.analyze(WORKING_LINE_START).state == "working"


def test_working_marker_embedded():
    assert scr.analyze(WORKING_EMBEDDED).state == "working"


def test_markdown_checklist_is_not_a_checkbox_dialog():
    info = scr.analyze(MARKDOWN_CHECKLIST)
    assert info.state == "idle"
    assert info.checkboxes is False


def test_checkbox_dialog_detected():
    info = scr.analyze(CHECKBOX_DIALOG)
    assert info.state == "waiting"
    assert info.checkboxes is True
    assert [n for n, _ in info.options] == ["1", "2", "3"]


def test_trailing_question_english():
    info = scr.analyze(TRAILING_QUESTION_EN)
    assert info.state == "waiting"
    assert "configuration" in info.question


def test_trailing_question_ukrainian():
    info = scr.analyze(TRAILING_QUESTION_UA)
    assert info.state == "waiting"
    assert "конфіг" in info.question


def test_quoted_marker_mid_text_is_idle():
    assert scr.analyze(QUOTED_MARKER_MID_TEXT).state == "idle"


def test_non_sequential_numbers_are_not_options():
    info = scr.analyze(NON_SEQUENTIAL_NUMBERS)
    assert info.options == []


def test_tool_extraction_real_claude_format():
    info = scr.analyze(TOOL_LINE)
    assert info.tool.startswith("Read(main.go")


def test_working_wins_over_stale_dialog_above():
    assert scr.analyze(STALE_DIALOG_ABOVE_WORK).state == "working"


def test_capture_failure_is_unknown_not_idle():
    assert scr.analyze(None).state == "unknown"


def test_fingerprint_ignores_timers_and_spinners():
    a = "✳ Running… (esc to interrupt · 12s · 1.2k tokens)"
    b = "✻ Running… (esc to interrupt · 47s · 9.8k tokens)"
    assert scr.fingerprint(a) == scr.fingerprint(b)


def test_useful_lines_blank_lines_do_not_eat_budget():
    screen = "перший\n" + "\n" * 20 + "останній"
    assert scr.useful_lines(screen, 2) == ["перший", "останній"]


def test_fixtures_from_real_screens():
    """Знімки з /dump: ім'я <очікуваний_стан>_<час>.txt.

    Мічені фікстури мають давати саме той стан; unverified_* —
    достатньо, що парсер не падає."""
    fixtures = pathlib.Path(__file__).parent / "fixtures"
    if not fixtures.is_dir():
        return
    for p in sorted(fixtures.glob("*.txt")):
        expected = p.name.split("_", 1)[0]
        info = scr.analyze(p.read_text())
        if expected in ("waiting", "working", "idle", "unknown"):
            assert info.state == expected, f"{p.name}: парсер каже {info.state}"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
    if failed:
        sys.exit(f"\n{failed} тест(и) впали")
    print("\nУсі тести пройшли.")
