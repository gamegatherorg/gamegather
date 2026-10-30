import asyncio
import os
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from src.database import init_db
from src.services.game_service import (
    create_game,
    create_organizer_registration,
    create_registration,
    get_attendance_registration,
    get_game,
    get_organizer_game,
    record_attendance,
    remove_registration,
    start_gathering,
    start_game,
    list_open_games,
)
from src.services.notification_service import process_notifications
from src.services.notification_methods import enabled_methods, validate_notification_configuration


logger = logging.getLogger(__name__)
NOTIFICATION_POLL_SECONDS = 10


async def notification_worker() -> None:
    """Poll the notification queue and send available notifications."""
    while True:
        try:
            await asyncio.to_thread(process_notifications)
        except Exception:
            logger.exception("Notification queue processing failed")
        await asyncio.sleep(NOTIFICATION_POLL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_notification_configuration()
    app.state.telegram_bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "gamegather_notifications_bot")
    app.state.discord_invite_url = os.environ.get("DISCORD_INVITE_URL", "").strip()
    init_db()
    methods = {method.name for method in enabled_methods()}
    async with AsyncExitStack() as services:
        if "discord" in methods:
            from src.services.discord_service import start_discord_service, stop_discord_service

            await start_discord_service()
            services.push_async_callback(stop_discord_service)
        if "telegram" in methods:
            from src.services.telegram_service import TelegramService

            telegram = TelegramService()
            await telegram.start()
            services.push_async_callback(telegram.close)
        worker = asyncio.create_task(notification_worker())
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


app = FastAPI(title="GameGather", lifespan=lifespan)
APP_DIR = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


class CreateGameRequest(BaseModel):
    title: str
    min_players: int
    organizer_name: str
    organizer_notification_method: str
    organizer_notification_target: str


class CreateRegistrationRequest(BaseModel):
    display_name: str
    notification_method: str
    notification_target: str


class CreateOrganizerRegistrationRequest(BaseModel):
    display_name: str


def page_error(request: Request, status_code: int, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"message": message, "status_code": status_code},
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"games": list_open_games()},
    )


@app.get("/games/new", response_class=HTMLResponse)
def new_game_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="new_game.html",
        context={"notification_methods": enabled_methods()},
    )


@app.post("/games/new")
async def create_game_form(request: Request) -> Response:
    form = await request.form()
    title = str(form.get("title", "")).strip()
    organizer_name = str(form.get("organizer_name", "")).strip()
    notification_target = str(form.get("notification_target", "")).strip()
    try:
        min_players = int(form.get("min_players", ""))
        if min_players < 1:
            raise ValueError
    except (TypeError, ValueError):
        return page_error(request, 400, "Minimum players must be a positive whole number.")
    if not title or not organizer_name or not notification_target:
        return page_error(request, 400, "Please complete all required fields.")

    try:
        game = create_game(
            title=title,
            min_players=min_players,
            organizer_name=organizer_name,
            organizer_notification_method=str(form.get("notification_method", "")),
            organizer_notification_target=notification_target,
        )
    except ValueError as exc:
        return page_error(request, 400, str(exc))

    return RedirectResponse(url=game["organizer_url"], status_code=303)


@app.get("/organizer/games/{organizer_token}", response_class=HTMLResponse)
def organizer_game_page(request: Request, organizer_token: str) -> HTMLResponse:
    result = get_organizer_game(organizer_token)
    if not result:
        return page_error(request, 404, "Organizer link not found.")
    return templates.TemplateResponse(
        request=request,
        name="organizer_game.html",
        context={
            **result,
            "registration_count": len(result["registrations"]),
            "minimum_reached": len(result["registrations"]) >= result["game"]["min_players"],
            "organizer_token": organizer_token,
        },
    )


@app.post("/organizer/games/{organizer_token}/register-form")
async def create_organizer_registration_form(request: Request, organizer_token: str) -> Response:
    form = await request.form()
    try:
        create_organizer_registration(organizer_token, str(form.get("display_name", "")))
    except ValueError as exc:
        status_code = 404 if str(exc) == "Organizer link not found" else 400
        return page_error(request, status_code, str(exc))
    return RedirectResponse(url=f"/organizer/games/{quote(organizer_token)}", status_code=303)


@app.post("/organizer/games/{organizer_token}/gather-form")
def gather_game_form(request: Request, organizer_token: str) -> Response:
    try:
        start_gathering(organizer_token)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Organizer link not found" else 400
        return page_error(request, status_code, str(exc))
    return RedirectResponse(url=f"/organizer/games/{quote(organizer_token)}", status_code=303)


@app.post("/organizer/games/{organizer_token}/start-form")
def start_game_form(request: Request, organizer_token: str) -> Response:
    try:
        start_game(organizer_token)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Organizer link not found" else 400
        return page_error(request, status_code, str(exc))
    return RedirectResponse(url=f"/organizer/games/{quote(organizer_token)}", status_code=303)


