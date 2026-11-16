#!/usr/bin/env python3

import os
import re
import html
import json
import time
import asyncio
import calendar
import logging
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import feedparser
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def parse_ids(raw: str) -> frozenset[int]:
    """Comma separated numeric Telegram ids; anything else is skipped."""
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


def parse_accounts(raw: str) -> list[str]:
    """Comma separated X handles, with or without "@". Order kept, duplicates dropped."""
    out: list[str] = []
    for part in (raw or "").split(","):
        u = part.strip().lstrip("@").lower()
        if u and re.fullmatch(r"\w{1,50}", u) and u not in out:
            out.append(u)
    return out


# Default deny: only these Telegram user ids may use the bot's commands and buttons.
# Empty means the bot answers nobody.
ALLOWED_USER_IDS = parse_ids(os.getenv("ALLOWED_USER_IDS", ""))

# Optional watchlist from the environment: WATCHED_ACCOUNTS is merged into the
# watchlist of DIGEST_CHAT_ID at start. Accounts added later with commands are kept.
WATCHED_ACCOUNTS = parse_accounts(os.getenv("WATCHED_ACCOUNTS", ""))
_digest = os.getenv("DIGEST_CHAT_ID", "").strip()
DIGEST_CHAT_ID = _digest if _digest.lstrip("-").isdigit() else ""


def is_allowed(user_id, allowed: frozenset[int] | None = None) -> bool:
    ids = ALLOWED_USER_IDS if allowed is None else allowed
    return isinstance(user_id, int) and user_id in ids

# Feed sources, tried in order. (family, URL template).
# "fx" = FxTwitter/FixupX (same backend, full-text post body in <description>).
# "nitter" = public Nitter forks that still serve RSS (as of late 2026).
# Public instances come and go; edit this list when one dies.
FEED_SOURCES = [
    ("fx", "https://fxtwitter.com/{user}/feed.xml?count=20"),
    ("fx", "https://fixupx.com/{user}/feed.xml?count=20"),
    ("nitter", "https://nitter.meowing.monster/{user}/rss"),
    ("nitter", "https://shitter.thepixora.com/{user}/rss"),
]
# nitter.meowing.monster serves an anti-bot page to any User-Agent containing "Mozilla"
FEED_USER_AGENT = "RSSBot/1.0"

# Hosts whose profile URLs parse_username accepts (Nitter sources + retired mirrors
# that users may still paste). fxtwitter.com / fixupx.com are deliberately NOT here:
# people share post links from them in groups, and a URL auto-adds without a prompt.
KNOWN_MIRRORS = [
    "https://nitter.meowing.monster",
    "https://shitter.thepixora.com",
    "https://xcancel.com",
    "https://nitter.space",
    "https://lightbrd.com",
    "https://nuku.trabun.org",
    "https://nitter.net",
    "https://nitter.privacyredirect.com",
    "https://nitter.kareem.one",
    "https://nitter.poast.org",
    "https://nitter.catsarch.com",
    "https://nitter.tiekoetter.com",
]

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "seen_posts.json"
ROUTES_FILE = DATA_DIR / "routes.json"
TELEGRAPH_TOKEN_FILE = DATA_DIR / "telegraph_token.json"
CHECK_INTERVAL = 15 * 60  # 15 minutes
MAX_SEEN_PER_KEY = 200

# Anti-flood guards (see _hold_reason). Reposts are exempt from the
# age guard: their date is the ORIGINAL post's date, so a fresh repost of an old post
# must still be delivered.
MAX_OWN_POST_AGE = 48 * 60 * 60   # never deliver the account's own post if older than this
_last_family: dict[str, str] = {}     # username -> feed family used on the previous fetch
_pending_family: dict[str, str] = {}  # username -> other family seen once, not yet switched to

# One check at a time: the scheduled job, /check and /recent all read-modify-write
# seen_posts.json across awaits. Overlap re-sends posts.
_state_lock = asyncio.Lock()

# Starvation alarm: if EVERY fetch fails for this many consecutive cycles, DM the
# admin instead of failing silently. Empty ADMIN_CHAT_ID turns the alarm off.
_admin = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = int(_admin) if _admin.lstrip("-").isdigit() else None
STARVATION_CYCLES = 4                # 4 cycles x 15 min = 1 hour of total failure
STARVATION_COOLDOWN = 24 * 60 * 60   # at most one alert per day

_starved_cycles = 0
_last_starvation_alert = 0.0
MAX_ACCOUNTS_PER_CHAT = 50
FETCH_CONCURRENCY = 8         # max parallel feed requests (thepixora 429s above this)
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
KEEPALIVE_INTERVAL = 10 * 60  # 10 min, under the idle timeout of free hosting tiers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and Telegram API URLs contain the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read {path}: {e}. Starting fresh.")
    return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_config() -> dict:
    return load_json(CONFIG_FILE)


def seed_config(config: dict, chat_id: str, accounts: list[str]) -> bool:
    """Merge accounts from the environment into one chat's watchlist. True if changed."""
    if not chat_id or not accounts:
        return False
    current = config.setdefault(chat_id, [])
    added = [a for a in accounts if a not in current]
    current.extend(added)
    return bool(added)


