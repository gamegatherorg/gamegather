# GameGather Design

## Purpose and scope

GameGather helps organize spontaneous and semi-spontaneous social games, initially
focused on **Blood on the Clocktower (BOTC)**. The workflow also fits board games,
RPG one-shots, and other group activities.

The core principle is **interest is not the same as commitment**: collect interest,
let the organizer decide when to gather, then collect attendance responses.
Players do not need an account or an installed app.

This document describes the current implementation. The final section preserves
future ideas from the original proposal; those are not implemented or committed
requirements. See `README.md` for setup and `MVP_BACKLOG.md` for remaining work.

## Current user workflow

### Create a proposal

The organizer provides a title, minimum player count, name, and one enabled
notification method and target. The game is created immediately as `open`.

Creation returns a public game URL and a private organizer URL. The website
redirects to the organizer page, and an organizer confirmation containing both
links is queued. Anyone holding the organizer link can manage that game; there
are no accounts or login sessions. QR code generation is not implemented.

### Register interest

The home page lists open games. Players visit a public game page and register a
display name plus one enabled notification method and target. Website submissions
redirect back to the game page. A `registration_created` confirmation is queued
through the selected provider with a private management link. Registrations are
specific to a game, not reusable player accounts.

The link uses the existing cryptographically random `attendance_token`, generated
with `secrets.token_urlsafe(32)`. While open, its page allows explicit withdrawal
by POST, physically deleting the registration. Opening a link never withdraws a
player. During gathering the same link allows attendance responses; after start,
it is read-only. No schema change or separate player account is needed. Existing
links remain valid, but confirmations are not backfilled for old registrations.

An organizer can also add an in-person player by name only while the game is open.
These registrations are immediately marked `coming`, count toward the minimum,
and have no notification delivery target.

When an insertion brings the registration count exactly to the minimum, an
organizer notification is queued. The game remains `open`: minimum reached is a
calculated condition, not a stored state or an automatic gathering trigger.

### Gather and respond

Once the registration count meets the minimum, the organizer can select **Start
gathering**. The game becomes `gathering`, and registration and deletion close.
Players with notification details receive private attendance links; in-person
players receive no notification and retain their confirmed response.

Attendance links allow players to select `coming` or `declined` and change their
response while gathering continues. Declining retains the registration. The
organizer page displays each player's current response when loaded; there is no
automatic live refresh.

### Start the game

The organizer explicitly selects **Start game** from a gathering game. This does
not require a minimum number of confirmed players. The game becomes `started`,
and attendance responses and further status changes are locked. A game-start
notification is queued only for unanswered registrations with a delivery method;
it contains no invitation to respond.

## Game lifecycle and removal rules

```text
open → gathering → started
```

| Operation | Open | Gathering | Started |
| --- | --- | --- | --- |
| Public registration or organizer-added in-person player | Allowed | Blocked | Blocked |
| Player withdrawal through private page or token-authorized API | Allowed | Blocked | Blocked |
| Attendance response through private link | Blocked | Submit or change | Blocked |
| Organizer starts gathering | Requires minimum registrations | Blocked | Blocked |
| Organizer starts game | Blocked | Allowed regardless of confirmations | Blocked |

The API endpoint `DELETE /registrations/{registration_id}` requires the matching
`attendance_token` in the `X-Registration-Token` header. Missing headers return
422; invalid or mismatched tokens return 404, as do missing registrations. Valid
tokens cannot remove a registration once gathering starts. Organizer tokens do
not authorize deletion; organizer removal is not implemented.

`draft`, `minimum_reached`, and `closed` are not implemented workflow states.
The database does not constrain the status strings, and the internal
`set_game_status()` helper accepts arbitrary non-`started` statuses before a game
starts. This does not constitute supported edit, close, cancel, or reopen flows.

## Current architecture

```text
Browser / API client
        ↓
FastAPI routes and Jinja2 pages (app/src/main.py, app/templates/)
        ↓
Game service (app/src/services/game_service.py)
        ↓
SQLite: games, registrations, notifications
        ↑
In-process notification worker → provider delivery
```

- **Routes** handle HTTP requests, form/JSON validation, rendering, and errors.
- **Game services** own registration rules and lifecycle transitions.
- **Database access** uses Python's `sqlite3` and `sqlite3.Row`; `init_db()` creates
  tables during application startup. SQLAlchemy and Alembic are not used.
- **UI** uses Jinja2 templates, custom CSS, and JavaScript, rather than a separate
  frontend application or an HTMX framework.
- **Notifications** are queued records, not an external event bus or subscriber
  system. Game services record delivery intent instead of calling providers.

Multi-step writes use transactions. Game creation and its organizer confirmation,
registration and its confirmation/threshold notices, gathering and all its player
notices, and game start and its notices each commit atomically. A queue insert
failure rolls back the associated data change and every notification inserted by
that operation. Delivery still happens asynchronously after commit; provider
failures use the worker's retry policy rather than rolling back the game.

