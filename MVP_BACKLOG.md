# GameGather MVP Backlog

## Product goal

Help organizers collect game interest, start gathering when enough players have
registered, collect attendance responses, and explicitly start the game.
**Interest is not the same as commitment.**

This backlog separates implemented features from remaining work and optional
proposals. It replaces the original build sequence; proposed features below are
not approved requirements. See `design.md` for the current design and `README.md`
for usage and configuration.

## Implemented

- [x] FastAPI API and server-rendered, mobile-friendly web pages.
- [x] SQLite tables for games, registrations, and queued notifications.
- [x] Create an immediately open game with public and private organizer URLs.
- [x] List open games and view individual public game pages and player counts.
- [x] Register interest with a name and one enabled notification method.
- [x] Atomically queue a player registration confirmation through email, Discord,
  SMS, or Telegram, with a random private management link available from registration.
- [x] Website withdrawal while open through the private link, with explicit POST
  confirmation; the same link handles attendance responses during gathering.
- [x] Add an in-person player from the private organizer page, immediately confirmed
  as coming and without contact details or notifications.
- [x] Alert the organizer when registration reaches the minimum, without changing
  the game state.
- [x] Organizer starts gathering once the registration minimum is met.
- [x] Private attendance links support coming/declined responses and changes during
  gathering; the organizer page displays individual responses when loaded.
- [x] Organizer explicitly starts a gathering game regardless of confirmed count,
  locking changes and notifying only unanswered players with contact details.
- [x] Separate queued notification intent from provider delivery, with an in-app
  worker, retries, interrupted-send recovery, and failure tracking.
- [x] Email (SMTP), Discord, SMS (Vonage, Swedish mobile numbers), and Telegram.
- [x] Environment-based provider selection shared by forms, API, and delivery.
- [x] Docker Compose deployment and SQLite volume configuration via `.env.example`.
- [x] Health endpoint and automated service, route, and provider tests.

The supported lifecycle is **`open` → `gathering` → `started`**. There are no
separate organizer/player account tables, stored minimum-reached state, draft
workflow, or close/cancel workflow.

Registration withdrawal physically removes an open-game registration. Declining
during gathering retains the registration. In-person players have no contact
details and receive no confirmation or management link.

## Completed registration-deletion hardening

- [x] Require a matching private registration token in `X-Registration-Token` for
  `DELETE /registrations/{registration_id}`, preserving open-game-only removal.
  Regression tests cover missing/invalid tokens, cross-registration and cross-game
  tokens, authorized removal, and rejection after gathering starts. Organizer-token
  deletion is not implemented; the player-token option resolves authorization.

## Remaining work

### Reliability and deployment follow-ups

These are recommended follow-ups, not features already provided by Compose:

- [ ] Make game creation and gathering transitions atomic with their notification
  queue inserts, following the existing registration and game-start patterns. Test
  rollback when queuing fails so a successful state change cannot silently lose
  its delivery intent.
- [ ] Document and validate a SQLite backup/restore procedure, including recovery
  of game data and pending notifications.
- [ ] Document a production HTTPS/reverse-proxy setup and verify that `BASE_URL`
  generates reachable links. Traefik and TLS are not currently included.
- [ ] Make test setup reproducible, including the test-only `httpx` dependency
  needed by FastAPI's `TestClient`, which is not listed in `app/requirements.txt`.

## Optional proposals — decisions required

These ideas are retained from the original design or current workflow gaps. They
are not commitments and should be scoped before implementation.

### Player and organizer workflow

- Organizer-authorized removal of registrations, with game-scoped access checks,
  if desired. Current deletion authorization is player-token-only.
- Game editing, cancellation/closure, drafts, or reopening. Define state and
  notification semantics rather than assuming these statuses already exist.
- QR code sharing and richer game details: description, location, duration,
  registration notes, ideal count, maximum count, or waitlists.
- Automatically refreshed attendance and aggregate coming/declined/unanswered counts.

### Notifications and access

- Contact ownership verification and optional reusable account/contact records.
- Rich Discord interactions or Web Push/PWA notifications.
- Delivery diagnostics or an operator workflow for failed notifications.

### Larger-scale coordination

- Convention/event management, venue maps, table assignments, and capacity tracking.
- Calendars, recurring games, attendance statistics, and multi-event support.
- Database migrations, PostgreSQL, external workers, or an event bus only when
  justified by actual operational needs; none is required by the current MVP model.

## Outside the current product scope

- Running BOTC games, assigning roles, or managing scripts.
- Social-network features.

## Validation for future changes

Add automated regression tests for lifecycle restrictions, authorization,
registration behavior, and notification targeting. Use temporary databases and
mocked delivery rather than real player data or provider sends. Test instructions
and coding conventions live in `AGENTS.md`.
