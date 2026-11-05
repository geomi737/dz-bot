#!/usr/bin/env python3
"""
Фетчер ДЗ из Дневник.ру для WhatsApp-бота.

Использование:
  python -m src.dnevnik.fetcher            -> до 10:00 ДЗ на сегодня, с 10:00 на завтра
  python -m src.dnevnik.fetcher --at UNIX  -> выбор даты по времени вопроса (секунды)
  python -m src.dnevnik.fetcher --demo     -> пример форматирования на тестовых данных
  python -m src.dnevnik.fetcher --debug    -> сырой JSON из API в stderr (для отладки полей)

Доступ: OAuth-токен Дневник.ру (DNEVNIK_TOKEN в .env) — получается из уже
залогиненной в браузере сессии (в т.ч. вход через Госуслуги), куки не нужны.
Токен берётся из .env в корне проекта или из переменных окружения.
Запросы идут напрямую в API по HTTPS без сторонних Python-библиотек.
Вход по логину/паролю и перенаправления не поддерживаются.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .client import DnevnikClient, DnevnikError

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    value = v.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    os.environ.setdefault(k.strip(), value)


def target_day(at=None):
    """Дата по времени вопроса в школьном часовом поясе, включая ровно 10:00."""
    try:
        zone = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Moscow"))
    except (ZoneInfoNotFoundError, ValueError):
        raise DnevnikError("Неизвестный BOT_TIMEZONE; пример: Europe/Moscow") from None
    try:
        local = datetime.now(zone) if at is None else datetime.fromtimestamp(at, zone)
    except (ValueError, OverflowError, OSError):
        raise DnevnikError("Некорректное время вопроса (--at)") from None
    return local.date() + timedelta(days=int(local.hour >= 10))


def link_url(value):
    """Публикуем веб-ссылки, в том числе относительные ссылки дневника."""
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if not value.startswith(("https://", "http://", "/", "./", "../")):
        return ""
    try:
        url = urljoin("https://dnevnik.ru/", value)
        parts = urlsplit(url)
        return url if parts.scheme in ("http", "https") and parts.netloc else ""
    except ValueError:
        return ""


class HomeworkHTML(HTMLParser):
    """WhatsApp не рендерит HTML: сохраняем текст, абзацы и адреса ссылок."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")
        if tag == "a":
            self.anchors.append((link_url(dict(attrs).get("href")), len(self.parts)))

    def handle_endtag(self, tag):
        if tag == "a" and self.anchors:
            url, start = self.anchors.pop()
            if url and url not in "".join(self.parts[start:]):
                self.parts.append(f" ({url})")
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    parser = HomeworkHTML()
    # Не трактуем «&copy=»/«&notin=» в обычном URL как HTML-сущности.
    html = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]*;)", "&amp;", str(value))
    parser.feed(html)
    parser.close()
    # Незакрытые <a> тоже не должны терять адрес.
    for url, start in parser.anchors:
        if url and url not in "".join(parser.parts[start:]):
            parser.parts.append(f" ({url})")
    text = "".join(parser.parts).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def subj_name(item):
    s = item.get("subject") or item.get("subjectName") or ""
    if isinstance(s, dict):
        return s.get("name") or s.get("title") or ""
    return str(s)


def hw_text(item):
    text = ""
    for key in ("text", "value", "homeworkText", "description", "task"):
        v = item.get(key)
        if v:
            text = plain_text(v)
            break
    # Если API отдаёт ссылки/файлы отдельно от описания, их URL также включаем.
    for key in ("links", "files", "attachments"):
        entries = item.get(key) or []
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            if isinstance(entry, dict):
                url = link_url(entry.get("url") or entry.get("downloadUrl") or entry.get("href"))
            else:
                url = link_url(entry)
            if url and url not in text:
                text += ("\n" if text else "") + url
    return text


