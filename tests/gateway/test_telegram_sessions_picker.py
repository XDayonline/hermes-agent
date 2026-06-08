"""Tests for the Telegram session picker (paginated inline keyboard for /resume).

The picker is the answer to "I have >10 sessions in Telegram and /resume only
shows 10 numbered lines — I want to scroll/page/search inline." It mirrors the
existing model-picker pattern: state on the adapter, ``send_sessions_picker``
to send, ``_handle_sessions_picker_callback`` for button taps, and a free-text
interceptor for the search field.

These tests use a fake ``telegram`` module so we don't pull in the real
``python-telegram-bot`` dependency chain — see ``test_telegram_thread_fallback``
for the same approach.
"""

import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult


# ── Fake telegram module ────────────────────────────────────────────────


class FakeInlineKeyboardButton:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.kwargs = kwargs


class FakeInlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


_fake_telegram = types.ModuleType("telegram")
_fake_telegram.Update = object
_fake_telegram.Bot = object
_fake_telegram.Message = object
_fake_telegram.InlineKeyboardButton = FakeInlineKeyboardButton
_fake_telegram.InlineKeyboardMarkup = FakeInlineKeyboardMarkup
_fake_telegram_constants = types.ModuleType("telegram.constants")
_fake_telegram_constants.ParseMode = SimpleNamespace(
    MARKDOWN_V2="MarkdownV2", MARKDOWN="Markdown", HTML="HTML"
)
_fake_telegram_constants.ChatType = SimpleNamespace(
    GROUP="group", SUPERGROUP="supergroup", CHANNEL="channel", PRIVATE="private"
)
_fake_telegram.constants = _fake_telegram_constants
_fake_telegram_ext = types.ModuleType("telegram.ext")
_fake_telegram_ext.Application = object
_fake_telegram_ext.CommandHandler = object
_fake_telegram_ext.CallbackQueryHandler = object
_fake_telegram_ext.MessageHandler = object
_fake_telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
_fake_telegram_ext.filters = SimpleNamespace(
    TEXT=object(), COMMAND=object(), Regex=object()
)
sys.modules.setdefault("telegram", _fake_telegram)
sys.modules.setdefault("telegram.constants", _fake_telegram_constants)
sys.modules.setdefault("telegram.ext", _fake_telegram_ext)


# ── Adapter fixture ─────────────────────────────────────────────────────