def save_config(config: dict) -> None:
    save_json(CONFIG_FILE, config)


def load_state() -> dict:
    return load_json(STATE_FILE)


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def load_routes() -> dict:
    """Optional per-chat delivery override: {managing_chat_id: target_chat_id}.

    A chat (typically a discussion group) can route its watchlist's new-post
    deliveries to another chat (typically the linked channel). Watchlist, state,
    and first-run detection stay keyed on the managing chat_id; only the send
    destination changes. Chats with no route deliver to themselves (unchanged).
    """
    return load_json(ROUTES_FILE)


def save_routes(routes: dict) -> None:
    save_json(ROUTES_FILE, routes)


# ---------------------------------------------------------------------------
# Feed helpers
# ---------------------------------------------------------------------------

def extract_post_id(entry: dict) -> str:
    link = entry.get("link", "") or entry.get("id", "")
    m = re.search(r"/status/(\d+)", link)
    if m:
        return m.group(1)
    return entry.get("id") or entry.get("guid") or link


def to_twitter_url(nitter_url: str) -> str:
    return re.sub(r"https?://[^/]+/", "https://x.com/", nitter_url)


_RESERVED_PATHS = {
    "i", "home", "search", "explore", "notifications", "messages",
    "settings", "bookmarks", "compose", "intent", "share", "hashtag",
    "about", "legal", "privacy", "following", "followers", "status",
}

_USERNAME_RE = re.compile(r"^\w{1,50}$")


def parse_username(text: str) -> str | None:
    """Extract a Twitter username from a URL, @mention, or bare word. Returns None if not found."""
    text = text.strip()

    # x.com / twitter.com URL (highest confidence)
    m = re.search(r"https?://(?:twitter\.com|x\.com)/(\w{1,50})", text, re.IGNORECASE)
    if m:
        u = m.group(1).lower()
        return u if u not in _RESERVED_PATHS else None

    # Any known mirror URL (FxTwitter, Nitter)
    for instance in KNOWN_MIRRORS:
        host = instance.replace("https://", "")
        if host in text:
            m = re.search(rf"https?://{re.escape(host)}/(\w{{1,50}})", text, re.IGNORECASE)
            if m:
                u = m.group(1).lower()
                return u if u not in _RESERVED_PATHS else None

    # @mention
    if text.startswith("@") and _USERNAME_RE.match(text[1:]):
        return text[1:].lower()

    # Bare username (single word, no spaces)
    if _USERNAME_RE.match(text) and text.lower() not in _RESERVED_PATHS:
        return text.lower()

    return None


def time_ago(entry: dict) -> str:
    t = entry.get("published_parsed")
    if not t:
        return entry.get("published", "Unknown time")
    post_dt = datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - post_dt).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = seconds // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def format_message(username: str, entry: dict) -> str:
    text = entry.get("title", "").strip()
    link = to_twitter_url(entry.get("link", ""))
    return (
        f"👤 @{username}\n"
        f"📝 {text}\n"
        f"🔗 {link}\n"
        f"⏰ {time_ago(entry)}"
    )


# ---------------------------------------------------------------------------
# Telegraph helpers
# ---------------------------------------------------------------------------

def get_telegraph_token() -> str:
    data = load_json(TELEGRAPH_TOKEN_FILE)
    if data.get("access_token"):
        return data["access_token"]
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://api.telegra.ph/createAccount",
                json={"short_name": "XMonBot", "author_name": "X Monitor Bot"},
            )
        result = resp.json()
        if result.get("ok"):
            token = result["result"]["access_token"]
            save_json(TELEGRAPH_TOKEN_FILE, {"access_token": token})
            logger.info("Created new Telegraph account")
            return token
        logger.error(f"Telegraph createAccount failed: {result}")
    except Exception as e:
        logger.error(f"Could not create Telegraph account: {e}")
    return ""


def build_telegraph_nodes(posts: list[tuple[str, dict]]) -> list:
    nodes = []
    for username, entry in posts:
        title_text = entry.get("title", "").strip()
        text = title_text  # Full text, no truncation
        link = to_twitter_url(entry.get("link", ""))
        nodes += [
            {"tag": "p", "children": [
                {"tag": "a", "attrs": {"href": f"https://x.com/{username}"}, "children": [f"@{username}"]},
                f"  ·  {time_ago(entry)}",
            ]},
            {"tag": "p", "children": [text]},
            {"tag": "p", "children": [
                {"tag": "a", "attrs": {"href": link}, "children": ["View on X →"]},
            ]},
            {"tag": "hr"},
        ]
    return nodes


