"""Минимальный клиент API Дневник.ру без сторонних зависимостей."""

import json
import ssl
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

BASE_URL = "https://api.dnevnik.ru/v2/"
TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class DnevnikError(Exception):
    """Ошибка с безопасным для вывода сообщением, без токена и ответа сервера."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DnevnikClient:
    def __init__(self, token):
        if not token or any(ord(c) <= 32 or ord(c) > 126 for c in token):
            raise DnevnikError("DNEVNIK_TOKEN пуст или содержит недопустимые символы")
        self._token = token
        # Сертификат и имя сервера проверяются; прокси из окружения не используются.
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ssl.create_default_context()),
            NoRedirects(),
        )

    def _get(self, endpoint, params=None):
        url = BASE_URL + endpoint
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        # Дополнительная защита: urllib не переносит этот заголовок в редиректы.
        request.add_unredirected_header("Access-Token", self._token)
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if 300 <= status < 400:
                raise DnevnikError("API вернул перенаправление; запрос остановлен") from None
            if status in (401, 403):
                raise DnevnikError(f"API отказал в доступе (HTTP {status}): проверьте токен и его права") from None
            raise DnevnikError(f"API вернул HTTP {status}") from None
        except (URLError, OSError, HTTPException, ValueError):
            # Исходные исключения могут содержать URL или заголовки запроса.
            raise DnevnikError("Не удалось связаться с API Дневник.ру по HTTPS") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise DnevnikError("Ответ API превышает допустимый размер")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise DnevnikError("API вернул некорректный JSON") from None
        if not isinstance(data, (dict, list)):
            raise DnevnikError("API вернул неожиданный формат данных")
        if isinstance(data, dict) and (
            "error" in data or ("type" in data and "description" in data)
        ):
            raise DnevnikError("API сообщил об ошибке; проверьте доступ и параметры")
        return data

    def get_profile(self):
        data = self._get("users/me")
        if not isinstance(data, dict):
            raise DnevnikError("API вернул неожиданный формат профиля пользователя")
        return data

    def get_schools(self):
        data = self._get("users/me/schools")
        if not isinstance(data, list):
            raise DnevnikError("Не удалось разобрать школы; задайте DNEVNIK_SCHOOL_ID явно")
        return data

    def get_person_homework(self, school_id, person_id, start, end):
        # Числовые ID не позволяют подставить в путь произвольный адрес.
        try:
            school_id, person_id = int(school_id), int(person_id)
            if school_id <= 0 or person_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise DnevnikError("ID школы и ученика должны быть положительными числами") from None
        return self._get(
            f"persons/{person_id}/school/{school_id}/homeworks",
            {"startDate": start, "endDate": end},
        )