## Current data model

### `games`

- `id`, `title`, `status`, `min_players`
- `organizer_name`, `organizer_notification_method`, `organizer_notification_target`
- `organizer_token`, `created_at`, `updated_at`

Organizer details are stored on the game, not in a separate organizer table.
There are no description, location, duration, ideal-count, or maximum-count fields.

### `registrations`

- `id`, `game_id`, `display_name`
- `notification_method`, `notification_target`
- Unique `attendance_token`
- Nullable `attendance_response` (`coming` or `declined`), `responded_at`

A null attendance response means unanswered. In-person registrations use method
`none`, an empty target, and an immediate `coming` response. Registration counts
count all remaining rows, not just players who have confirmed attendance.

There is no separate player, attendance-response, or verified-contact table, and
no soft-delete flag: removal deletes the registration row.

### `notifications`

- `id`, `game_id`, `recipient_role`, `recipient_name`
- `notification_method`, `notification_target`, `event_type`, optional `action_token`
- `status`, `attempt_count`, `next_attempt_at`, `last_error`, `sent_at`, `created_at`

Notification statuses are separate from game states: `queued`, `sending`, `sent`,
and `failed`. Dates and times are stored as text.

## Notification delivery

The implemented events are:

| Event | Recipient |
| --- | --- |
| `game_created` | Organizer, with public and private organizer links |
| `registration_created` | Newly registered player, with a private management link, via email, Discord, SMS, or Telegram |
| `minimum_players_reached` | Organizer |
| `gathering_started` | Registrations with contact details, with private attendance links |
| `game_started` | Only unanswered registrations with contact details |

There are no organizer-facing player-joined, player-left, attendance-response, or
game-closed notification events in the current implementation. In-person players
receive no notifications, including registration confirmations.

FastAPI's lifespan starts a background task that polls every 10 seconds and calls
`process_notifications()` in a worker thread. Processing claims up to ten records
per batch, with a two-minute sending lease for recovery of interrupted sends.
Temporary failures use exponential backoff; the retry policy marks a notification
failed on a permanent error or after five failed attempts. Delivery is not
exactly-once: an interrupted send may be retried after provider acceptance.

Enabled providers are configured with environment flags and shared across forms,
API validation, and delivery. At least one must be enabled. Startup checks for
required configuration variables; this does not prove credentials or targets work.

| Provider | Current implementation and onboarding |
| --- | --- |
| Email | SMTP with STARTTLS; example configuration uses Mailjet. User enters an address; no ownership verification. |
| Discord | `discord.py` bot; `/connect` DMs the user's ID for manual entry. No linked-account records or required ownership proof. |
| SMS | Vonage API; Swedish mobile numbers in international `+46` format only. Number format is validated, not ownership. |
| Telegram | Bot long polling; user presses Start and copies the returned private chat ID. No account-linking or verification records. |

Run only one Telegram polling app instance per bot token and do not configure a
webhook for that bot. Provider-specific configuration and delivery limitations are
documented in `README.md` and `.env.example`. `sent` records provider acceptance,
not confirmed receipt by the player.

## Access and deployment

- Public game pages and the open-game listing need no authentication.
- Private organizer and registration/attendance tokens are bearer credentials:
  protect their links and use HTTPS in public deployment. Numeric registration IDs
  alone do not authorize deletion.
- Docker Compose runs one application container on port 3000. No reverse proxy,
  TLS termination, or external job system is included.
- `.env.example` sets `DATABASE_PATH=/data/gamegather.db`, matching the Compose
  `sqlite_data` volume mounted at `/data`. Without the override, the code defaults
  to `<working directory>/data/gamegather.db`.
- `BASE_URL` determines links in notifications and must be reachable by recipients.
- `GET /health` reports application status; it is not an end-to-end provider check.
- Tests in `app/tests/` cover lifecycle locking, organizer-added registrations,
  notification configuration, and provider behavior. See `AGENTS.md` for test
  conventions and the test command.

## Future ideas — not implemented or committed

The original proposal included broader features. These remain optional design
directions, not descriptions of the current application:

- Organizer-authorized registration removal, if desired, with game-scoped access checks.
- Editing proposals, cancellation/closure, drafts, or reopening, with explicit
  transition and notification rules if adopted.
- QR codes, descriptions, locations, durations, notes, ideal/maximum counts, and
  capacity management.
- Notification ownership verification, reusable accounts/contact endpoints, richer
  Discord interactions, and Web Push/PWA support.
- Automatic attendance refresh and aggregate attendance summaries.
- Convention administration, venue maps, table assignments, calendars, recurring
  games, attendance statistics, and multi-event support.
- A different database, migrations, external workers, or an event bus if concrete
  deployment needs justify the added complexity.

Running BOTC itself, role assignment, script management, and social-network
features remain outside the current coordinator's scope.