def publish_to_telegraph(title: str, posts: list[tuple[str, dict]]) -> list[str]:
    """Create Telegraph pages, splitting only when needed. Returns list of URLs."""
    token = get_telegraph_token()
    if not token:
        return []

    def try_publish(posts_chunk: list, chunk_title: str) -> list[str]:
        """Try to publish chunk. If too big, split and recurse."""
        if not posts_chunk:
            return []

        nodes = build_telegraph_nodes(posts_chunk)
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    "https://api.telegra.ph/createPage",
                    data={
                        "access_token": token,
                        "title": chunk_title,
                        "content": json.dumps(nodes),
                        "return_content": "false",
                    },
                )
            result = resp.json()
            if result.get("ok"):
                return [result["result"]["url"]]

            # If too large, split in half and try again
            if result.get("error") == "CONTENT_TOO_BIG":
                mid = len(posts_chunk) // 2
                if mid == 0:
                    # Single post too large - shouldn't happen, but log it
                    logger.error(f"Single post too large: @{posts_chunk[0][0]}")
                    return []
                logger.info(f"Splitting {len(posts_chunk)} posts at midpoint {mid}")
                left = try_publish(posts_chunk[:mid], chunk_title + " (1)")
                right = try_publish(posts_chunk[mid:], chunk_title + " (2)")
                return left + right

            logger.error(f"Telegraph createPage failed: {result}")
            return []
        except Exception as e:
            logger.error(f"Telegraph error: {e}")
            return []

    return try_publish(posts, title)


_TG_LIMIT = 4096


async def _send_chunk(bot, chat_id: int, text: str) -> None:
    """Send a single chunk with retry on rate limit."""
    for attempt in range(3):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )
            return
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"Rate limited, waiting {wait}s...")
            await asyncio.sleep(wait)
    logger.error(f"Gave up sending to chat {chat_id} after 3 retries")


async def send_message(bot, chat_id: int, text: str) -> None:
    """Send a message, splitting into multiple messages if it exceeds Telegram's limit."""
    if len(text) <= _TG_LIMIT:
        await _send_chunk(bot, chat_id, text)
        return
    # Split at the last newline before the limit so words aren't cut mid-line
    split_at = text[:_TG_LIMIT].rfind("\n")
    if split_at <= 0:
        split_at = _TG_LIMIT
    await _send_chunk(bot, chat_id, text[:split_at])
    await asyncio.sleep(0.3)
    await send_message(bot, chat_id, text[split_at:].lstrip("\n"))


_NITTER_ERRORS = [
    "not yet whitelisted",
    "instance is not healthy",
    "too many requests",
    "rate limit",
    "blocked",
]


def _is_error_feed(entries) -> tuple[bool, str]:
    """Return (is_error, reason) for the first entry in a feed."""
    if not entries:
        return False, ""
    first = dict(entries[0])
    title = first.get("title", "").lower()
    link = first.get("link", "")
    if any(err in title for err in _NITTER_ERRORS):
        return True, first.get("title", "unknown error")
    # Error entries often link back to the RSS URL itself
    if link.endswith("/rss"):
        return True, f"entry links to RSS URL: {link}"
    return False, ""


_NITTER_REPOST_RE = re.compile(r"^RT (?:by )?@\w+:")
# FxTwitter wraps each photo/video in <a href="https://{pbs,video}.twimg.com/...">,
# videos with the anchor text "Media"
_FX_MEDIA_LINK_RE = re.compile(
    r'<a [^>]*href="https?://(?:video|pbs)\.twimg\.com[^"]*"[^>]*>.*?</a>', re.S | re.I
)


