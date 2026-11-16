#!/usr/bin/env python3
"""Mark every post currently visible in each watched feed as seen. Sends nothing.

Use after switching feed sources, so the new source's backlog (posts the old source
never showed) is not delivered as "new". Run with the bot STOPPED, because it
rewrites seen_posts.json:

    docker compose stop x-monitor
    docker compose run --rm --no-deps x-monitor python rebaseline.py
    docker compose up -d x-monitor

It reads each account from BOTH feed families (fx and nitter) and absorbs the union,
because the two show different posts and repost IDs, and the bot may read either.

Keys not yet in seen_posts.json are left alone: the bot's first-run logic already
saves those silently. Keys whose account could not be fetched from any source are
DELETED, so first-run reseeds them silently when the account comes back (keeping
their stale IDs would deliver the whole backlog then). Safe to re-run.
"""

import asyncio

import main


async def run() -> None:
    config = main.load_config()
    accounts = sorted({u for users in config.values() for u in users})

    ids: dict[str, list[str]] = {u: [] for u in accounts}
    missed: dict[str, list[str]] = {}
    for family in ("fx", "nitter"):
        sources = [s for s in main.FEED_SOURCES if s[0] == family]
        feeds = await main.fetch_feeds_concurrent(accounts, sources)
        for u in accounts:
            if feeds.get(u):
                ids[u] += [main.extract_post_id(dict(e)) for e in feeds[u]]
            else:
                missed.setdefault(family, []).append(u)

    state = main.load_state()
    absorbed = 0
    reset = []
    for chat_id, users in config.items():
        for username in users:
            key = f"{chat_id}:{username}"
            if key not in state:
                continue
            if not ids[username]:
                state.pop(key)
                reset.append(key)
                continue
            existing = state[key]
            existing_set = set(existing)
            add = [i for i in dict.fromkeys(ids[username]) if i not in existing_set]
            if add:
                state[key] = (existing + add)[-main.MAX_SEEN_PER_KEY:]
                absorbed += len(add)

    main.save_state(state)
    unreachable = [u for u in accounts if not ids[u]]
    print(f"Absorbed {absorbed} post ID(s) for {len(accounts) - len(unreachable)}/{len(accounts)} accounts.")
    print(f"Not served by fx: {', '.join(missed.get('fx', [])) or 'none'}")
    print(f"Not served by nitter: {', '.join(missed.get('nitter', [])) or 'none'}")
    if reset:
        print(f"Reset (reseed silently when back): {', '.join(reset)}")


if __name__ == "__main__":
    asyncio.run(run())
