# tg_dictaphone.py — Consolidated issue list

Merged from two independent reviews (A = Claude, B = second reviewer), deduplicated,
false positives removed (see Appendix). **36 unique issues: 5×P0, 13×P1, 8×P2, 10×P3.**

Provenance tags: `[A]` / `[B]` / `[A+B]` = found by one or both reviews.
`✓tested` = reviewer B confirmed it with an isolated parser/tmux test.

## Recommended fix order (the 20% that removes 80% of the risk)

1. **Fail closed before sending** — verify the target pane runs the expected agent
   *before* any keystroke leaves the bot (P0-1), and pin an immutable pane ID (P0-2).
2. **Generation tokens on every panel** — a button is valid only for the panel
   generation it was rendered for; re-verify state before injecting keys (P0-3),
   and set `drop_pending_updates=True` (P0-4).
3. **`send-keys -l --` + bracketed paste** for text delivery (P0-5).
4. **`concurrent_updates=True`** (or move tmux/settle work off the handler path)
   so ⛔️ Перервати actually works while waiting (P1-6/7).
5. **Replace the 45 s settle cap with a background completion watcher** that
   notifies when the agent finishes or asks (P1-8).

---

## P0 — Safety: keystrokes can land in the wrong place

### P0-1. Fail-open agent check: a prompt can execute in a shell or REPL `[A+B]`
`send_prompt()` reads `pane_command()` but sends the text + Enter **regardless**;
the "⚠️ не схоже на агента" warning is generated only after delivery. If Claude
exited and the pane is `zsh`, a voice transcript becomes a shell command. Worse,
`AGENT_HINTS = ("claude", "node", "python")` means a bare `python`/`node` REPL
passes as an "agent" with **no warning at all** — and a REPL executes whatever
arrives. The heuristic also misses agents launched via other wrappers.
**Fix:** check before sending; block (or demand a second explicit confirmation)
when the pane isn't the expected process. Make the hint list configurable/strict.

### P0-2. tmux target is not a fixed pane, and names are prefix-matched `[A+B]`
Every call targets `-t SESSION`, which resolves to the session's *currently
active* window/pane — switch windows on the Mac and every keystroke/prompt lands
in the wrong pane. tmux also prefix-matches session names (`-t agents` can hit
`agents2`). **Fix:** resolve once to an immutable pane ID (`%N`), use `=name`
for exact session matching, target the pane ID everywhere.

### P0-3. Stale keyboards stay armed forever; callback routing is too permissive `[A+B]`
- `on_button()` never checks that the clicked message is the current
  `ACTIVE_VIEW` — any old panel (including from before a bot restart, when
  `ACTIVE_VIEW`/`PENDING` are empty) can still send `Enter`, `Escape`, arrows,
  Space, digits to whatever now occupies the pane. `disarm_previous()` strips
  markup only from the immediately previous panel.
- `k:` digit/Enter buttons don't re-capture and re-verify the pane is still
  "waiting" before injecting keys.
- The final `else` branch treats **any unknown action** with a valid pending
  token as approval — only `action == "no"` cancels (`x:token` would send).
- `q.data.split(":", 1)` raises `ValueError` on colonless (stale/foreign)
  callback data, and `q.answer()` is then never called.
**Fix:** per-panel generation token embedded in callback data + strict action
allowlist + state re-check before any key injection.

### P0-4. Offline clicks replay after restart `[B]`
`run_polling()` uses the default `drop_pending_updates=False`; Telegram retains
updates ~24 h. A key button clicked while the bot is down is processed on next
start — against whatever the pane shows then. **Fix:** `drop_pending_updates=True`
(generation tokens from P0-3 also neutralize this).

### P0-5. Text delivery is injectable and non-atomic `[A+B]` `✓tested`
- `send-keys … -l <text>` lacks `--`: text starting with `-` is parsed as tmux
  flags (verified: `-hello` → `unknown flag -h`; `-- -hello` works). `/key` has
  the same option-boundary problem.
- Multiline text is not atomic: embedded newlines are interpreted by the
  terminal before the final Enter — in a shell each line executes as its own
  command; in a TUI it may submit several prompts.
