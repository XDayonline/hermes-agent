# Telegram inline session picker for /resume and /sessions

**Date:** 2026-06-07
**Branch target:** `feat/sessions-telegram-picker` (from `personal`)
**Goal:** Replace the hardcoded 10-session text list in `/resume` and `/sessions` with a paginated inline-keyboard picker, plus a search button. No more going to the dashboard to find old sessions.

## Problem

Current state (see `cli.py:7003`, `cli.py:7019`, `gateway/run.py:13904`):

```python
self._list_recent_sessions(limit=10)  # hardcoded
self._session_db.list_sessions_rich(source=..., limit=10)  # hardcoded
```

- `/resume` (no args) shows 10 most-recent titled sessions as a plain text list. User replies with a number.
- Sessions older than the 10 most recent require going to the dashboard via the Cloudflare tunnel to find them.

## Solution

Inline keyboard picker, 10 sessions per page, with prev/next + search buttons. Mirrors the existing `/model` picker pattern (`gateway/platforms/telegram.py:2974-3014`).

### UX (Telegram)

```
┌────────────────────────────────────────┐
│  📂 Sessions · page 1/3 · 27 total     │
├────────────────────────────────────────┤
│ [1] Bug fix PayPal webhook             │
│ [2] Refactor auth flow                 │
│ [3] Sync quota/agygravity              │
│ ...                                    │
│ [10] Setup Telegram bot                │
├────────────────────────────────────────┤
│ [« Prev]  [🔍 Search]  [Next »]        │
│ [✗ Close]                              │
└────────────────────────────────────────┘
```

- Tap `[N]` → resumes session N (same effect as `/resume N`)
- Tap `[« Prev]` / `[Next »]` → re-renders with new page
- Tap `[🔍 Search]` → picker enters search mode; user's next message becomes the query (filtered on title + preview); results render as a fresh picker
- Tap `[✗ Close]` → deletes the picker message

### UX (CLI / non-Telegram)

Unchanged. Still shows the plain text list. Pickers are Telegram-only.

## Files to modify

| File | Change |
|------|--------|
| `gateway/platforms/telegram.py` | New: `_build_sessions_keyboard`, `_handle_sessions_picker_callback`, search-mode handling. New state dict `_session_picker_state`. New callback prefixes `sr:` `sg:` `ss:` `sx:`. |
| `gateway/run.py` | Modify `_handle_resume_command` (line 13882): when source is Telegram, return `(text, reply_markup)` tuple instead of plain string. Detect via `source.platform`. |
| `tests/gateway/test_resume_command.py` | Existing tests must still pass (CLI + non-picker path). |
| `tests/gateway/test_telegram_sessions_picker.py` | New: unit tests for the picker builder + callback handler. |

## State shape

```python
# gateway/platforms/telegram.py — class TelegramGateway
self._session_picker_state: dict[str, dict] = {}
# key: chat_id (str)
# value: {
#   "sessions": list[dict],     # full result set (or filtered subset during search)
#   "page": int,                 # current 0-indexed page
#   "page_size": int,            # default 10
#   "query": str | None,         # active search filter
#   "started_at": float,         # epoch seconds, for TTL eviction
# }
```

TTL eviction: drop any entry older than 5 minutes on each new `/resume` invocation. Bounded memory even if the user spams the picker.

## Callback data format

Telegram allows max 64 bytes per `callback_data`. Our format:

- `sr:<index>` — resume session at absolute index in current state (max 7 digits for index → 10 bytes total)
- `sg:<page>` — goto page (max 4 digits → 7 bytes)
- `ss` — start search mode (no args; next user text becomes query)
- `sx` — close picker

State is keyed by chat_id, not encoded in callback_data, so we stay well under 64 bytes.

## Implementation steps

### 1. `gateway/platforms/telegram.py` — picker state + builder

Add to `TelegramGateway.__init__` (or wherever other state dicts live, likely `__init__` near `self._model_picker_state`):

```python
self._session_picker_state: dict[str, dict] = {}
```

Add new method:

```python
_PAGE_SIZE = 10
_PICKER_TTL_SECONDS = 300

def _build_sessions_keyboard(
    self, sessions: list[dict], page: int, query: str | None
) -> tuple[InlineKeyboardMarkup, str]:
    """Build paginated session picker. Returns (markup, header_text)."""
    page_size = self._PAGE_SIZE
    total = len(sessions)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start, end = page * page_size, min((page + 1) * page_size, total)
    page_sessions = sessions[start:end]

    buttons: list[InlineKeyboardButton] = []
    for i, s in enumerate(page_sessions):
        abs_idx = start + i
        title = (s.get("title") or "(untitled)")[:40]
        buttons.append(
            InlineKeyboardButton(f"{abs_idx + 1}. {title}", callback_data=f"sr:{abs_idx}")
        )
    # 1 button per row (titles can be long)
    rows = [[b] for b in buttons]

    # Navigation row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"sg:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="sx:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"sg:{page + 1}"))
    nav.append(InlineKeyboardButton("🔍 Search", callback_data="ss"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("✗ Close", callback_data="sx")])

    page_info = f" · page {page + 1}/{total_pages} · {total} total"
    if query:
        page_info += f' · search: "{query}"'
    return InlineKeyboardMarkup(rows), page_info
```

### 2. `gateway/platforms/telegram.py` — callback handler

Add:

