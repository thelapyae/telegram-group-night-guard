# Telegram Group Night Guard

<p align="center">
  <img src="assets/night-guard.png" width="280" alt="Telegram Group Night Guard pixel-art profile image">
</p>

Automatically close a Telegram group at night and reopen it in the morning.
Night Guard changes the default sending permissions for non-admin members, so it
works efficiently even in very large groups and linked channel discussion groups.

## The problem

Active Telegram communities can be difficult to moderate overnight. Spam, scams,
arguments, and off-topic messages may remain visible for hours while moderators
are asleep. Telegram has Slow Mode, but it does not provide a daily quiet-hours
schedule that completely prevents members from sending messages.

Night Guard solves this by:

- locking member sending permissions at a configured hour;
- restoring the group's original daytime permissions in the morning;
- checking the expected state every minute, so it recovers after a server reboot
  or a missed schedule boundary;
- using one group-wide Telegram API call instead of looping over every member.

Administrators are not affected by Telegram's default chat permissions and can
still post during quiet hours.

## How it works

The included cron job starts a short Python process once per minute. The process:

1. reads commands received through Telegram `getUpdates`;
2. calculates the current time in the configured IANA timezone;
3. changes permissions only when the group needs to transition between locked and
   unlocked mode;
4. saves the update offset, original permissions, and current mode in a small local
   JSON state file.

There is no member database, webhook, public domain, or continuously running
process. The bot uses Python's standard library and has no third-party package
dependencies.

## Requirements

- Linux VPS or another machine that runs cron continuously
- Python 3.10 or newer
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Bot administrator access with **Restrict Members** (sometimes shown as
  **Ban Users**) permission

## Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token secret.
2. For a channel, open **Channel settings → Discussion** and enter the linked
   discussion group. Add the bot to the discussion group, not only the channel.
3. Promote the bot to administrator and enable **Restrict Members**.

## VPS installation

```bash
git clone https://github.com/thelapyae/telegram-group-night-guard.git
cd telegram-group-night-guard
cp .env.example .env
chmod 600 .env
nano .env
chmod 700 run.sh quiet_hours_bot.py
```

Set the new token in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=replace_with_your_real_token
QUIET_BOT_TIMEZONE=Asia/Yangon
QUIET_BOT_LOCK_HOUR=23
QUIET_BOT_UNLOCK_HOUR=9
QUIET_BOT_DATA_DIR=/home/your-user/.local/share/telegram-quiet-hours
```

Never commit `.env` or paste a real bot token into an issue, log, screenshot, or
chat. If a token is exposed, revoke it immediately with BotFather.

Open the user's crontab with `crontab -e` and add:

```cron
* * * * * /absolute/path/telegram-group-night-guard/run.sh >>/absolute/path/telegram-group-night-guard/bot.log 2>&1
```

## Configure a group

### Option 1: Telegram command

Send this inside the discussion group:

```text
/setup@your_bot_username
```

Only a group administrator can run the setup command. Use `/status` to see the
saved group ID and current mode.

### Option 2: Numeric group ID

If Privacy Mode or another Telegram setting prevents command delivery, copy a
message link from the private discussion group:

```text
https://t.me/c/1234567890/42
```

Prefix the number after `/c/` with `-100`, producing `-1001234567890`, then run:

```bash
set -a
. ./.env
set +a
python3 quiet_hours_bot.py configure -1001234567890
```

## Cost

| Item | Cost |
|---|---:|
| Telegram Bot API | Free |
| This software | Free and open source |
| Existing always-on VPS | Usually no additional cost |
| New VPS | Provider-dependent; the smallest general-purpose plan is normally enough |
| Domain and TLS certificate | Not required |
| External database | Not required |

The job runs briefly once per minute and performs permission updates only at mode
transitions. CPU, memory, disk, and network use are minimal.

## Where it can run

- **Linux VPS — recommended:** precise cron scheduling, predictable timezone
  handling, and no additional service dependency.
- **Home server, NAS, or Raspberry Pi:** works if the machine and internet
  connection remain available overnight.
- **Docker host:** possible by running the script on a one-minute scheduler and
  mounting persistent storage for the state file.
- **Vercel:** possible only after adapting the polling design to webhooks and
  Vercel Cron. Hobby cron timing may not be precise enough for strict boundaries.
- **GitHub Actions:** technically possible, but scheduled workflows are not
  guaranteed to run at an exact minute and are not recommended for strict locks.

## Security and privacy

- The bot token is loaded from `.env`, which is excluded by `.gitignore`.
- State is stored locally with mode `0600` and contains group IDs, titles,
  permissions, and Telegram update offsets—not member messages or member lists.
- The bot does not need to read ordinary conversation content. Telegram Privacy
  Mode can remain enabled.
- Run the bot as an unprivileged operating-system user. Root access is unnecessary.

## Limitations

- Telegram default permissions do not restrict administrators.
- The host must be online near the schedule boundary. The next minute's run will
  repair the state after a temporary outage.
- A group's original daytime permissions must be captured before the first night
  lock. Night Guard preserves those permissions when reopening the group.
- Times are whole hours in the current release.

## Development

Syntax and boundary checks:

```bash
python3 -m py_compile quiet_hours_bot.py
python3 -m unittest -v
```

## License

[MIT](LICENSE)
