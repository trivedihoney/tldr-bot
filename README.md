# tldr_bot

Posts new items from TLDR RSS feeds to Discord channels.

## Setup

Add your bot token to `.env`:

```env
DISCORD_BOT_TOKEN=your-token-here
```

Define feeds in `config.yml`. The file is ignored by git so local channel IDs and feed choices can stay private.

```yaml
seen_file: .tldr_seen.json

defaults:
  poll_minutes: 360
  first_run_limit: 5

feeds:
  - name: TLDR AI
    url: https://bullrich.dev/tldr-rss/ai.rss
    channel_name: ai-news
    channel_id:
    poll_minutes: 60
    first_run_limit: 5

  - name: TLDR Data
    url: https://bullrich.dev/tldr-rss/data.rss
    channel_name: data-news
    channel_id: 123456789012345678
    poll_minutes: 360
    first_run_limit: 5
```

Each feed can use `channel_id`, `channel_name`, or both. If both are present, `channel_id` is used. `poll_minutes` and `first_run_limit` can be set per feed, with `defaults` filling in omitted values.

Items with titles ending in `(Sponsor)` are skipped. Seen entries are tracked separately for each configured feed and destination.

Optional `.env` settings:

```env
TLDR_BOT_CONFIG=config.yml
```

If `config.yml` is not present, the bot falls back to the previous `FEED_URLS`, `DISCORD_NEWS_CHANNEL_ID`, `DISCORD_NEWS_CHANNEL_NAME`, `TLDR_POLL_MINUTES`, `TLDR_FIRST_RUN_LIMIT`, and `TLDR_SEEN_FILE` environment variables.

## Run

```bash
uv run python main.py
```

## Release

Use the manual GitHub Actions workflow to build the Windows and Linux
executables and attach them to a GitHub Release:

1. Open the repository on GitHub.
2. Go to **Actions**.
3. Select **Release**.
4. Click **Run workflow**.
5. Enter a release tag such as `v0.1.0`.

The release workflow runs `exe_maker.sh` on `windows-latest` and `ubuntu-latest`,
then uploads `tldr_bot-windows-x64.exe` and `tldr_bot-linux-x64`.