```python
async def _handle_sessions_picker_callback(
    self, query, data: str, chat_id: str
) -> None:
    """Handle session picker callbacks (sr:/sg:/ss:/sx:)."""
    state = self._session_picker_state.get(chat_id)
    if not state:
        await query.answer(text="Picker expired — use /resume again.")
        return

    if data.startswith("sr:"):
        idx = int(data.split(":", 1)[1])
        sessions = state["sessions"]
        if idx < 0 or idx >= len(sessions):
            await query.answer(text="Out of range.")
            return
        target_id = sessions[idx].get("id")
        # Delegate to the gateway's resume pipeline
        await query.answer(text="Switching session…")
        # Reuse the same flow as a /resume <id> command
        # (call the gateway's internal resume method)
        ...

    elif data.startswith("sg:"):
        page = int(data.split(":", 1)[1])
        state["page"] = page
        markup, header = self._build_sessions_keyboard(
            state["sessions"], state["page"], state.get("query")
        )
        await query.edit_message_text(text=header, reply_markup=markup)
        await query.answer()

    elif data == "ss":
        # Enter search mode; next user text becomes the query
        state["awaiting_query"] = True
        await query.answer(text="Send the keyword to search session titles.")
        try:
            await query.edit_message_reply_markup(reply_markup=markup_with_search_hint)
        except Exception:
            pass

    elif data == "sx":
        self._session_picker_state.pop(chat_id, None)
        await query.edit_message_text(text="(picker closed)")
        await query.answer()
```

### 3. `gateway/platforms/telegram.py` — dispatch

In `_handle_callback_query` (line 3232), add alongside the existing `mp:` / `ea:` / `sc:` dispatch:

```python
if data.startswith(("sr:", "sg:", "ss", "sx")):
    chat_id = str(query.message.chat_id) if query.message else None
    if chat_id:
        await self._handle_sessions_picker_callback(query, data, chat_id)
    return
```

### 4. `gateway/run.py` — wire resume command to picker

Modify `_handle_resume_command` (line 13882). Add a code path that detects Telegram and returns the inline keyboard. Signature change: this method currently returns `str`; introduce a new return shape `(text, reply_markup|None)` OR (cleaner) add a sibling method `_handle_resume_command_telegram`.

The cleaner option: keep the current return-`str` method, and add a new wrapper that the command dispatcher calls first when source is Telegram. That way the CLI path is untouched.

```python
# in _handle_resume_command dispatch (around line 8326)
if canonical == "resume":
    if source.platform and source.platform.value == "telegram":
        return await self._handle_resume_command_telegram(event)
    return await self._handle_resume_command(event)
```

`_handle_resume_command_telegram` does the list, calls `_build_sessions_keyboard`, and emits a message with `reply_markup`. The Telegram platform adapter stores the state and exposes a `resume_session_by_id(chat_id, session_id)` method that the picker callback can call (or we just have the callback do the work directly via `self._session_db`).

### 5. Search flow

When the user taps `[🔍 Search]`, set `state["awaiting_query"] = True`. The next user message in that chat gets intercepted:

- If text starts with `/`, treat as a command (don't intercept, clear awaiting_query).
- Otherwise, treat as a search query, filter `state["sessions"]` by title/preview (case-insensitive substring), re-render the picker with `state["query"] = <text>`.

This intercept happens in the Telegram platform's message handler — add a check at the top: if `self._session_picker_state.get(chat_id, {}).get("awaiting_query")`, consume the message and rerender.

### 6. Tests

`tests/gateway/test_telegram_sessions_picker.py`:

- `test_build_sessions_keyboard_basic` — 25 sessions → 3 pages, correct row counts, correct callback_data.
- `test_build_sessions_keyboard_single_page` — 5 sessions → no nav buttons.
- `test_build_sessions_keyboard_with_query` — query in header.
- `test_handle_sessions_picker_callback_sr` — `sr:0` resumes the right session, clears state.
- `test_handle_sessions_picker_callback_sg` — `sg:1` re-renders with page 1.
- `test_handle_sessions_picker_callback_sx` — `sx` clears state and edits message.
- `test_handle_sessions_picker_callback_expired` — state missing → answer "Picker expired".
- `test_ttl_eviction` — state older than 5 min gets dropped on next `/resume`.

## Verification

1. `cd ~/.hermes/hermes-agent && source venv/bin/activate && pytest tests/gateway/test_resume_command.py tests/gateway/test_telegram_sessions_picker.py -v`
2. Restart gateway: `hermes gateway restart`
3. Manual test on Telegram:
   - `/resume` → see inline keyboard
   - Tap a session → confirm switch
   - `/resume` → tap Next » → second page renders
   - Tap Search → send "paypal" → filtered results
   - Tap Close → picker dismissed

## Risks / edge cases

- **Callback data size** (64-byte limit): our format `sr:<idx>` is ≤ 10 bytes. Safe.
- **State leaks**: TTL eviction (5 min) on every `/resume` call. Bounded.
- **Concurrent picks**: state is per-chat; if user A and user B share a chat_id (group chat), they collide. Mitigation: key by `(chat_id, user_id)` for groups. Implement in v1 if groups are in use; otherwise follow up.
- **Search performance**: filtering 100s of sessions in Python is instant. No DB hit.
- **User types free text while picker is showing**: search-mode intercept handles that case. Non-search free text is treated normally (sends to LLM).

## Out of scope (deferred)

- Group chat per-user state (single-user assumption for v1)
- Date-range filtering (e.g. "last week")
- Multi-select (resume multiple at once — not possible, but maybe "merge into current"?)
- Inline WebApp deep-link to the dashboard for power-user queries

## Estimated effort

~1.5–2 hours of focused work, single PR.