**Fix:** `send-keys -l -- <text>`, or better `load-buffer` + `paste-buffer -p`
(bracketed paste) for the whole prompt.

---

## P1 — Core reliability

### Architecture / responsiveness

### P1-6. The bot blocks itself for up to ~48 s — including the interrupt button `[A+B]`
PTB's default is `concurrent_updates=False`: updates are processed strictly one
at a time. `wait_until_settled()` holds the handler for up to
`SETTLE_FIRST + SETTLE_MAX ≈ 47.5 s`, during which **no other update runs — not
⛔️ Перервати (Escape), not /peek**. Voice transcription serializes the queue the
same way (first call also downloads/loads the Whisper model).
**Fix:** `concurrent_updates(True)` or move settle/transcribe into background
tasks that post results when done.

### P1-7. Blocking calls inside async handlers `[A+B]`
Every `tmux_run()` is a synchronous `subprocess.run` and `send_prompt()` calls
`time.sleep(0.3)` — both freeze the whole event loop (compounds P1-6).
**Fix:** `asyncio.to_thread` / `create_subprocess_exec`, `asyncio.sleep`.

### P1-8. 45 s settle cap with no completion watcher `[A+B]`
Claude Code tasks routinely run minutes. After `SETTLE_MAX` the loop gives up,
pushes a "working" panel and goes silent forever — the user is never notified
when the agent finishes or asks a question (the docstring promises exactly
that). The loop can also end **too early**: first poll at 2.5 s + `calm >= 2`
declares "settled" at ~5–7.5 s if the agent is slow to start rendering, or when
the parser misclassifies a busy pane as "waiting" (P1-10/11).
**Fix:** background watcher polling until a real terminal state (waiting/idle),
with periodic "still working" heartbeats and a notification on state change.

### P1-9. No subprocess timeouts, no startup validation `[B]`
tmux/ffmpeg/Whisper calls have no timeout; `tmux_run()` doesn't handle
`FileNotFoundError`. Nothing validates tmux/ffmpeg/mlx_whisper/model presence at
startup — PATH and tmux socket visibility commonly differ between an interactive
shell and a LaunchAgent. **Fix:** `timeout=` on subprocess calls + a startup
self-check that reports to the log (and to the owner on first /status).

### State detection (the screen parser)

### P1-10. Ordinary numbered lists become live prompts `[A+B]` `✓tested`
`parse_options()` treats any ≥2-item numbered list in the last 34 useful lines
as a selection prompt — agents constantly emit numbered lists in answers. There
is no prompt-block boundary or recency check, so it also merges unrelated lists
and picks up **already-answered dialogs** still inside the window. Consequence
chain: bogus "waiting" state → bogus option buttons → pressing "1" injects a
stray digit into the agent's input.

### P1-11. `waiting` is checked before `working` — precedence inversion `[A+B]`
A stale question/option block above current working output reports "waiting",
which also makes `wait_until_settled()` exit early (waiting counts as calm).
**Fix:** working markers win; scan options only below the last prompt/spinner.

### P1-12. Working-marker detection is self-contradictory and over-broad `[A+B]` `✓tested`
- `NOISE_PREFIXES` drops every line starting with `"esc to"` **before**
  `WORKING_MARKERS` looks for `"esc to interrupt"` — the strongest working
  marker is unreachable in its line-start form (it survives only when embedded
  mid-line, e.g. `✳ Thinking… (esc to interrupt)`).
- `"calling "` matches ordinary prose ("calling the API") → false "working"
  that spins the settle loop to the 45 s deadline.
- A completed answer *quoting* "Would you like…?" / "esc to interrupt" is
  classified as a live question / active work.

### P1-13. Ask detection is too narrow and internally inconsistent `[A+B]` `✓tested`
`has_ask_marker()` knows a handful of English phrases; a plain
`May I edit the configuration file?`, "Should I…", Ukrainian questions, or any
generic trailing `?` yield **idle**. Meanwhile `extract_question()` *does*
anchor on trailing `?` — it can extract a question that `pane_state()` never
classifies as waiting. **Fix:** one shared predicate; add trailing-`?` (in the
tail only) and the common confirmation phrasings.