def format_day(date, items):
    """items: список (предмет, текст ДЗ). Собирает сообщение для группы."""
    days_ru = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    head = f"📚 ДЗ на {days_ru[date.weekday()]}, {date.day} {months_ru[date.month - 1]} {date.year}:"
    lines = [head, ""]
    if not items:
        lines.append("ДЗ в дневнике пока нет (или уроки не выставлены).")
    for i, (subj, text) in enumerate(items, 1):
        lines.append(f"{i}. {subj} — {text if text else 'не задано'}")
    return "\n".join(lines)


def fetch_day(dn, school_id, person_id, date, debug=False):
    start = datetime(date.year, date.month, date.day, 0, 0, 0)
    end = datetime(date.year, date.month, date.day, 23, 59, 59)
    data = dn.get_person_homework(school_id, person_id, start.isoformat(), end.isoformat())
    if debug:
        print(json.dumps(data, ensure_ascii=False, default=str)[:4000], file=sys.stderr)
    subjects = {}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("works", data.get("homeworks", data.get("items")))
        for subject in data.get("subjects") or []:
            if not isinstance(subject, dict):
                raise DnevnikError("API вернул неожиданный формат предметов")
            subject_id = subject.get("id_str") or subject.get("id")
            name = subject.get("name") or subject.get("title")
            if subject_id is not None and name:
                subjects[str(subject_id)] = str(name)
    else:
        items = None
    if not isinstance(items, list):
        raise DnevnikError("API вернул неожиданный формат домашних заданий")
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise DnevnikError("API вернул неожиданный формат задания")
        name = subj_name(it) or subjects.get(str(it.get("subjectId")), "")
        if not name:
            raise DnevnikError("Не удалось определить название предмета для задания")
        out.append((name, hw_text(it)))
    return out


def resolve_ids(dn, debug=False):
    """Достаём person_id и school_id: сначала явные переопределения из .env,
    потом из профиля пользователя и списка школ."""
    env_person = os.environ.get("DNEVNIK_PERSON_ID")
    env_school = os.environ.get("DNEVNIK_SCHOOL_ID")
    person_id = int(env_person) if env_person else None
    school_id = int(env_school) if env_school else None
    if not person_id:
        # /users/me/context требует лишнее для бота право FriendsAndRelatives.
        profile = dn.get_profile()
        if debug:
            print(json.dumps(profile, ensure_ascii=False, default=str)[:2000], file=sys.stderr)
        person_id = profile.get("personId_str") or profile.get("personId")
    if not school_id:
        schools = dn.get_schools()
        if schools:
            school = schools[0]
            if isinstance(school, dict):
                school_id = school.get("id_str") or school.get("id") or school.get("schoolId")
            else:
                school_id = school
    return school_id, person_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--at", type=float, help="время вопроса в Unix-секундах")
    args = ap.parse_args()

    load_env()
    try:
        day = target_day(args.at)
    except DnevnikError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.demo:
        demo = [
            ("Алгебра", "№ 1"),
            ("Информатика", hw_text({"text": 'Выполнить <a href="https://example.org/task?part=1&amp;level=2">задание по ссылке</a>'})),
            ("Рус. язык", "Вариант № 2827262; письменно описать фотографию"),
        ]
        print(format_day(day, demo))
        return 0

    token = os.environ.get("DNEVNIK_TOKEN")
    if not token:
        print("ERROR: нужен DNEVNIK_TOKEN — см. .env.example", file=sys.stderr)
        return 2
    try:
        dn = DnevnikClient(token)
        try:
            school_id, person_id = resolve_ids(dn, debug=args.debug)
        except (TypeError, ValueError):
            raise DnevnikError("Проверьте числовые DNEVNIK_SCHOOL_ID / DNEVNIK_PERSON_ID") from None
        if not school_id or not person_id:
            print("ERROR: не удалось определить ID школы/ученика; задайте DNEVNIK_SCHOOL_ID / DNEVNIK_PERSON_ID", file=sys.stderr)
            return 3

        # Строго выбранный календарный день, без перехода на понедельник/после каникул.
        items = fetch_day(dn, school_id, person_id, day, debug=args.debug)
        print(format_day(day, items))
        return 0
    except DnevnikError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