class _FakeBot:
    """Minimal bot stand-in that records what was sent."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return SimpleNamespace(message_id=len(self.sent))


class _FakeQuery:
    def __init__(self, chat_id=42, message_id=1):
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id)
        self.answered: list = []
        self.edited_text: list = []
        self.edited_markup: list = []

    async def answer(self, text=None, **kwargs):
        self.answered.append({"text": text, "kwargs": kwargs})

    async def edit_message_text(self, text, **kwargs):
        self.edited_text.append({"text": text, "kwargs": kwargs})

    async def edit_message_reply_markup(self, **kwargs):
        self.edited_markup.append(kwargs)


@pytest.fixture
def telegram_adapter(monkeypatch):
    """Instantiate the Telegram adapter without running the bot constructor."""
    # Stub out the bot construction — we don't want to talk to Telegram.
    from gateway.platforms import telegram as tg_mod

    # The adapter imported the real python-telegram-bot classes at module
    # load time. Swap them for our fakes at the *attribute* level so the
    # InlineKeyboardButton/Markup constructors below use our recording
    # stubs.
    monkeypatch.setattr(tg_mod, "ParseMode", _fake_telegram_constants.ParseMode)
    monkeypatch.setattr(tg_mod, "InlineKeyboardButton", FakeInlineKeyboardButton)
    monkeypatch.setattr(tg_mod, "InlineKeyboardMarkup", FakeInlineKeyboardMarkup)
    # Build a minimal config so the adapter inits without errors.
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={})
    adapter = tg_mod.TelegramAdapter.__new__(tg_mod.TelegramAdapter)
    adapter.config = cfg
    adapter.platform = Platform.TELEGRAM
    adapter._bot = _FakeBot()
    # Init the state dict that the picker relies on.
    adapter._model_picker_state = {}
    adapter._session_picker_state = {}
    adapter._approval_state = {}
    adapter._slash_confirm_state = {}
    adapter._clarify_state = {}
    adapter._max_doc_bytes = 20 * 1024 * 1024
    adapter._reply_to_mode = "first"
    return adapter


def _make_sessions(n: int) -> list[dict]:
    return [
        {
            "id": f"sess-{i:03d}",
            "title": f"Session {i}",
            "preview": f"preview text {i}",
        }
        for i in range(n)
    ]


# ── _build_sessions_keyboard (pure) ────────────────────────────────────


def test_build_sessions_keyboard_pagination_math(telegram_adapter):
    """8 per page, correct slicing, 6 total pages for 50 sessions."""
    sessions = _make_sessions(50)
    markup, header = telegram_adapter._build_sessions_keyboard(sessions, page=0)

    # 8 buttons on page 1 + 1 nav row + 1 close row = 10 rows
    assert len(markup.inline_keyboard) == 10
    # First 8 buttons reference the first 8 sessions
    for i, row in enumerate(markup.inline_keyboard[:8]):
        assert row[0].callback_data == f"sr:{i}"
    # Nav row: Prev absent, page indicator present, Next present, Search present
    nav_row = markup.inline_keyboard[8]
    cb_data = [b.callback_data for b in nav_row]
    assert "sg:1" in cb_data
    assert "sg:-1" not in cb_data  # no prev on page 0
    assert "ss" in cb_data
    assert "sx" in [b.callback_data for b in markup.inline_keyboard[9]]
    assert "7 ·" in header or "1/7" in header  # 50 / 8 = 6.25 → 7 pages


def test_build_sessions_keyboard_last_page_no_next(telegram_adapter):
    sessions = _make_sessions(10)
    _, _ = telegram_adapter._build_sessions_keyboard(sessions, page=0)
    markup, _ = telegram_adapter._build_sessions_keyboard(sessions, page=1)

    # Page 2 of 10 / 8: 2 sessions, prev present, next absent
    # Layout: [button-0, button-1, nav-row, close-row] → 4 rows total
    assert len(markup.inline_keyboard) == 4
    nav_row = markup.inline_keyboard[2]
    cb_data = [b.callback_data for b in nav_row]
    assert "sg:0" in cb_data  # prev
    assert "sg:2" not in cb_data  # no next


def test_build_sessions_keyboard_clips_long_titles(telegram_adapter):
    """Long titles are truncated to fit Telegram's 64-byte callback budget."""
    sessions = [
        {
            "id": "long",
            "title": "X" * 200,
            "preview": "Y" * 200,
        }
    ]
    markup, _ = telegram_adapter._build_sessions_keyboard(sessions, page=0)
    button = markup.inline_keyboard[0][0]
    # Title truncated to 45 chars + "…"
    assert "…" in button.text
    # Truncated label is well under Telegram's 64-byte callback limit
    # (the callback_data "sr:0" is 4 bytes; button label is the visible text).
    assert button.callback_data == "sr:0"


# ── send_sessions_picker (async) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_send_sessions_picker_stores_state_and_sends(telegram_adapter):
    sessions = _make_sessions(3)
    called = []

    async def on_selected(chat_id, target_id):
        called.append((chat_id, target_id))
        return "resumed"

    result = await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=on_selected,
        metadata=None,
    )
    assert result.success
    assert result.message_id == 1
    # State stored
    assert "42" in telegram_adapter._session_picker_state
    state = telegram_adapter._session_picker_state["42"]
    assert state["sessions"] == sessions
    assert state["page"] == 0
    assert state["awaiting_query"] is False
    assert state["on_session_selected"] is on_selected
    # Bot received exactly one message with markup
    assert len(telegram_adapter._bot.sent) == 1
    sent = telegram_adapter._bot.sent[0]
    assert sent["chat_id"] == 42
    assert "reply_markup" in sent["kwargs"]


@pytest.mark.asyncio
async def test_send_sessions_picker_empty_sessions_returns_error(telegram_adapter):
    """An empty list should fail loudly — the gateway falls back to text."""
    result = await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=[],
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    assert not result.success
    assert "No sessions" in result.error
    assert "42" not in telegram_adapter._session_picker_state


# ── _handle_sessions_picker_callback ──────────────────────────────────


