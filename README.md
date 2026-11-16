<img src="https://raw.githubusercontent.com/sahandsorouri/sahandsorouri/main/assets/banner.svg" alt="Sahand Sorouri" width="100%">

# X Monitor Bot

A Telegram bot that watches chosen X (Twitter) accounts and posts their new posts to a Telegram chat, group or channel. It reads public feeds (FxTwitter first, then Nitter forks), so no X account or API key is needed.

## What it does

- Checks every watched account every 15 minutes and sends each new post as a short message with a tappable x.com link.
- Each chat has its own watchlist. Send a handle, an `@mention` or an x.com profile link to add one; `/list` shows the list with a button to remove each account.
- `/recent` publishes every post from the last 24 hours to one Telegraph page and sends you the link.
- `/postto` routes a chat's new posts to a channel where the bot is an admin.
- The first check after adding an account is silent, so you never get a flood of old posts.
- Falls back through FxTwitter, FixupX and two Nitter forks automatically.
- Optional alarm: if every feed fetch fails for an hour, the bot messages `ADMIN_CHAT_ID`.

Message format:

```
👤 @example_account
📝 Post text
🔗 https://x.com/example_account/status/...
⏰ 5 minutes ago
```

## Commands

| Command | What it does |
|---|---|
| `/start` | Register this chat |
| `/add example_account` | Watch an account in this chat |
| `/remove example_account` | Stop watching it |
| `/list` | Show the watchlist, tap ❌ to remove |
| `/recent` | Last 24 hours as a Telegraph page |
| `/check` | Run a check now |
| `/postto @channel` | Send new posts to a channel; `/postto off` to stop |
| `/support` | Show the support contact, if one is set |
| `/help` | Command reference |

## Setup

1. Create a bot with [BotFather](https://t.me/BotFather) and copy its token. To use it in groups, send `/setprivacy` to BotFather and choose Disable.
2. Copy the example settings and fill them in:

```bash
cp .env.example .env
```

3. Run it with Docker:

```bash
docker compose up -d --build
```

Or locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

## Choosing the accounts

Nothing is hardcoded. Accounts come from two places, and both end up in the same watchlist:

- `WATCHED_ACCOUNTS` plus `DIGEST_CHAT_ID` in `.env`: at start, these handles are added to that chat's watchlist (for example `WATCHED_ACCOUNTS=example_account,another_account`).
- Bot commands at runtime: `/add`, or just send a handle or link in an allowed chat.

## Environment variables

| Name | Required | What it is |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token |
| `ALLOWED_USER_IDS` | yes | Comma separated numeric Telegram user ids allowed to use the bot |
| `WATCHED_ACCOUNTS` | no | Comma separated X handles to watch from the start |
| `DIGEST_CHAT_ID` | with `WATCHED_ACCOUNTS` | Numeric chat id that receives those accounts' posts |
| `ADMIN_CHAT_ID` | no | Numeric chat id for the "all feeds failing" alert; empty turns it off |
| `SUPPORT_CONTACT` | no | Contact shown in `/help` and `/support` |
| `DATA_DIR` | no | Where the runtime JSON files live, default the current folder (`/app/data` in Docker) |
| `HEALTH_PORT` | no | Port of the small health endpoint, default `8080` |

## Access

The bot is default deny. Only Telegram user ids in `ALLOWED_USER_IDS` can run commands, press buttons or add accounts. Anyone else gets no answer, and the attempt is logged with their id. If the list is empty, the bot answers nobody and logs a warning at start. In a group, only allowed members can change the watchlist; everyone in the group still sees the posts.

To find your numeric user id, message a bot such as `userinfobot`, or send this bot a message and read the id from its log.

## Runtime files

Kept in `DATA_DIR` and never committed:

| File | What it holds |
|---|---|
| `config.json` | Watchlists per chat: `{"chat_id": ["example_account"]}` |
| `seen_posts.json` | Post ids already handled, keyed `"chat_id:username"` |
| `routes.json` | Channel routing set with `/postto` |
| `telegraph_token.json` | The Telegraph account used by `/recent` |

## How it avoids floods and repeats

- Seen post ids are accumulated in order and capped at 200 per account and chat, so the oldest drop first.
- A post is held back (marked seen, never sent) when the feed source just switched and it is a repost or pinned post, when it is the account's own post with an id lower than the newest one already seen, or when it is the account's own post older than 48 hours. Reposts are exempt from the age checks, since they carry the original post's date.
- A switch between feed families waits one cycle, so a flaky source does not cause re-sends.
- If you change feed sources, stop the bot and run `rebaseline.py` once (see its docstring). It marks every post currently visible as seen.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
```

The tests run offline and need no token.

## License

MIT, see `LICENSE`.

Built by Sahand Sorouri, https://sahand.sorouri.com
