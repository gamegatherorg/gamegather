"""Swedish mobile validation and Vonage SMS API delivery."""

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import phonenumbers

from src.database import get_connection


NUMBER_ERROR = "Enter a Swedish mobile number in +46 format, for example +46701234567."
GSM_CHARACTERS = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "^{}\\[~]|€"
)


def validate_swedish_mobile(target: str) -> str:
    """Accept only strict international-format Swedish mobile numbers."""
    if not re.fullmatch(r"\+467[02369][0-9]{7}", target):
        raise ValueError(NUMBER_ERROR)
    number = phonenumbers.parse(target, None)
    if (
        not phonenumbers.is_valid_number(number)
        or phonenumbers.region_code_for_number(number) != "SE"
        or phonenumbers.number_type(number) != phonenumbers.PhoneNumberType.MOBILE
    ):
        raise ValueError(NUMBER_ERROR)
    return target


def sms_content(item: Any) -> str:
    base_url = os.environ.get("BASE_URL", "http://localhost:3000").rstrip("/")
    with get_connection() as conn:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (item["game_id"],)).fetchone()
    if not game:
        raise ValueError("Notification game no longer exists")
    title = " ".join(game["title"].split())[:40]
    if item["event_type"] == "game_created":
        return f"GameGather: {title} created. Organizer: {base_url}/organizer/games/{quote(game['organizer_token'])}"
    if item["event_type"] == "registration_created":
        return f"GameGather: Registered for {title}. Manage or withdraw (keep link private): {base_url}/attendance/{quote(item['action_token'])}"
    if item["event_type"] == "minimum_players_reached":
        return f"GameGather: {title} has enough players. Gather: {base_url}/organizer/games/{quote(game['organizer_token'])}"
    if item["event_type"] == "gathering_started":
        return f"GameGather: {title} is gathering. Coming or declining? {base_url}/attendance/{quote(item['action_token'])}"
    if item["event_type"] == "game_started":
        return f"GameGather: {title} has started. Attendance responses are now closed. {base_url}/games/{game['id']}"
    raise ValueError("Unsupported SMS notification event")


def send_sms_notification(item: Any) -> None:
    target = validate_swedish_mobile(str(item["notification_target"]))
    text = sms_content(item)
    payload = urlencode({
        "api_key": os.environ["VONAGE_API_KEY"],
        "api_secret": os.environ["VONAGE_API_SECRET"],
        "from": os.environ["VONAGE_SMS_FROM"],
        "to": target[1:],
        "text": text,
        "type": "text" if all(character in GSM_CHARACTERS for character in text) else "unicode",
    }).encode("utf-8")
    request = Request(
        "https://rest.nexmo.com/sms/json", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise RuntimeError(f"Vonage SMS temporary HTTP error {exc.code}") from None
        raise ValueError(f"Vonage SMS rejected request (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise RuntimeError("Vonage SMS connection or response error") from None

    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise RuntimeError("Vonage SMS returned an unexpected response")
    status = str(messages[0].get("status", ""))
    if status == "0":
        return
    # Do not log provider response text: it may contain credentials or recipient data.
    if status in {"1", "5", "13", "29"}:
        raise RuntimeError(f"Vonage SMS temporary failure (status {status})")
    if not status.isdecimal():
        raise RuntimeError("Vonage SMS returned no valid status")
    raise ValueError(f"Vonage SMS rejected message (status {status})")