@pytest.mark.asyncio
async def test_callback_sr_resumes_and_clears_state(telegram_adapter):
    sessions = _make_sessions(2)
    selected = []

    async def on_selected(chat_id, target_id):
        selected.append(target_id)
        return f"Resumed {target_id}"

    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=on_selected,
        metadata=None,
    )

    query = _FakeQuery(chat_id=42)
    await telegram_adapter._handle_sessions_picker_callback(query, "sr:1", "42")

    # Selection fired with the right id
    assert selected == ["sess-001"]
    # Picker state cleaned up
    assert "42" not in telegram_adapter._session_picker_state
    # Bot received the confirmation. format_message() escapes MarkdownV2 chars
    # (the dash in "sess-001" becomes "\\-"), so we just check for the word
    # "Resumed" and the escaped session id substring.
    sent_texts = [s["text"] for s in telegram_adapter._bot.sent]
    assert any("Resumed" in t and "sess\\-001" in t for t in sent_texts)
    # query.answer fired with a "Resuming" toast
    assert any("Resuming" in (a.get("text") or "") for a in query.answered)


@pytest.mark.asyncio
async def test_callback_sr_out_of_range_toast(telegram_adapter):
    sessions = _make_sessions(2)
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    query = _FakeQuery(chat_id=42)
    await telegram_adapter._handle_sessions_picker_callback(query, "sr:99", "42")
    # Out of range — picker state must NOT be cleared (user can try again)
    assert "42" in telegram_adapter._session_picker_state
    assert any("Out of range" in (a.get("text") or "") for a in query.answered)


@pytest.mark.asyncio
async def test_callback_sg_paginates(telegram_adapter):
    sessions = _make_sessions(20)
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    query = _FakeQuery(chat_id=42)
    await telegram_adapter._handle_sessions_picker_callback(query, "sg:1", "42")

    # Message edited with new page
    assert len(query.edited_text) == 1
    # The new markup should reference sessions 8-15 (page 2 of 20 / 8)
    markup = query.edited_text[0]["kwargs"]["reply_markup"]
    first_row_cb = markup.inline_keyboard[0][0].callback_data
    assert first_row_cb == "sr:8"
    # State updated to page 1
    assert telegram_adapter._session_picker_state["42"]["page"] == 1


@pytest.mark.asyncio
async def test_callback_ss_enters_search_mode(telegram_adapter):
    sessions = _make_sessions(3)
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    query = _FakeQuery(chat_id=42)
    await telegram_adapter._handle_sessions_picker_callback(query, "ss", "42")

    assert telegram_adapter._session_picker_state["42"]["awaiting_query"] is True
    # Toast says "Send the keyword to filter sessions"
    assert any("keyword" in (a.get("text") or "") for a in query.answered)


@pytest.mark.asyncio
async def test_callback_sx_closes_picker(telegram_adapter):
    sessions = _make_sessions(2)
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    query = _FakeQuery(chat_id=42)
    await telegram_adapter._handle_sessions_picker_callback(query, "sx", "42")

    assert "42" not in telegram_adapter._session_picker_state
    # Close uses edit_message_text with reply_markup=None, not edit_message_reply_markup
    assert len(query.edited_text) == 1
    assert query.edited_text[0]["kwargs"].get("reply_markup") is None


@pytest.mark.asyncio
async def test_callback_without_state_answers_expired(telegram_adapter):
    """Tapping a button after the picker expired should answer gracefully."""
    query = _FakeQuery(chat_id=42)
    await telegram_adapter._handle_sessions_picker_callback(query, "sr:0", "42")
    assert any("expired" in (a.get("text") or "").lower() for a in query.answered)


# ── Search interceptor ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_input_filters_sessions(telegram_adapter):
    sessions = [
        {"id": "a", "title": "Implementing Hermes quotas", "preview": ""},
        {"id": "b", "title": "Cookinc cookie sync", "preview": ""},
        {"id": "c", "title": "Hermes antigravity OAuth", "preview": ""},
    ]
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    # Tap Search, then send a query.
    await telegram_adapter._handle_sessions_picker_callback(
        _FakeQuery(chat_id=42), "ss", "42"
    )
    result = await telegram_adapter.handle_sessions_picker_search_input(
        "42", "hermes"
    )
    # We sent one new message (the filtered results)
    assert result is None  # we sent the message ourselves
    state = telegram_adapter._session_picker_state["42"]
    assert state["query"] == "hermes"
    assert state["awaiting_query"] is False
    assert state["page"] == 0
    # The last sent message is the filtered header
    last = telegram_adapter._bot.sent[-1]
    assert "hermes" in last["text"].lower()


@pytest.mark.asyncio
async def test_search_input_cancelled_on_slash(telegram_adapter):
    """A slash command inside awaiting state should pass through to the
    command handler — not be consumed as a search query."""
    sessions = _make_sessions(2)
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    await telegram_adapter._handle_sessions_picker_callback(
        _FakeQuery(chat_id=42), "ss", "42"
    )
    result = await telegram_adapter.handle_sessions_picker_search_input(
        "42", "/help"
    )
    assert result is None  # passed through
    # State no longer awaiting
    assert telegram_adapter._session_picker_state["42"]["awaiting_query"] is False