@app.get("/games/{game_id}")
def public_game_page(request: Request, game_id: int) -> Any:
    result = get_game(game_id)
    if not result:
        if "text/html" in request.headers.get("accept", ""):
            return page_error(request, 404, "Game not found.")
        raise HTTPException(status_code=404, detail="Game not found")
    if "text/html" not in request.headers.get("accept", ""):
        return result
    return templates.TemplateResponse(
        request=request,
        name="game.html",
        context={
            **result, "registration_count": len(result["registrations"]),
            "notification_methods": enabled_methods(),
        },
    )


@app.post("/games/{game_id}/register-form")
async def create_registration_form(request: Request, game_id: int) -> Response:
    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()
    notification_target = str(form.get("notification_target", "")).strip()
    if not display_name or not notification_target:
        return page_error(request, 400, "Please provide your name and notification address.")
    try:
        create_registration(
            game_id=game_id,
            display_name=display_name,
            notification_method=str(form.get("notification_method", "")),
            notification_target=notification_target,
        )
    except ValueError as exc:
        status_code = 404 if str(exc) == "Game not found" else 400
        return page_error(request, status_code, str(exc))
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gamegather"}

@app.get("/games")
def get_all_open_games() -> dict[str, Any]:
    games = list_open_games()
    return {"games": games}

@app.post("/games")
def create_game_endpoint(payload: CreateGameRequest) -> dict[str, Any]:
    try:
        game = create_game(
            title=payload.title,
            min_players=payload.min_players,
            organizer_name=payload.organizer_name,
            organizer_notification_method=payload.organizer_notification_method,
            organizer_notification_target=payload.organizer_notification_target,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "game": game}


@app.post("/games/{game_id}/register")
def register_for_game(game_id: int, payload: CreateRegistrationRequest) -> dict[str, Any]:
    try:
        registration = create_registration(
            game_id=game_id,
            display_name=payload.display_name,
            notification_method=payload.notification_method,
            notification_target=payload.notification_target,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Game not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return {"ok": True, "registration": registration}


@app.delete("/registrations/{registration_id}")
def unregister_for_game(
    registration_id: int,
    registration_token: str = Header(alias="X-Registration-Token"),
) -> dict[str, bool]:
    try:
        remove_registration(registration_id, registration_token)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Registration not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return {"ok": True}


@app.post("/organizer/games/{organizer_token}/register")
def create_organizer_registration_endpoint(
    organizer_token: str,
    payload: CreateOrganizerRegistrationRequest,
) -> dict[str, Any]:
    try:
        registration = create_organizer_registration(organizer_token, payload.display_name)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Organizer link not found" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {"ok": True, "registration": registration}


@app.post("/organizer/games/{organizer_token}/gather")
def start_gathering_endpoint(organizer_token: str) -> dict[str, Any]:
    try:
        result = start_gathering(organizer_token)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Organizer link not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return {"ok": True, **result}


@app.post("/organizer/games/{organizer_token}/start")
def start_game_endpoint(organizer_token: str) -> dict[str, Any]:
    try:
        result = start_game(organizer_token)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Organizer link not found" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.get("/attendance/{attendance_token}", response_class=HTMLResponse)
def attendance_page(
    request: Request,
    attendance_token: str,
    saved: bool = False,
) -> Response:
    registration = get_attendance_registration(attendance_token)
    if not registration:
        return page_error(request, 404, "Attendance link not found.")
    return templates.TemplateResponse(
        request=request,
        name="attendance.html",
        context={
            **registration,
            "attendance_token": attendance_token,
            "saved": saved,
        },
    )


@app.post("/attendance/{attendance_token}")
async def attendance_form(request: Request, attendance_token: str) -> Response:
    form = await request.form()
    response = str(form.get("response", ""))
    if response not in {"coming", "declined"}:
        return page_error(request, 400, "Choose coming or declined.")
    try:
        record_attendance(attendance_token, response)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Attendance link not found" else 400
        return page_error(request, status_code, str(exc))
    return RedirectResponse(url=f"/attendance/{quote(attendance_token)}?saved=true", status_code=303)


@app.post("/attendance/{attendance_token}/withdraw", response_class=HTMLResponse)
def withdraw_registration_form(request: Request, attendance_token: str) -> Response:
    registration = get_attendance_registration(attendance_token)
    if not registration:
        return page_error(request, 404, "Registration link not found.")
    try:
        remove_registration(registration["id"], attendance_token)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Registration not found" else 400
        return page_error(request, status_code, str(exc))
    return templates.TemplateResponse(
        request=request,
        name="withdrawn.html",
        context={"game_title": registration["game_title"], "game_id": registration["game_id"]},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