def _html_to_text(fragment: str) -> str:
    """Flatten an FxTwitter <description> to one line, like a Nitter <title>.

    Drops the quoted-post <blockquote> (Nitter titles leave it out too) and media
    links, which carry no post text.
    """
    s = re.sub(r"<blockquote.*?</blockquote>", " ", fragment, flags=re.S | re.I)
    s = _FX_MEDIA_LINK_RE.sub(" ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _fx_text(entry) -> str:
    """Full post text; media-only posts read "Video" / "Image" like Nitter titles."""
    text = _html_to_text(entry.get("summary", ""))
    if text:
        return text
    kinds = [enc.get("type", "") for enc in entry.get("enclosures", [])]
    if any(k.startswith("video/") for k in kinds):
        return "Video"
    if kinds:
        return "Image"
    return entry.get("title", "").strip()


def _normalize_entries(username: str, family: str, entries: list) -> list:
    """Make entries from every source look like Nitter entries to the rest of the bot.

    Sets `title` to the full post text (FxTwitter truncates its <title> at ~140 chars),
    and tags each entry with `_family`, `_repost` and `_pinned` for the anti-flood guards.
    """
    for e in entries:
        e["_family"] = family
        if family == "fx":
            m = re.search(r"x\.com/(\w+)/status/", e.get("link", ""))
            is_repost = bool(m) and m.group(1).lower() != username.lower()
            text = _fx_text(e)
            e["title"] = f"RT by @{username}: {text}" if is_repost else text
            e["_repost"] = is_repost
            e["_pinned"] = False
        else:
            title = e.get("title", "")
            e["_repost"] = bool(_NITTER_REPOST_RE.match(title))
            e["_pinned"] = title.startswith("Pinned:")
    return entries


def _nitter_pinned(username: str, known_ids: set) -> list:
    """FxTwitter feeds leave out the pinned post, so a post pinned as soon as it is
    published would never be seen. Nitter lists it as "Pinned: ...": fetch that one
    entry and serve it alongside the fx feed. Failure is silent (returns [])."""
    for family, template in FEED_SOURCES:
        if family != "nitter":
            continue
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                r = client.get(template.format(user=username), headers={"User-Agent": FEED_USER_AGENT})
            if r.status_code != 200:
                continue
            entries = feedparser.parse(r.text).entries
            if not entries or _is_error_feed(entries)[0]:
                continue
            for e in _normalize_entries(username, "nitter", entries):
                # the Pinned item is not always first (seen at index 2 and 11)
                if e["_pinned"]:
                    if extract_post_id(dict(e)) in known_ids:
                        return []
                    e["_family"] = "fx"    # served with the fx feed: not a source switch
                    e["_pinned"] = False   # a real new post; guards 2 and 3 still apply
                    return [e]
            return []  # feed fetched, no pinned post
        except Exception as ex:
            logger.warning(f"pinned probe for @{username}: {ex}")
    return []


def fetch_feed(username: str, sources: list | None = None) -> list | None:
    for family, template in (sources or FEED_SOURCES):
        url = template.format(user=username)
        host = url.split("/")[2]
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                response = client.get(url, headers={"User-Agent": FEED_USER_AGENT})
            if response.status_code != 200:
                logger.warning(f"{host}: HTTP {response.status_code} for @{username}")
                continue
            feed = feedparser.parse(response.text)
            if not feed.entries:
                logger.warning(f"{host}: empty feed for @{username}")
                continue
            # Nitter signals trouble with placeholder entries; FxTwitter uses HTTP status
            # codes, and a real post that mentions "blocked" must not be read as an error
            if family == "nitter":
                is_err, reason = _is_error_feed(feed.entries)
                if is_err:
                    logger.warning(f"{host}: error feed for @{username}: {reason}")
                    continue
            logger.info(f"Fetched {len(feed.entries)} entries for @{username} from {host}")
            entries = _normalize_entries(username, family, feed.entries)
            if family == "fx":
                entries += _nitter_pinned(username, {extract_post_id(dict(e)) for e in entries})
            return entries
        except Exception as e:
            logger.warning(f"{host}: error for @{username}: {e}")

    logger.error(f"All feed sources failed for @{username}")
    return None


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------

# Optional contact shown in /start, /help and /support, for example a Telegram handle.
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "").strip()
_support_line = f"\n\n🆘 Support: {SUPPORT_CONTACT}" if SUPPORT_CONTACT else ""

WELCOME_TEXT = (
    "👋 Welcome to X Monitor Bot!\n\n"
    "I watch Twitter/X accounts and send new posts here every 15 minutes.\n\n"
    "➕ To add an account, just send me any of these:\n"
    "  example_account\n"
    "  @example_account\n"
    "  https://x.com/example_account\n\n"
    "I'll ask you to confirm before adding.\n"
    "On first add, I stay silent, no flood. Only new posts from that point on.\n\n"
    "📋 To manage accounts: /list\n"
    "Tap ❌ next to any account to remove it."
    + _support_line
)