### P1-14. Checkbox prompts never establish "waiting" `[A+B]` `✓tested`
By (documented) design, `has_checkboxes()` only picks the keyboard — so a real
multi-select prompt without a recognized English phrase shows as "✅ Панель
вільна". The regex also false-positives on markdown task lists (`- [x] done`)
and code anywhere in the 34-line window, requires ≥2 hits (misses a single
checkbox), and counts radio symbols `◉◯` as checkboxes.

### P1-15. `TOOL_RE` never matches real Claude Code output `[A+B]` `✓tested`
The regex expects `Name — verb(` (em-dash) or `mcp__…(`, but real output is
`⏺ Bash(ls …)` / `●`-bulleted lines — and `_clean()` doesn't strip those
bullets, so the `^`-anchored match fails. `Read(file.py)` also doesn't match.
Dead heuristic: the 🔧 line in the panel effectively never renders.

### P1-16. Capture failure is reported as "all clear" `[A+B]`
`capture()` turns every failure into `""` → `pane_state("")` → "idle" → the
panel says "✅ Панель вільна". Unknown/error state must be distinguishable from
a genuinely free pane.

### P1-17. Capture-window quality: wrapped lines and blank-line budget `[A+B]`
`capture-pane` is called without `-J`, so wrapped lines are split — questions
and option rows can be cut and missed. `_useful()` slices *raw* lines before
filtering, so blanks/borders eat the 14-line `ASK_TAIL` budget and the actual
question can fall outside the window.

### P1-18. Option coverage gaps `[A+B]`
Only single digits `1–9`; ≥2 entries required (single-option confirmations are
missed — a deliberate anti-false-positive tradeoff, but still a gap); only the
first 5 become buttons with no "…more" hint; duplicate numbers silently
overwrite each other; labels truncate to 34 chars and can become
indistinguishable. The digit button also assumes the TUI auto-submits on a
digit — true for Claude Code menus, not for arbitrary agents.

---

## P2 — Robustness / delivery

### P2-19. Edited messages crash the text/voice handlers `[A]`
`MessageHandler` also fires for `edited_message` updates (that's what
`filters.UpdateType` exists for); there `u.message` is `None` →
`u.message.text` raises `AttributeError`. **Fix:** `u.effective_message` or
`& filters.UpdateType.MESSAGES`.

### P2-20. Silent failures: download path and the error handler `[A+B]`
`get_file()` / `download_to_drive()` sit outside the `try` — Bot API's 20 MB
`getFile` cap or a network error propagates to `on_error`, which only logs.
Globally, `on_error` never notifies the user, so any unexpected failure looks
like the bot ignored you (and never says whether tmux was already modified).

### P2-21. Two-step send is not transactional, and neither is the feedback `[B]`
Text and Enter are separate `send-keys` calls: if the first succeeds and the
second fails, the bot reports "НЕ надіслано" while the text sits in the input —
retrying duplicates it. Symmetrically, a Telegram edit/network failure *after*
successful delivery leaves the user unsure whether execution started.

### P2-22. `PENDING` lifecycle `[A+B]`
No TTL, size cap, or message binding: stale ▶️ buttons can resend an old prompt
hours later; a used/expired token reports a misleading "Скасовано." (including
after a double-click race where the prompt *was* sent); if the `confirm()`
reply itself fails, the token is orphaned. **Fix:** TTL + bind token to
message_id + distinct "already handled / expired" replies.

