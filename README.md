# TLDR Discord Bot

Posts new items from TLDR RSS feeds to a Discord text channel.

## Setup

Add your bot token to `.env`:

```env
DISCORD_BOT_TOKEN=your-token-here
```

By default the bot reads TLDR AI and TLDR Data, then posts to a channel named `news-channel`.
Items with titles ending in `(Sponsor)` are skipped.

Optional `.env` settings:

```env
DISCORD_NEWS_CHANNEL_ID=123456789012345678
DISCORD_NEWS_CHANNEL_NAME=news-channel
FEED_URLS=TLDR AI|https://bullrich.dev/tldr-rss/ai.rss,TLDR Data|https://bullrich.dev/tldr-rss/data.rss
TLDR_POLL_MINUTES=60
TLDR_FIRST_RUN_LIMIT=5
TLDR_SEEN_FILE=.tldr_seen.json
```

Each feed is configured as `Name|URL`, separated by commas. You can add more TLDR feeds to `FEED_URLS` with the same format.

Using `DISCORD_NEWS_CHANNEL_ID` is the most reliable option. If it is not set, the bot looks for the first text channel named `news-channel`.

## Run

```bash
uv run python main.py
```