HELP_TEXT = (
    "📡 X Monitor Bot: Commands\n\n"
    "➕ Add an account, send any of these:\n"
    "  example_account  ·  @example_account  ·  https://x.com/example_account\n\n"
    "📋 /list: Show accounts (tap ❌ to remove)\n"
    "🔴 /recent: Latest posts right now (Telegraph page)\n"
    "⚡ /check: Force a check immediately\n"
    "📣 /postto: Route new posts to a channel (admin)\n"
    "🆘 /support: Contact support\n\n"
    f"⏱ Checks every 15 min · Max {MAX_ACCOUNTS_PER_CHAT} accounts per chat"
    + _support_line
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    config = load_config()
    if chat_id not in config:
        config[chat_id] = []
        save_config(config)
    await update.message.reply_text(WELCOME_TEXT)


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if SUPPORT_CONTACT:
        await update.message.reply_text(f"🆘 Need help or found a bug?\nContact {SUPPORT_CONTACT}.")
    else:
        await update.message.reply_text("🆘 No support contact is set for this bot.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback text-based remove for power users; /list with ❌ buttons is preferred."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /remove username\n\nOr use /list and tap ❌ next to any account."
        )
        return
    username = context.args[0].lstrip("@").lower()
    chat_id = str(update.effective_chat.id)
    config = load_config()
    accounts: list = config.get(chat_id, [])
    if username not in accounts:
        await update.message.reply_text(f"@{username} is not in this chat's watchlist.")
        return
    accounts.remove(username)
    config[chat_id] = accounts
    save_config(config)
    await update.message.reply_text(f"🗑 Removed @{username}.")
    logger.info(f"Chat {chat_id}: removed @{username} via command")


async def _do_add(update: Update, username: str) -> None:
    """Shared logic for adding an account from any source."""
    chat_id = str(update.effective_chat.id)
    config = load_config()
    accounts: list = config.get(chat_id, [])

    if username in accounts:
        await update.message.reply_text(f"@{username} is already in the watchlist.")
        return
    if len(accounts) >= MAX_ACCOUNTS_PER_CHAT:
        await update.message.reply_text(
            f"⚠️ This chat has reached the limit of {MAX_ACCOUNTS_PER_CHAT} accounts.\n"
            f"Remove one first with /remove username."
        )
        return

    accounts.append(username)
    config[chat_id] = accounts
    save_config(config)
    await update.message.reply_text(
        f"✅ Now monitoring @{username}\n"
        f"New posts will arrive within 15 min, or use /check now."
    )
    logger.info(f"Chat {chat_id}: added @{username}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Send me a username, @mention, or x.com link and I'll add it.\n"
            "Examples:\n"
            "  example_account\n"
            "  @example_account\n"
            "  https://x.com/example_account"
        )
        return

    raw = " ".join(context.args)
    username = parse_username(raw)
    if not username:
        await update.message.reply_text("⚠️ Couldn't recognise a Twitter username in that.")
        return
    await _do_add(update, username)




def _list_keyboard(accounts: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(f"@{a}", url=f"https://x.com/{a}"),
            InlineKeyboardButton("❌", callback_data=f"rm:{a}"),
        ]
        for a in accounts
    ]
    return InlineKeyboardMarkup(rows)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    config = load_config()
    accounts: list = config.get(chat_id, [])

    if not accounts:
        await update.message.reply_text(
            "No accounts monitored yet.\n"
            "Send a username, @mention, or x.com link to add one."
        )
        return

    await update.message.reply_text(
        f"Watching {len(accounts)} account(s), tap ❌ to remove:",
        reply_markup=_list_keyboard(accounts),
    )


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish the 5 latest posts per account to a Telegraph page, send one link."""
    chat_id = str(update.effective_chat.id)
    config = load_config()
    accounts: list = config.get(chat_id, [])

    if not accounts:
        await update.message.reply_text("No accounts monitored. Use /add username first.")
        return

    await update.message.reply_text(
        f"⏳ Fetching latest posts from {len(accounts)} account(s)..."
    )

    all_posts: list[tuple[str, dict]] = []
    failed: list[str] = []

    feed_cache = await fetch_feeds_concurrent(accounts)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # Under the lock and after the fetch: the check cycle must not interleave with
    # this read-modify-write of seen_posts.json
    async with _state_lock:
        state = load_state()
        for username in accounts:
            entries = feed_cache.get(username)
            if not entries:
                failed.append(username)
                continue
            # Only posts from the last 24 hours
            for entry in entries:
                e = dict(entry)
                t = e.get("published_parsed")
                if t:
                    post_dt = datetime(t.tm_year, t.tm_mon, t.tm_mday,
                                       t.tm_hour, t.tm_min, t.tm_sec, tzinfo=timezone.utc)
                    if post_dt < cutoff:
                        continue  # older than 24h, skip
                all_posts.append((username, e))
            # Mark all fetched posts as seen so the 15-min cycle won't re-send them
            state_key = f"{chat_id}:{username}"
            current_ids = [extract_post_id(dict(e)) for e in entries]
            existing_ordered = state.get(state_key, [])
            existing_set = set(existing_ordered)
            merged = existing_ordered + [eid for eid in current_ids if eid not in existing_set]
            state[state_key] = merged[-MAX_SEEN_PER_KEY:]

        save_state(state)

    # Sort ALL posts globally by time, newest first (across all accounts)
    all_posts.sort(
        key=lambda x: x[1].get("published_parsed", (1970, 1, 1, 0, 0, 0)),
        reverse=True,
    )

    if not all_posts:
        if failed:
            msg = f"⚠️ Could not fetch: {', '.join('@' + u for u in failed)}"
        else:
            msg = "😶 No posts from any account in the last 24 hours."
        await update.message.reply_text(msg)
        return

    title = f"Last 24h · {datetime.now(timezone.utc).strftime('%b %d, %Y')}"
    urls = publish_to_telegraph(title, all_posts)

    if urls:
        msg = f"📰 {len(all_posts)} post(s) from {len(accounts) - len(failed)} account(s)\n"
        for i, url in enumerate(urls, 1):
            if len(urls) > 1:
                msg += f"\n📄 Part {i}: {url}"
            else:
                msg += f"\n🔗 {url}"
        if failed:
            msg += f"\n\n⚠️ Failed to fetch: {', '.join('@' + u for u in failed)}"
        await update.message.reply_text(msg)
    else:
        # Telegraph failed, fall back to individual messages (capped at 10)
        await update.message.reply_text(
            "⚠️ Telegraph unavailable, sending messages directly (capped at 10)..."
        )
        for username, entry in all_posts[:10]:
            await send_message(context.bot, int(chat_id), format_message(username, entry))
            await asyncio.sleep(0.3)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain-text messages: auto-add on URL, confirm button on bare username/@mention."""
    msg = update.effective_message
    # Ignore channel→group auto-forwards and anything posted on behalf of a channel
    # (including the bot's own posts mirrored from the linked discussion channel).
    # Without this guard the bot reads the x.com links inside its own posts and
    # auto-adds them as accounts, a runaway feedback loop.
    if msg is None or msg.is_automatic_forward or msg.sender_chat is not None:
        return
    text = (msg.text or "").strip()
    username = parse_username(text)
    if not username:
        return

    is_url = bool(
        re.search(r"https?://(?:twitter\.com|x\.com)/\w", text, re.IGNORECASE)
        or any(inst.replace("https://", "") in text for inst in KNOWN_MIRRORS)
    )

    if is_url:
        # High confidence, add immediately
        await _do_add(update, username)
    else:
        # Bare username or @mention, ask first to avoid false positives
        await update.message.reply_text(
            f"Add @{username} to your watchlist?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, add", callback_data=f"add:{username}"),
                InlineKeyboardButton("❌ No", callback_data="add:cancel"),
            ]]),
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button presses (remove account, confirm add)."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = str(query.message.chat_id)

    # ── Remove account ──────────────────────────────────────────────────────
    if data.startswith("rm:"):
        username = data[3:]
        config = load_config()
        accounts: list = config.get(chat_id, [])
        if username not in accounts:
            await query.answer("Already removed!", show_alert=True)
            return
        accounts.remove(username)
        config[chat_id] = accounts
        save_config(config)
        logger.info(f"Chat {chat_id}: removed @{username} via button")

        if accounts:
            await query.edit_message_text(
                f"Watching {len(accounts)} account(s), tap ❌ to remove:",
                reply_markup=_list_keyboard(accounts),
            )
        else:
            await query.edit_message_text("No accounts monitored. Send a username to add one.")

    # ── Confirm add ─────────────────────────────────────────────────────────
    elif data.startswith("add:"):
        username = data[4:]
        if username == "cancel":
            await query.edit_message_text("Cancelled.")
            return
        # Replay through _do_add, need a fake update-like object
        # Simplest: edit the message to show result, call the logic directly
        config = load_config()
        accounts: list = config.get(chat_id, [])
        if username in accounts:
            await query.edit_message_text(f"@{username} is already in the watchlist.")
            return
        if len(accounts) >= MAX_ACCOUNTS_PER_CHAT:
            await query.edit_message_text(
                f"⚠️ Limit of {MAX_ACCOUNTS_PER_CHAT} accounts reached. Remove one first."
            )
            return
        accounts.append(username)
        config[chat_id] = accounts
        save_config(config)
        await query.edit_message_text(
            f"✅ Now monitoring @{username}\n"
            f"New posts within 15 min, or use /check now."
        )
        logger.info(f"Chat {chat_id}: added @{username} via confirm button")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Immediately run one check cycle (only sends posts newer than last seen)."""
    await update.message.reply_text("Running check cycle...")
    await check_cycle(context)
    await update.message.reply_text("✅ Done.")


POSTTO_USAGE = (
    "📣 Route this chat's new posts to a channel.\n\n"
    "Usage:\n"
    "  /postto @channelusername\n"
    "  /postto -1001234567890\n\n"
    "I must be an admin with permission to post in that channel.\n"
    "Posts go to the channel only, if the channel has a linked discussion "
    "group, Telegram mirrors each post there automatically.\n\n"
    "Turn routing off (deliver here again): /postto off"
)


async def cmd_postto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route this chat's periodic deliveries to a channel (manual channel ID/username)."""
    chat_id = str(update.effective_chat.id)
    routes = load_routes()

    if not context.args:
        current = routes.get(chat_id)
        if current:
            await update.message.reply_text(
                f"📣 This chat currently delivers new posts to channel {current}.\n\n"
                + POSTTO_USAGE
            )
        else:
            await update.message.reply_text(POSTTO_USAGE)
        return

    arg = context.args[0].strip()

    # Turn routing off
    if arg.lower() in {"off", "here", "none", "stop"}:
        if routes.pop(chat_id, None) is not None:
            save_routes(routes)
            await update.message.reply_text("✅ Routing off. New posts will be delivered here again.")
            logger.info(f"Chat {chat_id}: channel routing cleared")
        else:
            await update.message.reply_text("This chat has no channel routing set.")
        return

    # Resolve the target: @username (str) or numeric id (int)
    target: str | int = arg
    if arg.lstrip("-").isdigit():
        target = int(arg)
    elif not arg.startswith("@"):
        target = "@" + arg

    # Validate the channel exists and the bot can post there
    try:
        channel = await context.bot.get_chat(target)
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Couldn't find that channel: {e}\n"
            "Check the @username or ID, and make sure I've been added to it."
        )
        return

    try:
        member = await context.bot.get_chat_member(channel.id, context.bot.id)
        can_post = (
            member.status == "creator"
            or (member.status == "administrator" and getattr(member, "can_post_messages", False))
        )
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Couldn't verify my permissions in that channel: {e}\n"
            "Make sure I'm an admin there."
        )
        return

    if not can_post:
        await update.message.reply_text(
            "⚠️ I'm not allowed to post in that channel.\n"
            "Make me an admin with the 'Post Messages' permission, then try again."
        )
        return

    routes[chat_id] = str(channel.id)
    save_routes(routes)
    title = channel.title or channel.username or str(channel.id)
    await update.message.reply_text(
        f"✅ New posts from this chat's watchlist will now be sent to “{title}”.\n"
        "Manage the watchlist here as usual (/add, /list, /check).\n"
        "Turn it off with /postto off."
    )
    logger.info(f"Chat {chat_id}: routing deliveries to channel {channel.id}")


