# Telegram Group Night Guard

<p align="center">
  <img src="assets/night-guard.png" width="280" alt="Telegram Group Night Guard pixel-art profile image">
</p>

Automatically close a Telegram group at night, reopen it in the morning, and let
the community report off-topic or abusive messages for administrator review.
Night Guard works efficiently even in very large groups and linked channel
discussion groups.

## The problem

Active Telegram communities can be difficult to moderate overnight. Spam, scams,
arguments, and off-topic messages may remain visible for hours while moderators
are asleep. Telegram has Slow Mode, but it does not provide a daily quiet-hours
schedule that completely prevents members from sending messages.

Large communities also need a safe way for members to flag irrelevant content
without giving every member the power to ban someone. A raw `/ban` command for
everyone would be easy to abuse, while asking a bot to guess the "most recent"
message could punish the wrong person during a busy conversation.

Night Guard solves this by:

- locking member sending permissions at a configured hour;
- restoring the group's original daytime permissions in the morning;
- checking the expected state every minute, so it recovers after a server reboot
  or a missed schedule boundary;
- using one group-wide Telegram API call instead of looping over every member.
- accepting reply-based reports from unique community members;
- requiring administrator confirmation before a permanent ban;
- temporarily muting a reported user for one hour after five unique reports.

Administrators are not affected by Telegram's default chat permissions and can
still post during quiet hours.

## How it works

The included cron watchdog keeps one long-polling Python process running. The process:

1. receives commands and moderation-button actions immediately through Telegram
   `getUpdates` long polling;
2. calculates the current time in the configured IANA timezone;
3. changes permissions only when the group needs to transition between locked and
   unlocked mode;
4. stores report cases and audit events in local SQLite;
5. saves the update offset, original permissions, and current mode in a small JSON
   state file.

There is no webhook, public domain, external database, or third-party Python
dependency.

## Requirements

- Linux VPS or another machine that runs cron continuously
- Python 3.10 or newer
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Bot administrator access with **Restrict Members** (sometimes shown as
  **Ban Users**) and **Delete Messages** permissions

## Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token secret.
2. For a channel, open **Channel settings → Discussion** and enter the linked
   discussion group. Add the bot to the discussion group, not only the channel.
3. Promote the bot to administrator and enable **Restrict Members** and
   **Delete Messages**.

## VPS installation

```bash
git clone https://github.com/thelapyae/telegram-group-night-guard.git
cd telegram-group-night-guard
cp .env.example .env
chmod 600 .env
nano .env
chmod 700 run.sh run-daemon.sh quiet_hours_bot.py
```

Set the new token in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=replace_with_your_real_token
QUIET_BOT_TIMEZONE=Asia/Yangon
QUIET_BOT_LOCK_HOUR=23
QUIET_BOT_UNLOCK_HOUR=9
QUIET_BOT_DATA_DIR=/home/your-user/.local/share/telegram-quiet-hours
QUIET_BOT_REPORT_THRESHOLD=3
QUIET_BOT_AUTO_MUTE_THRESHOLD=5
QUIET_BOT_REPORT_MAX_AGE_MINUTES=30
QUIET_BOT_REPORT_RATE_LIMIT=5
QUIET_BOT_TEMP_MUTE_HOURS=1
QUIET_BOT_REMINDER_TIMES=09:30,14:00,20:00,23:30
QUIET_BOT_REMINDER_NAME=ညကင်း
```

Never commit `.env` or paste a real bot token into an issue, log, screenshot, or
chat. If a token is exposed, revoke it immediately with BotFather.

Open the user's crontab with `crontab -e` and add:

```cron
* * * * * /absolute/path/telegram-group-night-guard/run-daemon.sh >>/absolute/path/telegram-group-night-guard/bot.log 2>&1
```

The first cron invocation keeps the daemon running. Later invocations exit because
of the process lock. If the process or server restarts, cron starts it again within
one minute. `run.sh` remains available for one-shot polling installations.

The optional reminder schedule uses the same IANA timezone as quiet hours. Each
configured time sends one Burmese-friendly reporting reminder per group, with a
daily state guard that prevents duplicate delivery when the daemon loops. Before
sending, Night Guard deletes its previous scheduled reminder so only the latest
reminder remains in the group. Moderation alerts and decisions are preserved.

Send or replace the reminder immediately:

```bash
./run.sh remind
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

## Community moderation workflow

### Report a message

Any member can reply to a message sent within the last 30 minutes:

```text
/report@your_bot_username
```

Safeguards:

- one report per reporter per message;
- maximum five reports per reporter per hour;
- self-reports, bot reports, and reports against administrators are rejected;
- three unique reports create an administrator review panel;
- five unique reports temporarily mute the user for one hour;
- only an administrator can confirm a permanent ban.

The review panel provides **Ban & delete**, **Mute**, and **Dismiss** buttons.
Button clicks are checked against current Telegram administrator status.
Dismissal after an automatic mute closes the report case but does not end the
one-hour safety mute early.

### Direct administrator ban

An administrator can reply to the exact offending message:

```text
/ban@your_bot_username
```

Night Guard never guesses which "recent message" the administrator intended.
Replying creates an exact, race-free target. In a supergroup, Telegram permanently
bans the user and revokes their previous messages. Use this carefully.

Other administrator commands:

```text
/reports
/unban USER_ID
```

## Cost

| Item | Cost |
|---|---:|
| Telegram Bot API | Free |
| This software | Free and open source |
| Existing always-on VPS | Usually no additional cost |
| New VPS | Provider-dependent; the smallest general-purpose plan is normally enough |
| Domain and TLS certificate | Not required |
| External database | Not required; SQLite is included with Python |

Long polling waits for Telegram updates without consuming meaningful CPU. Permission
updates happen only at mode transitions. CPU, memory, disk, and network use remain
minimal for a typical community.

## Where it can run

- **Linux VPS — recommended:** precise cron scheduling, predictable timezone
  handling, and no additional service dependency.
- **Home server, NAS, or Raspberry Pi:** works if the machine and internet
  connection remain available overnight.
- **Docker host:** possible by running the daemon and mounting persistent storage
  for the state and SQLite files.
- **Vercel:** possible only after adapting the polling design to webhooks and
  Vercel Cron. Hobby cron timing may not be precise enough for strict boundaries.
- **GitHub Actions:** technically possible, but scheduled workflows are not
  guaranteed to run at an exact minute and are not recommended for strict locks.

## Security and privacy

- The bot token is loaded from `.env`, which is excluded by `.gitignore`.
- State and SQLite files use mode `0600`. The database stores reporter IDs, reported
  user IDs, message IDs, timestamps, case status, and an administrator audit log.
  It does not store message text or a member list.
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
- Telegram's `banChatMember` revokes the banned user's previous messages in a
  supergroup. If that is too destructive, use the temporary mute action instead.

## Development

Syntax and boundary checks:

```bash
python3 -m py_compile quiet_hours_bot.py
python3 -m unittest -v
```

## License

[MIT](LICENSE)