### P2-23. No message-length guards for genuinely long content `[A+B]`
A 4096-char incoming prompt (Telegram's max for user messages) is confirmed,
sent to tmux, then the post-send edit adds a prefix and exceeds the limit →
`BadRequest` → silent (P2-20) while the prompt *was* delivered. Voice
transcripts are unbounded and can fail already at the `confirm()` echo.
**Fix:** truncate echoes; keep panels within budget.
*(Note: HTML-escaping does **not** count against the limit — see Appendix.)*

### P2-24. Documented env file is never loaded `[A+B]`
The docstring points at `~/.config/tgbot.env`, but nothing sources it (no
dotenv). `os.environ["TG_TOKEN"]` KeyErrors unless a wrapper exports the vars.

### P2-25. No input validation before the terminal `[B]`
Whitespace-only prompts, absurdly long prompts, and multiline prompts are all
accepted verbatim. Strip, reject empty, cap length (or chunk deliberately).

### P2-26. Audio pipeline gaps `[A+B]`
No duration/size/MIME guard (bounded only by the 20 MB `getFile` cap); a long
file monopolizes CPU/model time (and the queue, per P1-6). ffmpeg's stderr is
captured but discarded — the chat gets a generic exception string instead of
the actual diagnostic, while raw exception text (with local paths) *is* leaked
to the chat. First-use model download happens silently mid-request.

---

## P3 — UX / cosmetic / informational

### P3-27. Checkbox nav and refresh collapse the full-screen view `[B]`
`Space/Up/Down` and `k:__r__` always call `edit_view(q)` with `full=False`,
so a panel opened as "📄 Повний екран" unexpectedly collapses on interaction.

### P3-28. "Повний екран" is a misnomer `[B]`
It's a filtered last-60-useful-lines, last-3000-chars slice, not the screen.

### P3-29. HTML entities shown literally `[A+B]`
`html.escape(err)` is sent **without** `parse_mode` in `on_key` and the `k:`
error path — the user sees `&lt;`-style entities.

### P3-30. Callback answering and chat scoping `[A+B]`
Rejected (non-owner) and malformed callbacks are never `q.answer()`ed — the
Telegram client spins. Authorization checks the *user* only, not the chat: if
the owner uses the bot in a group, terminal content and prompt buttons are
visible (and pressable-looking) to every member. **Fix:** answer everything;
restrict to the owner's private chat.

### P3-31. Privacy: bot chats are not end-to-end encrypted `[B]`
Terminal screens (which may contain code, tokens, customer data) transit and
persist on Telegram's servers. Accept consciously; consider redaction.

### P3-32. The "waiting" panel has no refresh button `[A]`
Only reachable via the full-screen toggle round-trip.

### P3-33. Typing indicator expires `[A+B]`
`ChatAction.TYPING` lasts ~5 s; long transcription/settle shows nothing after.

### P3-34. `/key` always runs the full settle wait `[A]`
Even `/key Up` costs 5+ s before the panel returns.

### P3-35. Doc/code drift `[A]`
Header says question markers are searched in "12 рядків"; `ASK_TAIL = 14`.

### P3-36. Small correctness nits `[A]`
`SESSION` interpolated into HTML unescaped; `time.time()` instead of
`time.monotonic()` for deadlines; `q.message` can be `InaccessibleMessage`
(>48 h old buttons) → `edit_message_text` fails; audio sent *as a document*
isn't matched by `filters.VOICE | filters.AUDIO`; prompts/transcripts are
partially written to logs `[B]` (local privacy consideration).

### Meta: no tests for a heuristic-heavy parser `[B]`
No fixtures of real captured Claude screens, no parser regression tests. For
logic this fragile, a small corpus of recorded screens + unit tests over
`pane_state`/`parse_options`/`extract_*` is the highest-leverage quality
investment.

---

## Appendix — dropped claims and why

1. **"HTML escaping can expand a 3000-char screen to well over 4096"** (B #30,
   first half) — *false positive.* Telegram's 4096 limit counts characters
   **after entity parsing**, so `&lt;` counts as 1. The raw payload grows, but
   the API accepts it. The genuine long-content cases are kept as P2-23.
2. **"Key side effects happen before `q.answer()`"** (B #8) — *misframed.*
   Answering a callback is UX acknowledgement only; Telegram never "rejects"
   an action via the answer, so reordering gates nothing. The real defect is
   the missing generation/state check — covered by P0-3.
3. **"32-bit tokens → collisions"** (B #38) — *overstated.* At realistic
   `PENDING` sizes collision probability is negligible; binding tokens to a
   message + TTL (P2-22) removes the concern entirely. Kept only as a nit.

## Closing architectural note (both reviews converge here)

Bind the bot to an **immutable pane ID**; **fail closed** unless that exact
pane runs the expected agent; give every panel/confirmation a **one-use
generation token**; derive state from a **correlated post-send screen change**
(diff against the pre-send capture, scoped below the prompt) rather than
pattern-matching arbitrary scrollback; and run settle-watching **in the
background** so the interrupt path always stays responsive.