# ---------------------------------------------------------------------------
# Concurrent feed fetching
# ---------------------------------------------------------------------------

async def fetch_feeds_concurrent(usernames: list[str], sources: list | None = None) -> dict[str, list | None]:
    """Fetch all feeds in parallel (up to FETCH_CONCURRENCY at a time)."""
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch_one(username: str) -> tuple[str, list | None]:
        async with sem:
            result = await asyncio.to_thread(fetch_feed, username, sources)
            return username, result

    pairs = await asyncio.gather(*[fetch_one(u) for u in usernames], return_exceptions=True)
    return {
        u: r
        for pair in pairs
        if not isinstance(pair, Exception)
        for u, r in [pair]
    }


# ---------------------------------------------------------------------------
# Periodic check job
# ---------------------------------------------------------------------------

def _hold_reason(entry, entry_id: str, switched: bool, newest_seen: int) -> str | None:
    """Why an unseen entry must NOT be sent, or None to send it."""
    if switched and (entry.get("_repost") or entry.get("_pinned")):
        return "source switch"
    if entry.get("_repost"):
        return None  # reposts carry the original post's date and ID; see the anti-flood notes in the README
    # Own posts: X status IDs grow with time, so a post made since the last check is
    # always newer than everything already seen. An older unseen one is history that
    # a different source (or a flaky one) surfaced, not news.
    if entry_id.isdigit() and int(entry_id) < newest_seen:
        return "own post older than newest seen"
    t = entry.get("published_parsed")
    if t and time.time() - calendar.timegm(t) > MAX_OWN_POST_AGE:
        return "own post older than 48h"
    return None