@pytest.mark.asyncio
async def test_search_input_no_match_reports_via_message(telegram_adapter):
    sessions = [{"id": "a", "title": "Cookinc sync", "preview": ""}]
    await telegram_adapter.send_sessions_picker(
        chat_id="42",
        sessions=sessions,
        session_key="key",
        on_session_selected=lambda *a: "",
        metadata=None,
    )
    await telegram_adapter._handle_sessions_picker_callback(
        _FakeQuery(chat_id=42), "ss", "42"
    )
    result = await telegram_adapter.handle_sessions_picker_search_input(
        "42", "kubernetes"
    )
    # Returns confirmation string for the caller to log
    assert "no matches" in (result or "").lower()
    # And we sent a message anyway
    assert any("No matches" in s["text"] for s in telegram_adapter._bot.sent)


# ── TTL eviction ───────────────────────────────────────────────────────


def test_ttl_evicts_stale_state(telegram_adapter):
    sessions = _make_sessions(1)
    # Insert with a backdated started_at
    telegram_adapter._session_picker_state["42"] = {
        "sessions": sessions,
        "page": 0,
        "query": None,
        "awaiting_query": False,
        "started_at": time.time() - 600,  # 10 min ago, > 5 min TTL
        "on_session_selected": lambda *a: "",
        "metadata": None,
    }
    telegram_adapter._evict_expired_session_picker("42")
    assert "42" not in telegram_adapter._session_picker_state


def test_ttl_keeps_fresh_state(telegram_adapter):
    telegram_adapter._session_picker_state["42"] = {
        "sessions": _make_sessions(1),
        "page": 0,
        "query": None,
        "awaiting_query": False,
        "started_at": time.time() - 30,  # 30s ago, < 5 min TTL
        "on_session_selected": lambda *a: "",
        "metadata": None,
    }
    telegram_adapter._evict_expired_session_picker("42")
    assert "42" in telegram_adapter._session_picker_state


# ── Gateway: _resume_to_session_id (no Telegram) ──────────────────────


class _FakeSessionDB:
    def __init__(self):
        self.sessions = {
            "s1": {"id": "s1", "title": "Old session"},
            "s2": {"id": "s2", "title": "Another session"},
        }

    def get_session(self, sid):
        return self.sessions.get(sid)

    def get_session_title(self, sid):
        s = self.sessions.get(sid)
        return s.get("title") if s else None

    def resolve_resume_session_id(self, sid):
        # Identity: no compression in the fake.
        return sid


def test_resume_to_session_id_dispatches_call(tmp_path):
    """The shared helper must work without an adapter — used by both
    /resume <id> and the picker callback."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)

    class _FakeSessionStore:
        def __init__(self):
            self.current = "current-id"

        def get_or_create_session(self, source):
            return SimpleNamespace(session_id=self.current)

        def switch_session(self, key, target):
            self.current = target
            return SimpleNamespace(session_id=target)

        def load_transcript(self, sid):
            return [{"role": "user"}, {"role": "assistant"}] * 3

    runner._session_db = _FakeSessionDB()
    runner.session_store = _FakeSessionStore()
    runner._release_running_agent_state = lambda key: None
    runner._clear_session_boundary_security_state = lambda key: None
    runner._evict_cached_agent = lambda key: None

    result = runner._resume_to_session_id(
        session_key="k",
        source=SimpleNamespace(),
        target_id="s1",
        name="Old session",
    )
    assert "Old session" in result
    assert runner.session_store.current == "s1"


def test_resume_to_session_id_already_on(tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)

    class _FakeSessionStore:
        def __init__(self):
            self.current = "s1"

        def get_or_create_session(self, source):
            return SimpleNamespace(session_id=self.current)

        def switch_session(self, key, target):
            return None

        def load_transcript(self, sid):
            return []

    runner._session_db = _FakeSessionDB()
    runner.session_store = _FakeSessionStore()
    runner._release_running_agent_state = lambda key: None
    runner._clear_session_boundary_security_state = lambda key: None
    runner._evict_cached_agent = lambda key: None

    result = runner._resume_to_session_id(
        session_key="k",
        source=SimpleNamespace(),
        target_id="s1",
        name="s1",
    )
    # No message-count branch — confirms via the "already on" message.
    assert "already" in result.lower() or "current" in result.lower()
