# GameGather

A Python-first MVP for organizing pick-up games with an initial focus on BOTC-style gatherings.

## Quick start

1. Copy `.env.example` to `.env` and configure at least one notification provider with real credentials. Email is enabled by default; the example credentials are placeholders.
2. Build and run:

```bash
docker compose up --build
```

3. Open http://localhost:3000

Set `BASE_URL` to an address recipients can reach before sending notifications.
Keep `DATABASE_PATH=/data/gamegather.db` when using the supplied Compose volume
so the database survives container replacement.

## Game workflow

**`open` → `gathering` → `started`**

1. **Create a game:** Enter a title, minimum player count, organizer name, and
   notification details. The game is immediately open. Share the public game link
   and keep the private organizer link secret; there are no user accounts.
2. **Collect interest:** Players register with a display name and one enabled
   notification method. A confirmation through that method includes a private
   management link, usable immediately to withdraw while the game is open. The
   organizer can also add in-person players by name only.
   Reaching the minimum queues an organizer alert, but does not change the game
   state or automatically start gathering.
3. **Start gathering:** Once the minimum is met, the organizer selects **Start
   gathering**. Registration closes, and players with contact details receive
   private attendance links. They can select **Coming** or **Decline** and change
   their response while the game is gathering. The organizer page shows individual
   responses when loaded; it does not automatically refresh.
4. **Start the game:** The organizer selects **Start game**, regardless of the
   number of confirmed players. This locks attendance responses and further status
   changes. Only players who have not responded receive a game-start notification.

The same private link lets players withdraw while open or respond during
gathering. Opening it does not remove a registration: players must explicitly
select **Withdraw registration**. Declining during gathering retains the
registration instead. There are no draft or closed/cancelled workflow states.

Keep management links private: anyone with a link can manage that registration.
Use HTTPS in public deployments. API deletion at
`DELETE /registrations/{registration_id}` requires that registration's token in
the `X-Registration-Token` header; an ID alone no longer permits deletion.
Existing private attendance links continue to work, but registration confirmations
are sent only for new registrations, not retroactively.

## Email notifications

Set `EMAIL_ENABLED=true` and configure `MAILJET_SMTP_HOST`, `MAILJET_SMTP_PORT`,
`MAILJET_SMTP_USERNAME`, `MAILJET_SMTP_PASSWORD`, and `MAIL_FROM_ADDRESS` in `.env`.
The implementation sends through SMTP with STARTTLS; `.env.example` uses Mailjet.
Email ownership verification is not implemented.

Notifications are queued in SQLite and processed by an in-app worker polling every
10 seconds. Temporary delivery failures are retried; permanent failures or
exhausted retries are recorded as failed. There is no separate worker to launch.

## Discord notifications

Set `DISCORD_ENABLED=true`, `DISCORD_BOT_TOKEN`, and `DISCORD_GUILD_ID` in your
local `.env` using the names documented in `.env.example`. Set `BASE_URL` to the
address players can reach; it is used in organizer and attendance links.

Invite the bot to the configured server with the `bot` and `applications.commands`
scopes. Privileged intents (including Message Content) are not needed. The app
connects the bot and registers `/connect` in that server during startup.

Players and organizers can run `/connect` in the server to receive their Discord
user ID by DM, then select Discord and paste that ID into the website form.
If DMs are blocked, the command explains how to enable them. There is no mandatory
ownership-verification flow, and no user accounts or linking records are stored.

The notification queue sends organizer and player registration confirmations, minimum-player alerts,
gathering requests, and game-start notices to unanswered players by DM when
Discord is selected. Unknown users and blocked DMs are marked
as failed; temporary delivery errors use the queue's retry policy. Email remains
available when `EMAIL_ENABLED=true`.

## SMS notifications

Enable `SMS_ENABLED=true` and set `VONAGE_API_KEY`, `VONAGE_API_SECRET`, and
`VONAGE_SMS_FROM` using `.env.example`. The sender defaults in the example to
`GameGather`.

SMS is available only for Swedish mobile numbers in international format, for
example `+46701234567` (omit the leading zero from `0701234567`). Both forms show
format instructions and inline validation; API submissions are validated too.
Norwegian numbers, Swedish landlines, and local-format numbers are rejected.

Messages identify the game and include the relevant organizer, attendance, or
public game link. `sent` means Vonage accepted the message, not confirmed handset delivery.
Vonage charges per SMS segment; long links and Unicode can require extra segments.

## Telegram notifications

Set `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, and
`TELEGRAM_BOT_USERNAME=gamegather_notifications_bot` using `.env.example`.
Open `https://t.me/<TELEGRAM_BOT_USERNAME>` (for example,
https://t.me/gamegather_notifications_bot) and press Start. The bot replies
in the private chat with the chat ID to enter when selecting Telegram on the
website. No user accounts or verification records are stored.

The app uses long polling, so no public webhook endpoint is needed. Run one
polling app instance per bot token, and do not configure a webhook for this bot.
Telegram sends organizer and player registration confirmations, minimum-player alerts, private attendance
links, and game-start notices to unanswered players through the queue and retry
policy. Set `BASE_URL`
to an address recipients can reach.

## In-person players

While a game is open, use **Add an in-person player** on its private organizer
page to register someone by name only. Adding them immediately confirms they
are coming and counts toward the minimum needed to start gathering. They receive
no registration, gathering, or game-start notifications.

API clients can POST `{"display_name": "Player name"}` to
`/organizer/games/{organizer_token}/register`. The private organizer token is
required; public registration still requires notification details.

## Stack

- Python
- FastAPI
- Jinja2 server-rendered pages with custom CSS and JavaScript
- SQLite
- Docker Compose

This keeps the core game logic readable and easy to reason about while still supporting portable deployment.

## Project documentation

- [`design.md`](design.md): Current architecture, data model, and clearly labeled future ideas.
- [`MVP_BACKLOG.md`](MVP_BACKLOG.md): Implemented features, remaining work, and optional proposals.
- [`AGENTS.md`](AGENTS.md): Coding conventions, implementation gotchas, and test guidance.