async def check_cycle(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with _state_lock:
        await _check_cycle(context)


async def _check_cycle(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    if not config:
        logger.info("No chats configured yet, skipping cycle.")
        return

    # Build a map: username -> [chat_ids] to fetch each account only once
    account_to_chats: dict[str, list[str]] = {}
    for chat_id, accounts in config.items():
        for username in accounts:
            account_to_chats.setdefault(username, []).append(chat_id)

    if not account_to_chats:
        logger.info("No accounts to monitor, skipping cycle.")
        return

    logger.info(
        f"Cycle start, {len(account_to_chats)} unique account(s) "
        f"across {len(config)} chat(s)"
    )

    # Fetch every unique account concurrently
    feed_cache = await fetch_feeds_concurrent(list(account_to_chats.keys()))

    # Starvation alarm: zero successful fetches across the whole cycle means the
    # pipeline is dead (host cron broken, whitelist revoked, instances gone).
    global _starved_cycles, _last_starvation_alert
    fetched_ok = sum(1 for entries in feed_cache.values() if entries)
    if fetched_ok == 0:
        _starved_cycles += 1
        logger.error(
            f"Starvation: 0/{len(account_to_chats)} feeds fetched "
            f"({_starved_cycles} cycle(s) in a row)"
        )
        if (
            _starved_cycles >= STARVATION_CYCLES
            and time.time() - _last_starvation_alert > STARVATION_COOLDOWN
        ):
            if ADMIN_CHAT_ID is None:
                logger.error("Starvation alarm: no ADMIN_CHAT_ID set, not sending an alert")
                _last_starvation_alert = time.time()
            else:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=(
                            f"⚠️ X Monitor Bot: every feed fetch has failed for "
                            f"{_starved_cycles * CHECK_INTERVAL // 60} minutes. "
                            "Check FEED_SOURCES in main.py (FxTwitter, then the Nitter forks) "
                            "and the bot's logs."
                        ),
                    )
                    _last_starvation_alert = time.time()
                except Exception as e:
                    logger.error(f"Could not send starvation alert: {e}")
    else:
        _starved_cycles = 0

    state = load_state()
    routes = load_routes()

    for username, chat_ids in account_to_chats.items():
        entries = feed_cache.get(username)
        if not entries:
            continue

        current_ids = [extract_post_id(dict(e)) for e in entries]

        # Source switch (fx <-> nitter): the two families can give the same repost
        # different IDs, so reposts visible at the switch are absorbed, not re-sent.
        # A fresh process counts as a switch (it cannot know what was delivered before).
        # A one-cycle flap is skipped instead: nothing is sent or saved for the account
        # this cycle, and fresh reposts go out normally on the next one.
        family = entries[0].get("_family", "fx")
        last = _last_family.get(username)
        if last is not None and family != last and _pending_family.get(username) != family:
            _pending_family[username] = family
            logger.info(f"@{username}: {family} served this cycle, waiting one cycle before switching")
            continue
        _pending_family.pop(username, None)
        switched = family != last
        _last_family[username] = family
        if switched and last is not None:
            logger.info(f"@{username}: feed source switched to {family}, absorbing visible reposts")

        for chat_id in chat_ids:
            state_key = f"{chat_id}:{username}"
            seen_ids: set[str] = set(state.get(state_key, []))
            is_first_time = state_key not in state

            if is_first_time:
                state[state_key] = current_ids
                logger.info(
                    f"First time: @{username} in chat {chat_id}, "
                    f"saved {len(current_ids)} IDs silently"
                )
                continue

            unseen = [
                (extract_post_id(dict(e)), e)
                for e in entries
                if extract_post_id(dict(e)) not in seen_ids
            ]
            # Held entries are never sent; they still land in state via current_ids below
            # Newest OWN post already seen, from this feed. Reposts are left out: their
            # IDs belong to other people's posts and would skew the comparison.
            newest_seen = max(
                (int(eid) for eid, e in zip(current_ids, entries)
                 if eid in seen_ids and not e.get("_repost") and eid.isdigit()),
                default=0,
            )
            new_entries = [
                (eid, e) for eid, e in unseen
                if not _hold_reason(e, eid, switched, newest_seen)
            ]
            held = len(unseen) - len(new_entries)
            if held:
                logger.info(f"@{username} → chat {chat_id}: held back {held} old/switched post(s)")

            # Deliver to the routed channel if one is set, else to the chat itself.
            # State/first-run stay keyed on chat_id; only the destination changes.
            target_id = int(routes.get(chat_id, chat_id))

            # Oldest first. FxTwitter feeds are not strictly newest-first, so sort by
            # date (stable, so undated entries keep the old reversed-feed order)
            to_send = list(reversed(new_entries))
            to_send.sort(key=lambda p: tuple(p[1].get("published_parsed") or ()))
            for entry_id, entry in to_send:
                message = format_message(username, dict(entry))
                try:
                    await send_message(context.bot, target_id, message)
                    logger.info(f"Sent @{username}/{entry_id} → {target_id} (chat {chat_id})")
                except Exception as e:
                    logger.error(
                        f"Failed to send @{username}/{entry_id} → {target_id} (chat {chat_id}): {e}"
                    )
                seen_ids.add(entry_id)
                await asyncio.sleep(0.3)

            # Accumulate seen IDs preserving order so the cap drops the OLDEST entries.
            # Using set union (list(set | set)) gives random order and randomly drops
            # recently-sent IDs when capping, that was the original duplicate-loop bug.
            existing_ordered = state.get(state_key, [])
            existing_set = set(existing_ordered)
            merged = existing_ordered + [eid for eid in current_ids if eid not in existing_set]
            state[state_key] = merged[-MAX_SEEN_PER_KEY:]

            if new_entries:
                logger.info(
                    f"@{username} → chat {chat_id}: {len(new_entries)} new post(s)"
                )
            else:
                logger.info(f"@{username} → chat {chat_id}: no new posts")

    save_state(state)
    logger.info("Cycle complete.")


# ---------------------------------------------------------------------------
# Health check server + keep-alive
# ---------------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server() -> None:
    try:
        server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
        logger.info(f"Health server listening on port {HEALTH_PORT}")
        server.serve_forever()
    except OSError as e:
        logger.warning(f"Health server could not bind to port {HEALTH_PORT}: {e} (continuing without it)")


def keepalive_loop() -> None:
    time.sleep(30)
    while True:
        try:
            with httpx.Client(timeout=5) as client:
                client.get(f"http://localhost:{HEALTH_PORT}/")
            logger.debug("Keep-alive ping OK")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(KEEPALIVE_INTERVAL)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def gated(handler):
    """Wrap a handler so only users in ALLOWED_USER_IDS reach it. Strangers get no answer."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.is_bot or not is_allowed(user.id):
            logger.info(f"Ignored update from a user not on the list ({user.id if user else '?'})")
            if update.callback_query is not None:
                await update.callback_query.answer()
            return
        await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty: the bot will answer nobody")

    if WATCHED_ACCOUNTS and not DIGEST_CHAT_ID:
        logger.warning("WATCHED_ACCOUNTS is set but DIGEST_CHAT_ID is not a numeric chat id, ignoring it")
    config = load_config()
    if seed_config(config, DIGEST_CHAT_ID, WATCHED_ACCOUNTS):
        save_config(config)
        logger.info(f"Watchlist from WATCHED_ACCOUNTS merged into chat {DIGEST_CHAT_ID}")

    logger.info(f"Feed sources: {', '.join(t.split('/')[2] for _, t in FEED_SOURCES)}")

    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", gated(cmd_start)))
    app.add_handler(CommandHandler("help", gated(cmd_help)))
    app.add_handler(CommandHandler("support", gated(cmd_support)))
    app.add_handler(CommandHandler("add", gated(cmd_add)))
    app.add_handler(CommandHandler("remove", gated(cmd_remove)))
    app.add_handler(CommandHandler("list", gated(cmd_list)))
    app.add_handler(CommandHandler("recent", gated(cmd_recent)))
    app.add_handler(CommandHandler("check", gated(cmd_check)))
    app.add_handler(CommandHandler("postto", gated(cmd_postto)))
    app.add_handler(CallbackQueryHandler(gated(handle_callback)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gated(handle_message)))

    # First check after 15 s, then every 15 min
    app.job_queue.run_repeating(check_cycle, interval=CHECK_INTERVAL, first=15)

    logger.info("Bot started. Listening for commands and scheduling checks...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
