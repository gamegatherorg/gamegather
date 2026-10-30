# Agent Documentation: GameGather

This document provides essential context for AI agents working on the GameGather codebase. It focuses on non-obvious architecture, conventions, and workflows to minimize trial-and-error.

## Project Overview
GameGather is a Python-based MVP for organizing social games (e.g., Blood on the Clocktower). The core principle is that **interest does not equal commitment**. The application manages proposals, gathering attendance responses, and explicitly starting games. This document describes the current implementation; `design.md` and `MVP_BACKLOG.md` also contain proposals that are not implemented.

## Core Architecture & Data Flow

### Layered Architecture
The project follows a simplified layered architecture:
- **API Layer (`app/src/main.py`)**: FastAPI routes. Handles HTTP requests/responses and Pydantic validation. Should *not* contain business logic.
- **Web UI (`app/templates/`, `app/static/`)**: Server-rendered Jinja2 pages and static assets. A backend operation is not necessarily exposed in the UI.
- **Service Layer (`app/src/services/`)**: Contains the core business logic. This is where state transitions and complex rules reside.
- **Database Layer (`app/src/database.py`)**: Manages SQLite connections and schema initialization.
- **Notification Layer (`app/src/services/notification_service.py`)**: Decouples notification triggering from notification delivery.

### State Machine (Game Status)
The implemented organizer workflow is `open` → `gathering` → `started`:
1. `open`: Games are created directly in this state and accept registrations. Reaching the minimum player count queues an organizer notification; it does not change the stored status or automatically start gathering.
2. `gathering`: The organizer starts gathering using the private organizer token, provided the registration count meets the minimum. Registration and removal are locked. Players can submit or change `coming` / `declined` responses using their private attendance links.
3. `started`: The organizer explicitly starts a gathering game. No minimum confirmed-attendance count is enforced. Attendance responses and further status changes are locked; only unanswered registrations with a notification method receive the game-start notification.

`draft`, `minimum_reached`, and `closed` are not implemented workflow states. The database status column is unrestricted TEXT, and the internal `set_game_status()` helper accepts arbitrary non-`started` values while a game has not started; do not mistake this for supported lifecycle transitions. Use the organizer actions for gathering and starting.

### Registration Removal vs. Declining Attendance
- **Open games**: Players receive a confirmation through their chosen provider with a private `/attendance/{attendance_token}` management link. That page offers withdrawal through an explicit POST to `/attendance/{attendance_token}/withdraw`, which physically deletes the registration. GET requests never withdraw a player.
- **Gathering games**: Players can select **Decline** through their private attendance link. This retains the registration and sets `attendance_response = 'declined'`; it is not deletion. They can change their response while gathering continues.
- **Started games**: Both registration removal and attendance changes are blocked.
- **API authorization**: `DELETE /registrations/{registration_id}` requires `X-Registration-Token` matching that registration's `attendance_token`. Numeric IDs, other players' tokens, and organizer tokens do not authorize deletion. Missing headers return 422, mismatched credentials return 404, and valid credentials after gathering starts return 400.
- **Token lifecycle**: `attendance_token` is generated with `secrets.token_urlsafe(32)` at registration and serves both withdrawal and later attendance responses. Registration and its `registration_created` confirmation are committed atomically. No schema migration is required; existing links still work, but old registrations are not automatically sent new confirmations.

### In-Person Players
- Organizers can add a player by name only while the game is open, using the private organizer token.
- These registrations use `notification_method = 'none'`, an empty notification target, and an immediate `coming` response. They count toward the minimum for gathering but receive no registration, gathering, or game-start notifications.
- Public registration still requires a supported notification method and valid target.

### Key Data Flows
- **Registration**: Player $\rightarrow$ API $\rightarrow$ `game_service` $\rightarrow$ Database (`registrations` table).
- **Notifications**: Logic triggers `queue_notification` $\rightarrow$ Database (`notifications` table with `queued` status). An in-process background task started by FastAPI's lifespan polls every 10 seconds and calls `process_notifications` in a worker thread for real delivery, with retry/failure tracking.
- **Providers**: Email, Discord, SMS (Vonage, Swedish mobile numbers only), and Telegram are implemented. Enabled methods and target validation live in `app/src/services/notification_methods.py`; configuration is documented in `.env.example` and `README.md`. Discord and Telegram services are started and stopped by the application lifespan; Telegram uses long polling.

## Development Workflow

### Environment & Setup
- **Prerequisites**: Docker and Docker Compose.
- **Environment Variables**: Copy `.env.example` to `.env`.
- **Running the App**:
  ```bash
  docker compose up --build
  ```
- **Database**: Uses SQLite. `DATABASE_PATH` overrides the default `<working directory>/data/gamegather.db`. Ensure the configured path is inside the mounted volume when relying on Docker persistence; Compose mounts `sqlite_data` at `/data`.

### Essential Commands
- **Build/Run**: `docker compose up --build`
- **Stop**: `docker compose down`

## Coding Conventions & Patterns

### Database Access
- Always use `get_connection()` from `src.database` to ensure `row_factory = sqlite3.Row` is applied (allows accessing columns by name).
- Follow the existing `with get_connection() as conn:` transaction pattern. SQLite connection context managers commit or roll back transactions; they do not close connections automatically, and `get_connection()` returns a plain SQLite connection.
- **Note**: The `init_db()` function must be called at application startup (handled in `main.py`).

### Service Layer Pattern
- Services should return dictionaries or plain data objects, not raw database rows, to maintain layer separation.
- Business logic involving multiple database steps should be encapsulated in a single service function to ensure atomicity.

### Non-Obvious Gotchas
- **Registration storage**: There are no `is_active` or `left_at` columns. Removal deletes the row; attendance responses retain it. Registration counts include all remaining rows, including in-person players and declined responses.
- **Notification Decoupling**: Never call a third-party delivery API directly inside a game service function. Always use `queue_notification` to record the intent in the database first. Pass the current connection when notification intent must be atomic with the state change, as in `start_game()`. Existing `create_game()` and `start_gathering()` queue notifications after committing their state changes, so do not assume all current notification flows are atomic.
- **Private links**: Organizer and attendance tokens grant access without user accounts. Treat them as secrets; a numeric registration ID is not proof of ownership.
- **SQLite Types**: Dates and times are stored as ISO 8601 strings in TEXT columns.

### Naming & Style
- Follow standard PEP 8 for Python.
- Function names in services should be descriptive of the action (e.g., `create_registration` vs `add_player`).
- Use type hints for all function signatures.

## Testing Strategy
Tests live in `app/tests/` and use Python's `unittest`, mocks, and FastAPI's `TestClient`. With the application dependencies installed, run from the repository root:

```bash
PYTHONPATH=app python -m unittest discover -s app/tests
```

Service tests use temporary SQLite databases by patching `src.database.DB_PATH` and calling `init_db()`. Follow the existing environment-patching and mocked-provider patterns; do not use the development database or send real notifications in tests. Add regression tests for lifecycle restrictions, authorization, registration behavior, and notification targeting when changing those rules.
