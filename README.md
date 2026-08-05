# tg-dictaphone

Telegram як голосовий/текстовий пульт до tmux-сесії з агентом (Claude Code)
на твоєму Mac. v2 — повний рефакторинг після аудиту v1
(36 виправлених проблем; список — `TG_DICTAPHONE_ISSUES.md` у нотатках).

## Що вміє

- голосове → Whisper (mlx, локально) → промт на підтвердження → у tmux;
- фоновий нагляд: бот сам напише, коли агент завершив або щось питає —
  навіть якщо задача йшла 20 хвилин (heartbeat «ще працює · N хв»);
- панель стану з кнопками: нумеровані варіанти, чекбокси (↑ ↓ ␣ ⏎),
  Enter/Escape, повний екран;
- `/peek`, `/screen`, `/status`, `/bind`, `/key <клавіші>`.

## Безпекова модель (чому v2, а не v1)

1. **Fail-closed.** Текст іде в термінал лише якщо у прив'язаній панелі
   агент (`AGENT_HINTS`). Якщо там shell/REPL — блок і явний override.
2. **Прив'язана панель.** Бот працює з immutable pane id (`%N`), а не з
   «активною панеллю сесії». Перемкнув вікно на Mac — кнопки не полетять
   не туди. `/bind` — переприв'язка.
3. **Покоління панелей.** Кнопки застарілих панелей (у т.ч. з-перед
   рестарту чи offline-кліки) нічого не надсилають — пропонують свіжу
   панель. `drop_pending_updates=True` додатково ріже чергу.
4. **Bracketed paste.** Текст вставляється через tmux buffer — безпечно
   для `-...`, перенесень рядків, будь-якого юнікоду.
5. **Тільки власник, тільки приватний чат.** У групі бот мовчить.
6. ⚠️ Бот-чати Telegram **не end-to-end шифровані**: вміст екрана
   проходить через сервери Telegram. Не світи секрети в панелі,
   яку показуєш через `/screen`.

## Встановлення

```sh
brew install ffmpeg tmux python@3.13
python3.13 -m venv ~/.venvs/tgbot
~/.venvs/tgbot/bin/pip install -r requirements.txt
cp tgbot.env.example ~/.config/tgbot.env && chmod 600 ~/.config/tgbot.env
# впиши TG_TOKEN і TG_OWNER_ID
```

Запуск:

```sh
~/.venvs/tgbot/bin/python -m tg_dictaphone
```

(з кореня репозиторію, або додай репозиторій у `PYTHONPATH`).

## Автозапуск через launchd (опційно)

`~/Library/LaunchAgents/com.maksym.tg-dictaphone.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.maksym.tg-dictaphone</string>
  <key>ProgramArguments</key><array>
    <string>/Users/maksymshykov/.venvs/tgbot/bin/python</string>
    <string>-m</string><string>tg_dictaphone</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/maksymshykov/Projects/Personal/tg-dictaphone</string>
  <key>EnvironmentVariables</key><dict>
    <!-- launchd не бачить brew PATH — tmux/ffmpeg живуть тут -->
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/tg-dictaphone.log</string>
  <key>StandardErrorPath</key><string>/tmp/tg-dictaphone.log</string>
</dict></plist>
```

```sh
launchctl load ~/Library/LaunchAgents/com.maksym.tg-dictaphone.plist
```

Стартові перевірки (`preflight`) самі скажуть у лог, якщо launchd не
бачить tmux/ffmpeg. Нюанс tmux під launchd: бот має бачити той самий
tmux-сокет, що й твій термінал (за замовчуванням так і є — сокет
у `/private/tmp/tmux-<uid>/`).

## Структура

```
tg_dictaphone/
├── __main__.py    # запуск, реєстрація хендлерів, preflight
├── config.py      # ~/.config/tgbot.env + ENV, валідація
├── tmuxio.py      # async tmux: прив'язка панелі, capture, paste, keys
├── screen.py      # парсер екрану — ЧИСТІ функції, покриті тестами
├── views.py       # панелі + покоління клавіатур
├── watcher.py     # фоновий нагляд за реакцією агента
├── transcribe.py  # ffmpeg + mlx-whisper
└── handlers.py    # телеграм-хендлери, auth, підтвердження
tests/test_screen.py   # фікстури всіх граблів v1
```

## Тести

```sh
~/.venvs/tgbot/bin/python tests/test_screen.py
```

Парсер — найкрихкіша частина (евристики поверх TUI). Петля супроводу
вбудована в бота: якщо панель показала не той стан — прямо з Telegram
надішли `/dump <як мало бути>` (`waiting|working|idle|unknown`), і
знімок екрана ляже в `tests/fixtures/<стан>_<час>.txt`. Тест
`test_fixtures_from_real_screens` жене кожну мічену фікстуру через
парсер і падає, поки `screen.py` не полагоджено. `/dump` без мітки →
`unverified_*` (перевіряється лише, що парсер не падає).

⚠️ Фікстура — це знімок реального термінала. Репозиторій публічний:
перед комітом фікстури перечитай її й прибери внутрішній/чутливий
вміст (структуру екрана — рамки, спінер, промт, статус-бар — зберігай,
саме її перевіряє тест).

## Конфіг

Див. `tgbot.env.example`. Найважливіше:

- `AGENT_HINTS` — кома-розділені підрядки `pane_current_command`, які
  вважаються агентом. За замовчуванням `claude,node`. Якщо Claude Code
  у тебе окремий бінарник (`claude`/`claude.exe`) — звузь до `claude`,
  тоді голий `node` REPL теж буде блокуватись.
- `WATCH_MAX_SECONDS` / `WATCH_HEARTBEAT` — скільки стежити за довгою
  задачею і як часто писати «ще працює».
