#!/usr/bin/env python3
"""Проверка услуг REDSMS через HTTP API. Нужен только стандартный Python 3.9+.

Документация: https://docs.redsms.ru/http/
Номера чатов в комментариях относятся к выгрузке переписки с поддержкой.

Режимы:
    python3 redsms_test.py --list              список услуг
    python3 redsms_test.py --selftest          офлайн-тесты самого скрипта, в API не ходит
    python3 redsms_test.py --to +7XXXXXXXXXX   имитация: test=true, сообщения не отправляются
    python3 redsms_test.py --to +7XXXXXXXXXX --live   НАСТОЯЩАЯ отправка, она платная

Логин и ключ задаются только переменными окружения REDSMS_LOGIN и REDSMS_API_KEY.
Имитация с test=true описана поддержкой (чаты 32067, 32557), но не документацией, поэтому после каждой
проверки скрипт сравнивает баланс и останавливается, если он уменьшился. Имитация проверяет доступ
и приём запроса, но не доставку: для доставки нужен --live.

Лог пишется в redsms_test_<дата-время>.log рядом со скриптом. API-ключа и secret в нём нет,
номера получателей замаскированы, номер для звонка Wait Call остаётся.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("redsms.test")
log.addHandler(logging.NullHandler())
log.propagate = False

DEFAULT_BASE_URL = "https://cp.redsms.ru/api"
PENDING_STATUSES = {"created", "moderation", "progress"}
POSITIVE_STATUSES = {
    "sms": {"delivered"},
    "tgauth": {"delivered", "read"},
    "wcall": {"wcall_delivered", "delivered"},  # реальный ответ содержит wcall_delivered (чат 33947)
    "fcall": {"delivered"},
    "pushok": {"delivered"},
    "voice": {"delivered"},
    "hlr": {"delivered"},
    "vk": {"delivered", "read"},
    "ping": {"delivered"},
    "viber": {"delivered", "read", "reply"},
}
MIN_VALIDITY = {"sms": 600}  # для остальных каналов 60
DEFAULT_MIN_VALIDITY = 60
MAX_VALIDITY = 86400
POLL_FIELDS = "uuid,status,status_time,child_uuid,type,traffic_type,price,errCode,provider_status,operator"
PROMPT_PREFIX = "  → "  # строка с действием для человека, например куда звонить

HTTP_HINTS = {
    400: "Нет корректных сообщений или сработало ограничение: дубликат, лимит, запрещённый параметр",
    401: "Проверьте логин, ключ и белый список IP в личном кабинете",
    404: "Сообщение не найдено или неверный URL",
    420: "Недостаточно средств",
    500: "Непредвиденная ошибка сервера REDSMS",
}
STATUS_HINTS = {
    "wcall_delivered": "Звонок абонента зафиксирован.",
    "delivered": "Доставлено.",
    "read": "Прочитано.",
    "reply": "Получатель ответил.",
    "reject": "Отклонено модератором.",
    "undelivered": "Не доставлено. Если поставщик сбоит, передайте uuid в поддержку (чат 34202).",
    "timeout": "Время жизни истекло. Для Wait Call: проверьте, что звонок шёл с номера to на номер replacedFrom "
               "в формате +7 (чаты 34135, 33567).",
    "no_money": "Недостаточно средств.",
    "doubled": "Дубликат: сработал лимит в настройках.",
    "limit_exceeded": "Исчерпан лимит из настроек.",
    "bad_number": "Некорректный номер.",
    "stop_list": "Номер в стоп-листе.",
    "route_closed": "Направление закрыто в настройках.",
    "error": "Неизвестная ошибка REDSMS.",
}


class ApiError(Exception):
    def __init__(self, http_status: int, reason: str):
        hint = HTTP_HINTS.get(http_status)
        super().__init__(f"HTTP {http_status}: {reason}" + (f" ({hint})" if hint else ""))
        self.http_status = http_status


class WaitExpired(Exception):
    pass


def mask_phones(value: Any) -> str:
    return re.sub(r"\+?\d{10,15}", lambda m: m.group()[:5] + "***" + m.group()[-2:], str(value))


def mask(value: Any) -> Any:
    """Копия данных для лога: номера получателей скрыты, replacedFrom (номер для звонка) остаётся."""
    if isinstance(value, dict):
        return {k: mask_phones(v) if k == "to" or k.endswith("_to") else "***" if k == "ip" else mask(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def dial_format(number: str) -> str:
    """Без «+» абонент слышит «Неправильно набран номер» (чаты 33567, 33613)."""
    return "+" + number.lstrip("+")


def describe_error(reply: Dict[str, Any]) -> str:
    details = [e.get("message", "") for e in reply.get("errors") or [] if isinstance(e, dict)]
    return "; ".join(filter(None, [reply.get("error_message"), *details])) or "неизвестная ошибка"


class Client:
    def __init__(self, login: str, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0):
        self.login = login
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")  # URL с «/» на конце REDSMS отвечает 404
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                 level: int = logging.INFO) -> Dict[str, Any]:
        ts = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}"
        secret = hashlib.md5((ts + self.api_key).encode("utf-8")).hexdigest()
        headers = {"login": self.login, "ts": ts, "secret": secret, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        log.log(level, "-> %s %s %s", method, path, json.dumps(mask(body), ensure_ascii=False) if body else "")
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            parsed = json.loads(raw)
        except ValueError:
            log.warning("<- HTTP %s за %.0f мс: ответ не JSON, %d байт", status, elapsed_ms, len(raw))
            raise ApiError(status, "ответ не JSON") from None
        if not isinstance(parsed, dict):
            log.warning("<- HTTP %s за %.0f мс: ожидался JSON-объект", status, elapsed_ms)
            raise ApiError(status, "ожидался JSON-объект")
        failed = status != 200 or parsed.get("success") is not True
        log.log(logging.WARNING if failed else level, "<- HTTP %s за %.0f мс: %s", status, elapsed_ms,
                json.dumps(mask(parsed), ensure_ascii=False))
        if failed:
            raise ApiError(status, describe_error(parsed))
        return parsed

    def client_info(self) -> Dict[str, Any]:
        return self._request("GET", "/client/info").get("info") or {}

    def send(self, body: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
        if dry_run:
            return self._request("POST", "/message?fields=all", {**body, "test": True})
        return self._request("POST", "/message", body)

    def get_message(self, message_uuid: str) -> Dict[str, Any]:
        reply = self._request("GET", f"/message/{message_uuid}?fields={POLL_FIELDS}", level=logging.DEBUG)
        item = reply.get("item") or {}
        if not item.get("status"):
            raise ApiError(200, "в ответе нет статуса")
        return item

    def follow(self, message_uuid: str, routes: Sequence[str], wait: float, interval: float = 5.0,
               child_wait: float = 30.0) -> List[Tuple[str, str]]:
        """Ждёт конечный статус и идёт по каскаду через child_uuid. Возвращает пары (канал, статус)."""
        started = time.monotonic()
        chain: List[Tuple[str, str]] = []
        index, current, previous = 0, message_uuid, None
        final_since: Optional[float] = None
        while True:
            now = time.monotonic()
            try:
                item = self.get_message(current)
            except OSError as e:  # сеть бывает нестабильной, GET безопасно повторить до конца ожидания
                log.warning("опрос %s не удался: %s", current, e)
                item = None
            if item is not None:
                status = item["status"]
                route = routes[index]
                if status != previous:
                    log.info("статус %s [%s]: %s -> %s (прошло %.0f с)", current, route, previous, status,
                             now - started)
                    previous = status
                if status not in PENDING_STATUSES:
                    child = item.get("child_uuid")
                    if child:
                        chain.append((route, status))
                        index, current, previous, final_since = min(index + 1, len(routes) - 1), child, None, None
                        continue
                    positive = status in POSITIVE_STATUSES.get(route, {"delivered"})
                    # HLR продолжает каскад при delivered, остальные каналы при неудаче (док. «Каскадные сообщения»)
                    expects_child = index < len(routes) - 1 and (positive if route == "hlr" else not positive)
                    final_since = now if final_since is None else final_since
                    if not expects_child or now - final_since >= child_wait:
                        chain.append((route, status))
                        return chain
            if now - started >= wait:
                raise WaitExpired(f"за {wait:g} с статус не стал конечным, последний: {previous}")
            time.sleep(interval)


@dataclass
class Settings:
    to: str = ""
    to2: Optional[str] = None
    sms_from: Optional[str] = None
    sms_text: Optional[str] = None
    voice_from: Optional[str] = None
    vk_from: Optional[str] = None
    vk_text: Optional[str] = None
    viber_from: Optional[str] = None
    pushok_text: str = "phone"
    expect_traffic: Optional[str] = None  # тип шаблона: текст должен тарифицироваться именно так
    validity: Optional[int] = None
    wait: Optional[float] = None
    interval: float = 5.0
    child_wait: float = 30.0
    stamp: str = field(default_factory=lambda: time.strftime("%H:%M:%S"))


def code(digits: int = 4) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(digits))


def sms_text(s: Settings) -> str:
    return s.sms_text or f"Тест REDSMS {s.stamp}, код {code()}"  # метка времени снижает риск ответа «дубликат»


def optional(**values: Optional[str]) -> Dict[str, str]:
    return {k: v for k, v in values.items() if v}


def body_sms(s: Settings) -> Dict[str, Any]:
    return {"route": "sms", "to": s.to, "text": sms_text(s), **optional(**{"from": s.sms_from})}


def body_sms_scheduled(s: Settings) -> Dict[str, Any]:
    return {**body_sms(s), "startTime": int(time.time()) + 3600}


def body_cascade_hlr_sms(s: Settings) -> Dict[str, Any]:
    return {"route": "hlr,sms", "to": s.to, "hlr_validity": 120, "sms_text": sms_text(s),
            "sms_validity": 600, **optional(sms_from=s.sms_from)}


def body_cascade_pushok_sms(s: Settings) -> Dict[str, Any]:
    return {"route": "pushok,sms", "to": s.to, "pushok_validity": 60, "pushok_text": s.pushok_text,
            "sms_text": sms_text(s), "sms_validity": 600, **optional(sms_from=s.sms_from)}


def body_cascade_tgauth_sms(s: Settings) -> Dict[str, Any]:
    return {"route": "tgauth,sms", "to": s.to, "tgauth_text": code(6), "tgauth_validity": 300,
            "sms_text": sms_text(s), "sms_validity": 600, **optional(sms_from=s.sms_from)}


def body_mass(s: Settings) -> Dict[str, Any]:
    return {"route": "sms", "to": f"{s.to},{s.to2}", "text": sms_text(s), **optional(**{"from": s.sms_from})}


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    routes: Tuple[str, ...]
    build: Callable[[Settings], Dict[str, Any]]
    validity: int  # срок жизни для одного канала, для каскада сумма сроков: от неё зависит ожидание
    needs: Tuple[Tuple[str, str], ...] = ()  # (поле Settings, флаг командной строки)
    prompt: str = ""
    russia_only: bool = False
    extra: bool = False  # только по явному имени в --services
    live_ok: bool = True
    skip_note: str = ""


SCENARIOS: Tuple[Scenario, ...] = (
    Scenario("sms", "SMS", ("sms",), body_sms, 600),
    Scenario("sms_scheduled", "SMS отложенное (startTime)", ("sms",), body_sms_scheduled, 600, live_ok=False,
             skip_note="только имитация: настоящее сообщение уйдёт через час, и отменить его нельзя"),
    Scenario("tgauth", "Telegram Gateway", ("tgauth",),
             lambda s: {"route": "tgauth", "to": s.to, "text": code(6)}, 300,
             prompt="Проверьте Telegram: должен прийти код {text}."),
    # text не передаём: режим с диктовкой кода снят с поддержки (чаты 34367, 34090, 33099)
    Scenario("wcall", "Wait Call", ("wcall",), lambda s: {"route": "wcall", "to": s.to}, 360,
             prompt="Позвоните с {to} на {call_number}.", russia_only=True),
    Scenario("fcall", "Flash Call", ("fcall",), lambda s: {"route": "fcall", "to": s.to, "text": code()}, 120,
             prompt="Ждите входящий звонок: номер должен заканчиваться на {text}."),
    Scenario("pushok", "PushOK (SIM-Push)", ("pushok",),
             lambda s: {"route": "pushok", "to": s.to, "text": s.pushok_text}, 60,
             prompt="Подтвердите запрос на телефоне в течение 60 с.", russia_only=True),
    Scenario("voice", "Voice (звонок)", ("voice",),
             lambda s: {"route": "voice", "from": s.voice_from, "to": s.to,
                        "text": f"Проверка голосового сообщения. Код {' '.join(code())}"}, 300,
             needs=(("voice_from", "--voice-from"),), prompt="Ждите звонок и примите его."),
    Scenario("hlr", "HLR", ("hlr",), lambda s: {"route": "hlr", "to": s.to}, 120),
    Scenario("vk", "VK", ("vk",),
             lambda s: {"route": "vk", "from": s.vk_from, "to": s.to, "text": s.vk_text}, 600,
             needs=(("vk_from", "--vk-from"), ("vk_text", "--vk-text"))),
    Scenario("cascade_hlr_sms", "Каскад HLR → SMS", ("hlr", "sms"), body_cascade_hlr_sms, 720),
    Scenario("cascade_pushok_sms", "Каскад PushOK → SMS", ("pushok", "sms"), body_cascade_pushok_sms, 660,
             prompt="Подтвердите запрос на телефоне или дождитесь SMS.", russia_only=True),
    Scenario("cascade_tgauth_sms", "Каскад Telegram → SMS", ("tgauth", "sms"), body_cascade_tgauth_sms, 900),
    Scenario("mass", "Массовая отправка SMS", ("sms",), body_mass, 600, needs=(("to2", "--to2"),)),
    # Нет отдельной страницы в документации; в чатах Ping описан как временно отключённый (33735)
    Scenario("ping", "Ping", ("ping",), lambda s: {"route": "ping", "to": s.to}, 120, extra=True),
    Scenario("viber", "Viber", ("viber",),
             lambda s: {"route": "viber", "to": s.to, "text": sms_text(s), **optional(**{"from": s.viber_from})},
             600, extra=True),
)
BY_NAME = {s.name: s for s in SCENARIOS}


@dataclass
class Result:
    name: str
    title: str
    outcome: str  # OK, ОШИБКА, ПРОПУЩЕНО
    detail: str = ""
    uuids: Tuple[str, ...] = ()


def effective_validity(scenario: Scenario, settings: Settings) -> int:
    return settings.validity if settings.validity is not None and len(scenario.routes) == 1 else scenario.validity


def build_body(scenario: Scenario, settings: Settings) -> Dict[str, Any]:
    body = scenario.build(settings)
    if len(scenario.routes) == 1:
        route = scenario.routes[0]
        validity = effective_validity(scenario, settings)
        minimum = MIN_VALIDITY.get(route, DEFAULT_MIN_VALIDITY)
        if not minimum <= validity <= MAX_VALIDITY:
            raise ValueError(f"validity для {route} должен быть от {minimum} до {MAX_VALIDITY} секунд")
        body["validity"] = validity
    return body


def skip_reason(scenario: Scenario, settings: Settings, live: bool) -> Optional[str]:
    if live and not scenario.live_ok:
        return scenario.skip_note
    missing = [flag for attr, flag in scenario.needs if not getattr(settings, attr)]
    if missing:
        return "нужно указать " + ", ".join(missing)
    if scenario.russia_only and not settings.to.startswith("+7"):
        return "услуга работает только для номеров РФ"
    return None


def check_dry(scenario: Scenario, client: Client, settings: Settings, body: Dict[str, Any]) -> Result:
    reply = client.send(body, dry_run=True)
    items = reply.get("items") or []
    if reply.get("errors") or not items or not all(i.get("uuid") for i in items):
        return Result(scenario.name, scenario.title, "ОШИБКА", describe_error(reply) if reply.get("errors")
                      else "в ответе нет сообщений с uuid")
    first = items[0]
    info = {k: first.get(k) for k in ("operator", "country", "traffic_type", "price") if first.get(k) not in (None, "")}
    detail = "запрос принят, сообщение не отправлялось"
    if info:
        detail += "; " + ", ".join(f"{k}={v}" for k, v in info.items())
    if settings.expect_traffic and scenario.routes == ("sms",):
        actual = first.get("traffic_type")
        if not actual:
            detail += f"; тип тарификации не вернулся, совпадение с шаблоном ({settings.expect_traffic}) не подтверждено"
        elif actual != settings.expect_traffic:
            # Текст не совпал с зарегистрированным шаблоном: по умолчанию сообщение рекламное (чаты 33750, 33973)
            return Result(scenario.name, scenario.title, "ОШИБКА",
                          f"{detail}; ожидался тип {settings.expect_traffic}. Текст не совпал с шаблоном или "
                          "шаблон не действует у этого оператора")
    return Result(scenario.name, scenario.title, "OK", detail)


def check_live(scenario: Scenario, client: Client, settings: Settings, body: Dict[str, Any],
               out: Callable[[str], None]) -> Result:
    reply = client.send(body, dry_run=False)
    items = reply.get("items") or []
    if not items or not all(i.get("uuid") for i in items):
        return Result(scenario.name, scenario.title, "ОШИБКА", "в ответе нет сообщений с uuid")
    uuids = tuple(i["uuid"] for i in items)
    call_number = items[0].get("replacedFrom")
    if scenario.name == "wcall":
        if not call_number:
            return Result(scenario.name, scenario.title, "ОШИБКА", "в ответе нет replacedFrom", uuids)
        call_number = dial_format(call_number)
    if scenario.prompt:
        out(PROMPT_PREFIX + scenario.prompt.format(to=settings.to, call_number=call_number, text=body.get("text", "")))
    wait = settings.wait if settings.wait is not None else effective_validity(scenario, settings) + 60
    # Статусы приходят с задержкой, поэтому ждём дольше срока жизни
    problems: List[str] = []
    chains: List[str] = []
    for message_uuid in uuids:
        try:
            chain = client.follow(message_uuid, scenario.routes, wait, settings.interval, settings.child_wait)
        except WaitExpired as e:
            problems.append(str(e))
            continue
        chains.append(" → ".join(f"{route}:{status}" for route, status in chain))
        route, status = chain[-1]
        unfinished_hlr = route == "hlr" and len(scenario.routes) > 1
        if status not in POSITIVE_STATUSES.get(route, {"delivered"}):
            problems.append(STATUS_HINTS.get(status, "Статус не описан в документации."))
        elif unfinished_hlr:
            problems.append("HLR доставлен, но следующий канал не создан")
    detail = "; ".join(chains + problems)
    for error in reply.get("errors") or []:
        problems.append(str(mask(error)))
        detail = "; ".join([detail, str(mask(error))])
    return Result(scenario.name, scenario.title, "ОШИБКА" if problems else "OK", detail, uuids)


def run_scenario(scenario: Scenario, client: Client, settings: Settings, live: bool,
                 out: Callable[[str], None]) -> Result:
    reason = skip_reason(scenario, settings, live)
    if reason:
        return Result(scenario.name, scenario.title, "ПРОПУЩЕНО", reason)
    try:
        body = build_body(scenario, settings)
        return check_live(scenario, client, settings, body, out) if live else check_dry(scenario, client, settings, body)
    except (ApiError, OSError, ValueError) as e:
        return Result(scenario.name, scenario.title, "ОШИБКА", str(e))


def read_balance(client: Client) -> Optional[float]:
    try:
        balance = client.client_info().get("balance")
    except (ApiError, OSError):
        return None
    return float(balance) if isinstance(balance, (int, float)) else None


def run_all(client: Client, settings: Settings, scenarios: Sequence[Scenario], live: bool,
            out: Callable[[str], None] = print, results: Optional[List[Result]] = None) -> List[Result]:
    """Результаты дописываются в results по мере готовности, чтобы окно могло показывать их сразу."""
    results = [] if results is None else results
    try:
        info = client.client_info()
    except (ApiError, OSError) as e:
        out(f"Доступ: ОШИБКА, {e}")
        results.append(Result("client_info", "Доступ и баланс", "ОШИБКА", str(e)))
        return results
    start_balance = info.get("balance")
    detail = f"логин {info.get('login')}, баланс {start_balance}, договор активен: {info.get('active')}"
    results.append(Result("client_info", "Доступ и баланс", "OK" if info.get("active") is not False else "ОШИБКА",
                          detail if info.get("active") is not False else detail + " (нет полного доступа)"))
    out(f"Доступ и баланс: {results[0].outcome}, {results[0].detail}")

    balance = float(start_balance) if isinstance(start_balance, (int, float)) else None
    stopped = False
    for scenario in scenarios:
        if stopped:
            results.append(Result(scenario.name, scenario.title, "ПРОПУЩЕНО", "остановлено: баланс уменьшился"))
            continue
        out(f"{scenario.title}...")
        log.info("=== %s", scenario.name)
        result = run_scenario(scenario, client, settings, live, out)
        log.info("итог %s: %s %s", scenario.name, result.outcome, result.detail)
        results.append(result)
        out(f"  {result.outcome}" + (f": {result.detail}" if result.detail else ""))
        if not live and result.outcome != "ПРОПУЩЕНО" and balance is not None:
            current = read_balance(client)
            if current is not None and current < balance:
                results.append(Result("balance_guard", "Защита баланса", "ОШИБКА",
                                      f"баланс уменьшился с {balance} до {current} после «{scenario.title}». "
                                      "Имитация могла быть платной, либо на аккаунте идут другие отправки."))
                out(f"  СТОП: {results[-1].detail}")
                stopped = True
    final = read_balance(client)
    if isinstance(start_balance, (int, float)) and final is not None:
        out(f"\nБаланс: было {start_balance}, стало {final}, разница {final - start_balance:+.2f}")
    return results


def print_summary(results: Sequence[Result], out: Callable[[str], None] = print) -> None:
    out("\nИтог:")
    width = max(len(r.title) for r in results)
    for r in results:
        out(f"  {r.outcome:<10} {r.title:<{width}}  {r.detail}")
        if r.outcome == "ОШИБКА" and r.uuids:
            out(f"  {'':<10} uuid для поддержки: {', '.join(r.uuids)}")


def confirm_live(scenarios: Sequence[Scenario], settings: Settings, assume_yes: bool, is_tty: bool,
                 ask: Callable[[str], str], out: Callable[[str], None] = print) -> bool:
    runnable = [s for s in scenarios if not skip_reason(s, settings, live=True)]
    out(f"НАСТОЯЩАЯ ОТПРАВКА на {settings.to}" + (f" и {settings.to2}" if settings.to2 else "") + ". Запросы платные:")
    for s in runnable:
        out(f"  - {s.title}")
    if assume_yes:
        return True
    if not is_tty:
        out("Нет терминала для подтверждения. Добавьте --yes, если отправка действительно нужна.")
        return False
    return ask("Введите ДА, чтобы отправить: ").strip() == "ДА"


def select_scenarios(names: Optional[str]) -> List[Scenario]:
    if not names:
        return [s for s in SCENARIOS if not s.extra]
    selected = []
    for name in (n.strip() for n in names.split(",") if n.strip()):
        if name not in BY_NAME:
            raise ValueError(f"неизвестная услуга: {name}. Список: --list")
        selected.append(BY_NAME[name])
    return selected


def setup_log_file() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), time.strftime("redsms_test_%Y%m%d-%H%M%S.log"))
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    return path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Проверка услуг REDSMS. Без --live сообщения не отправляются (test=true).")
    p.add_argument("--to", help="номер получателя в формате +7XXXXXXXXXX")
    p.add_argument("--services", help="услуги через запятую, по умолчанию все, кроме ping и viber")
    p.add_argument("--live", action="store_true", help="настоящая платная отправка")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение для --live")
    p.add_argument("--list", action="store_true", help="показать услуги и выйти")
    p.add_argument("--selftest", action="store_true", help="офлайн-тесты скрипта и выход")
    p.add_argument("--to2", help="второй номер для проверки mass")
    p.add_argument("--sms-from", help="имя отправителя SMS, иначе имя по умолчанию из настроек")
    p.add_argument("--sms-text", help="текст SMS, например под зарегистрированный шаблон")
    p.add_argument("--voice-from", help="имя отправителя Voice из личного кабинета")
    p.add_argument("--vk-from", help="имя отправителя VK")
    p.add_argument("--vk-text", help="текст VK по зарегистрированному шаблону")
    p.add_argument("--viber-from", help="имя отправителя Viber")
    p.add_argument("--pushok-text", choices=("enter", "reg", "phone"), default="phone", help="текст окна PushOK")
    p.add_argument("--expect-traffic", choices=("auth", "service", "info", "transact", "market_template"),
                   help="в имитации проверить, что SMS тарифицируется как этот тип шаблона")
    p.add_argument("--validity", type=int, help="срок жизни, секунд, для одноканальных услуг (SMS от 600, др. от 60)")
    p.add_argument("--wait", type=float, help="сколько ждать конечный статус, секунд (по умолчанию срок жизни + 60)")
    p.add_argument("--interval", type=float, default=5.0, help="интервал опроса статуса, секунд")
    p.add_argument("--base-url", default=os.environ.get("REDSMS_API_BASE", DEFAULT_BASE_URL), help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.list:
        for s in SCENARIOS:
            marks = [note for note, on in (("только по имени", s.extra), ("только имитация", not s.live_ok)) if on]
            needs = [flag for _, flag in s.needs]
            line = f"{s.name:<20} {s.title}"
            if marks or needs:
                line += f"  ({', '.join(marks + ['нужно ' + n for n in needs])})"
            print(line)
        return 0

    login, api_key = os.environ.get("REDSMS_LOGIN"), os.environ.get("REDSMS_API_KEY")
    if not login or not api_key:
        print("Задайте переменные окружения REDSMS_LOGIN и REDSMS_API_KEY.", file=sys.stderr)
        return 2
    if not args.to or not re.fullmatch(r"\+\d{10,15}", args.to):
        print("Укажите номер: --to +7XXXXXXXXXX", file=sys.stderr)
        return 2
    if args.to2 and not re.fullmatch(r"\+\d{10,15}", args.to2):
        print("Неверный формат --to2, нужен +7XXXXXXXXXX", file=sys.stderr)
        return 2
    try:
        scenarios = select_scenarios(args.services)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    settings = Settings(to=args.to, to2=args.to2, sms_from=args.sms_from, sms_text=args.sms_text,
                        voice_from=args.voice_from, vk_from=args.vk_from, vk_text=args.vk_text,
                        viber_from=args.viber_from, pushok_text=args.pushok_text, expect_traffic=args.expect_traffic,
                        validity=args.validity,
                        wait=args.wait, interval=args.interval)
    if args.live and not confirm_live(scenarios, settings, args.yes, sys.stdin.isatty(), input):
        print("Отправка отменена, запросов не было.")
        return 2

    log_path = setup_log_file()
    print("Режим: НАСТОЯЩАЯ ОТПРАВКА" if args.live else "Режим: имитация (test=true), сообщения не отправляются")
    results = run_all(Client(login, api_key, base_url=args.base_url), settings, scenarios, args.live)
    print_summary(results)
    print(f"\nЛог: {log_path}")
    return 1 if any(r.outcome == "ОШИБКА" for r in results) else 0


# --- Офлайн-тесты: python3 redsms_test.py --selftest ------------------------------------------------------------

TO = "+79993332211"
TO2 = "+79993332212"
CALL_NUMBER = "+79160001122"
UUID = "11111111-2222-11f1-8000-0242c0a80001"


def sent(message_uuid: str = UUID, **extra: Any) -> Tuple[int, Dict[str, Any]]:
    return 200, {"items": [{"uuid": message_uuid, "status": "progress", "status_time": 1790589700, "to": TO, **extra}],
                 "errors": [], "count": 1, "success": True}


# Реальные ответы из чатов 34090 и 33947, номера заменены на тестовые
TEXT_REJECTED = (200, {"error_message": "error while creating messages",
                       "errors": [{"to": TO, "message": "service temporarily unavailable",
                                   "uuid": "33333333-4444-11f1-8000-0242c0a80002"}], "success": False})
NOT_FOUND = (200, {"error_message": "Не найдено", "success": False})
# Реальный ответ из живого запуска wcall 06.11.2026, номер заменён на тестовый
BAD_NUMBER = (400, {"error_message": "error while creating messages",
                    "errors": [{"to": TO, "message": "bad number rejected"}], "success": False})


class FakeRedsms:
    """POST отвечает из очереди send_replies, GET статусов идёт по списку statuses[uuid], последний повторяется."""

    def __init__(self) -> None:
        self.send_replies: List[Tuple[int, Any]] = []
        self.statuses: Dict[str, List[Dict[str, Any]]] = {}
        self.balances: List[float] = [100.0]
        self.get_error: Optional[Tuple[int, Any]] = None
        self.client_info_reply: Optional[Tuple[int, Any]] = None
        self.calls: List[Dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self) -> Tuple[int, Any]:
                path = self.path.split("?")[0]
                if path == "/api/client/info":
                    if fake.client_info_reply:
                        return fake.client_info_reply
                    balance = fake.balances.pop(0) if len(fake.balances) > 1 else fake.balances[0]
                    return 200, {"info": {"login": "user", "balance": balance, "active": True}, "success": True}
                if self.command == "POST":
                    return fake.send_replies.pop(0) if fake.send_replies else sent()
                if fake.get_error:
                    return fake.get_error
                queue = fake.statuses.get(path.rsplit("/", 1)[1], [{"status": "delivered"}])
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                return 200, {"item": item, "success": True}

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.calls.append({"method": self.command, "path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": json.loads(self.rfile.read(length)) if length else None})
                status, data = self._reply()
                raw = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/api"

    def posts(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRedsms()
        self.addCleanup(self.fake.close)
        self.client = Client("user", "api-key", base_url=self.fake.base_url, timeout=5)
        self.settings = Settings(to=TO, interval=0, child_wait=0.02, wait=5)
        self.output: List[str] = []

    def run_one(self, name: str, live: bool = True) -> Result:
        return run_scenario(BY_NAME[name], self.client, self.settings, live, self.output.append)


class RequestBodyTest(Base):
    def test_single_channel_bodies_match_docs(self) -> None:
        s = replace(self.settings, sms_from="Company", voice_from="VoiceName", vk_from="VkName", vk_text="Шаблон")
        expected = {
            "sms": {"route": "sms", "to": TO, "from": "Company", "validity": 600},
            "tgauth": {"route": "tgauth", "to": TO, "validity": 300},
            "wcall": {"route": "wcall", "to": TO, "validity": 360},
            "fcall": {"route": "fcall", "to": TO, "validity": 120},
            "pushok": {"route": "pushok", "to": TO, "text": "phone", "validity": 60},
            "voice": {"route": "voice", "from": "VoiceName", "to": TO, "validity": 300},
            "hlr": {"route": "hlr", "to": TO, "validity": 120},
            "vk": {"route": "vk", "from": "VkName", "to": TO, "text": "Шаблон", "validity": 600},
            "ping": {"route": "ping", "to": TO, "validity": 120},
        }
        for name, fields in expected.items():
            with self.subTest(name):
                body = build_body(BY_NAME[name], s)
                self.assertEqual({k: v for k, v in body.items() if k in fields}, fields)

    def test_wcall_never_sends_text(self) -> None:
        self.assertNotIn("text", build_body(BY_NAME["wcall"], self.settings))

    def test_codes_have_documented_format(self) -> None:
        self.assertRegex(build_body(BY_NAME["fcall"], self.settings)["text"], r"^\d{4}$")
        self.assertRegex(build_body(BY_NAME["tgauth"], self.settings)["text"], r"^\d{4,8}$")

    def test_cascade_bodies_use_channel_prefixes(self) -> None:
        body = build_body(BY_NAME["cascade_hlr_sms"], replace(self.settings, sms_from="Company"))
        self.assertEqual(body["route"], "hlr,sms")
        self.assertEqual(body["hlr_validity"], 120)
        self.assertEqual(body["sms_from"], "Company")
        self.assertNotIn("validity", body)
        self.assertEqual(build_body(BY_NAME["cascade_pushok_sms"], self.settings)["route"], "pushok,sms")
        self.assertRegex(build_body(BY_NAME["cascade_tgauth_sms"], self.settings)["tgauth_text"], r"^\d{4,8}$")

    def test_mass_joins_numbers(self) -> None:
        self.assertEqual(build_body(BY_NAME["mass"], replace(self.settings, to2=TO2))["to"], f"{TO},{TO2}")

    def test_scheduled_sms_is_in_future_within_limit(self) -> None:
        start = build_body(BY_NAME["sms_scheduled"], self.settings)["startTime"]
        self.assertGreater(start, time.time())
        self.assertLess(start, time.time() + 7 * 86400)

    def test_validity_limits(self) -> None:
        for name, value, ok in (("sms", 599, False), ("sms", 600, True), ("wcall", 59, False), ("wcall", 60, True),
                                ("hlr", 86400, True), ("hlr", 86401, False)):
            with self.subTest(f"{name} {value}"):
                s = replace(self.settings, validity=value)
                if ok:
                    self.assertEqual(build_body(BY_NAME[name], s)["validity"], value)
                else:
                    with self.assertRaises(ValueError):
                        build_body(BY_NAME[name], s)

    def test_bad_validity_sends_nothing(self) -> None:
        self.settings = replace(self.settings, validity=59)
        self.assertEqual(self.run_one("wcall").outcome, "ОШИБКА")
        self.assertEqual(self.fake.posts(), [])

    def test_signature_and_no_key_on_wire(self) -> None:
        self.client.send({"route": "hlr", "to": TO}, dry_run=False)
        self.client.send({"route": "hlr", "to": TO}, dry_run=False)
        first, second = (c["headers"] for c in self.fake.calls)
        self.assertEqual(first["login"], "user")
        self.assertEqual(first["secret"], hashlib.md5((first["ts"] + "api-key").encode()).hexdigest())
        self.assertNotEqual(first["ts"], second["ts"])
        self.assertNotIn("api-key", json.dumps(first))


class DryRunTest(Base):
    def test_dry_run_sets_test_flag_and_never_polls_status(self) -> None:
        result = self.run_one("sms", live=False)
        self.assertEqual(result.outcome, "OK")
        post = self.fake.posts()[0]
        self.assertIs(post["body"]["test"], True)
        self.assertEqual(post["path"], "/api/message?fields=all")
        self.assertEqual([c for c in self.fake.calls if c["method"] == "GET"], [])

    def test_live_has_no_test_flag(self) -> None:
        self.run_one("hlr")
        self.assertNotIn("test", self.fake.posts()[0]["body"])

    def test_dry_run_shows_operator(self) -> None:
        self.fake.send_replies = [sent(operator="mts", country="RU", price=7)]
        self.assertIn("operator=mts", self.run_one("sms", live=False).detail)

    def test_expected_traffic_type_matches(self) -> None:
        self.settings = replace(self.settings, expect_traffic="auth")
        self.fake.send_replies = [sent(traffic_type="auth")]
        result = self.run_one("sms", live=False)
        self.assertEqual(result.outcome, "OK")
        self.assertIn("traffic_type=auth", result.detail)

    def test_market_traffic_when_template_expected_is_error(self) -> None:
        self.settings = replace(self.settings, expect_traffic="auth")
        self.fake.send_replies = [sent(traffic_type="market")]
        result = self.run_one("sms", live=False)
        self.assertEqual(result.outcome, "ОШИБКА")
        self.assertIn("не совпал с шаблоном", result.detail)

    def test_missing_traffic_type_is_not_claimed_as_match(self) -> None:
        self.settings = replace(self.settings, expect_traffic="auth")
        result = self.run_one("sms", live=False)
        self.assertEqual(result.outcome, "OK")
        self.assertIn("не подтверждено", result.detail)

    def test_expected_traffic_ignored_for_other_channels(self) -> None:
        self.settings = replace(self.settings, expect_traffic="auth")
        self.fake.send_replies = [sent(traffic_type="fcall4")]
        self.assertEqual(self.run_one("fcall", live=False).outcome, "OK")

    def test_dry_run_rejection_is_error(self) -> None:
        self.fake.send_replies = [TEXT_REJECTED]
        result = self.run_one("wcall", live=False)
        self.assertEqual(result.outcome, "ОШИБКА")
        self.assertIn("service temporarily unavailable", result.detail)

    def test_balance_drop_stops_remaining_checks(self) -> None:
        self.fake.balances = [100.0, 93.0, 93.0]
        results = run_all(self.client, self.settings, [BY_NAME["hlr"], BY_NAME["sms"]], False, self.output.append)
        outcomes = {r.name: r.outcome for r in results}
        self.assertEqual(outcomes["balance_guard"], "ОШИБКА")
        self.assertEqual(outcomes["sms"], "ПРОПУЩЕНО")
        self.assertEqual(len(self.fake.posts()), 1)

    def test_stable_balance_runs_everything(self) -> None:
        results = run_all(self.client, self.settings, [BY_NAME["hlr"], BY_NAME["sms"]], False, self.output.append)
        self.assertEqual([r.outcome for r in results], ["OK", "OK", "OK"])

    def test_bad_auth_stops_before_any_message(self) -> None:
        self.fake.client_info_reply = (401, {"error_message": "unauthorized", "success": False})
        results = run_all(self.client, self.settings, [BY_NAME["sms"]], False, self.output.append)
        self.assertEqual(results[0].outcome, "ОШИБКА")
        self.assertIn("белый список IP", results[0].detail)
        self.assertEqual(self.fake.posts(), [])


class LiveChecksTest(Base):
    def test_every_service_passes_on_its_success_status(self) -> None:
        self.settings = replace(self.settings, voice_from="V", vk_from="V", vk_text="T", to2=TO2)
        for name in ("sms", "tgauth", "fcall", "pushok", "voice", "hlr", "vk", "ping", "viber", "mass"):
            with self.subTest(name):
                self.assertEqual(self.run_one(name).outcome, "OK")

    def test_wcall_shows_number_and_passes_on_real_status(self) -> None:
        # В живом запуске replacedFrom пришёл без плюса, абоненту показываем с ним
        self.fake.send_replies = [sent(replacedFrom="79160001122")]
        self.fake.statuses[UUID] = [{"status": "progress"}, {"status": "wcall_delivered"}]
        result = self.run_one("wcall")
        self.assertEqual(result.outcome, "OK")
        self.assertIn(f"Позвоните с {TO} на {CALL_NUMBER}.", "\n".join(self.output))

    def test_wcall_without_call_number_fails(self) -> None:
        self.assertIn("replacedFrom", self.run_one("wcall").detail)

    def test_failed_final_statuses_are_errors_with_hint(self) -> None:
        for status, hint in (("timeout", "replacedFrom"), ("undelivered", "поставщик"), ("no_money", "средств")):
            with self.subTest(status):
                self.fake.send_replies = [sent(message_uuid=status, replacedFrom=CALL_NUMBER)]
                self.fake.statuses[status] = [{"status": status}]
                result = self.run_one("wcall")
                self.assertEqual(result.outcome, "ОШИБКА")
                self.assertIn(hint, result.detail)
                self.assertEqual(result.uuids, (status,))

    def test_wcall_delivered_is_not_success_for_other_channels(self) -> None:
        self.fake.statuses[UUID] = [{"status": "wcall_delivered"}]
        self.assertEqual(self.run_one("sms").outcome, "ОШИБКА")

    def test_wait_expired_is_error(self) -> None:
        self.settings = replace(self.settings, wait=0)
        self.fake.statuses[UUID] = [{"status": "progress"}]
        result = self.run_one("hlr")
        self.assertEqual(result.outcome, "ОШИБКА")
        self.assertIn("progress", result.detail)

    def test_real_bad_number_reply_is_error_with_reason(self) -> None:
        self.fake.send_replies = [BAD_NUMBER]
        result = self.run_one("wcall")
        self.assertEqual(result.outcome, "ОШИБКА")
        self.assertIn("HTTP 400", result.detail)
        self.assertIn("bad number rejected", result.detail)

    def test_real_not_found_is_error(self) -> None:
        self.fake.get_error = NOT_FOUND
        self.assertIn("Не найдено", self.run_one("hlr").detail)

    def test_http_400_420_and_non_json(self) -> None:
        for reply, text in (((400, {"error_message": "x", "success": False}), "ограничение"),
                            ((420, {"error_message": "x", "success": False}), "средств"),
                            ((502, b"<html>"), "не JSON")):
            with self.subTest(text):
                self.fake.send_replies = [reply]
                result = self.run_one("hlr")
                self.assertEqual(result.outcome, "ОШИБКА")
                self.assertIn(text, result.detail)

    def test_partial_mass_failure_is_error(self) -> None:
        self.settings = replace(self.settings, to2=TO2)
        status, body = sent()
        self.fake.send_replies = [(status, {**body, "errors": [{"to": TO2, "message": "bad number rejected"}]})]
        result = self.run_one("mass")
        self.assertEqual(result.outcome, "ОШИБКА")
        self.assertIn("bad number rejected", result.detail)
        self.assertNotIn(TO2, result.detail)

    def test_mass_checks_every_message(self) -> None:
        self.settings = replace(self.settings, to2=TO2)
        self.fake.send_replies = [(200, {"items": [{"uuid": "a"}, {"uuid": "b"}], "errors": [], "success": True})]
        self.fake.statuses = {"a": [{"status": "delivered"}], "b": [{"status": "undelivered"}]}
        self.assertEqual(self.run_one("mass").outcome, "ОШИБКА")

    def test_network_failure_on_send_is_error_without_retry(self) -> None:
        self.client.base_url = "http://127.0.0.1:9/api"
        self.client.timeout = 1
        self.assertEqual(self.run_one("sms").outcome, "ОШИБКА")


class CascadeTest(Base):
    def test_hlr_delivered_continues_to_sms(self) -> None:
        self.fake.statuses = {UUID: [{"status": "delivered", "child_uuid": "child"}], "child": [{"status": "delivered"}]}
        result = self.run_one("cascade_hlr_sms")
        self.assertEqual(result.outcome, "OK")
        self.assertIn("hlr:delivered → sms:delivered", result.detail)

    def test_hlr_child_appearing_late_is_followed(self) -> None:
        self.settings = replace(self.settings, child_wait=5)
        self.fake.statuses = {UUID: [{"status": "delivered", "child_uuid": ""}, {"status": "delivered", "child_uuid": "c"}],
                              "c": [{"status": "delivered"}]}
        self.assertIn("sms:delivered", self.run_one("cascade_hlr_sms").detail)

    def test_hlr_delivered_without_next_channel_fails(self) -> None:
        self.fake.statuses[UUID] = [{"status": "delivered"}]
        result = self.run_one("cascade_hlr_sms")
        self.assertEqual(result.outcome, "ОШИБКА")
        self.assertIn("следующий канал", result.detail)

    def test_hlr_undelivered_stops_after_child_wait(self) -> None:
        self.fake.statuses[UUID] = [{"status": "undelivered"}]
        self.assertEqual(self.run_one("cascade_hlr_sms").outcome, "ОШИБКА")

    def test_first_channel_success_ends_cascade(self) -> None:
        self.fake.statuses[UUID] = [{"status": "delivered"}]
        self.assertEqual(self.run_one("cascade_pushok_sms").outcome, "OK")

    def test_failed_channel_falls_back_to_sms(self) -> None:
        self.fake.statuses = {UUID: [{"status": "undelivered", "child_uuid": "c"}], "c": [{"status": "delivered"}]}
        result = self.run_one("cascade_tgauth_sms")
        self.assertEqual(result.outcome, "OK")
        self.assertIn("tgauth:undelivered → sms:delivered", result.detail)

    def test_last_channel_failure_is_error(self) -> None:
        self.fake.statuses = {UUID: [{"status": "undelivered", "child_uuid": "c"}], "c": [{"status": "undelivered"}]}
        self.assertEqual(self.run_one("cascade_tgauth_sms").outcome, "ОШИБКА")


class SafetyTest(Base):
    def test_default_selection_excludes_extra_services(self) -> None:
        names = [s.name for s in select_scenarios(None)]
        self.assertNotIn("ping", names)
        self.assertNotIn("viber", names)
        for name in ("sms", "tgauth", "wcall", "fcall", "pushok", "voice", "hlr", "vk", "mass"):
            self.assertIn(name, names)

    def test_unknown_service_rejected(self) -> None:
        with self.assertRaises(ValueError):
            select_scenarios("sms,nope")

    def test_scheduled_sms_is_never_sent_live(self) -> None:
        self.assertEqual(self.run_one("sms_scheduled").outcome, "ПРОПУЩЕНО")
        self.assertEqual(self.fake.posts(), [])

    def test_missing_arguments_skip_without_request(self) -> None:
        for name, flag in (("voice", "--voice-from"), ("vk", "--vk-text"), ("mass", "--to2")):
            with self.subTest(name):
                result = self.run_one(name)
                self.assertEqual(result.outcome, "ПРОПУЩЕНО")
                self.assertIn(flag, result.detail)
        self.assertEqual(self.fake.posts(), [])

    def test_russia_only_services_skip_foreign_numbers(self) -> None:
        self.settings = replace(self.settings, to="+380501234567")
        self.assertEqual(self.run_one("wcall").outcome, "ПРОПУЩЕНО")
        self.assertEqual(self.run_one("pushok").outcome, "ПРОПУЩЕНО")

    def test_live_requires_exact_confirmation(self) -> None:
        scenarios = [BY_NAME["sms"]]
        for answer, expected in (("ДА", True), ("да", False), ("yes", False), ("", False)):
            with self.subTest(answer):
                self.assertEqual(confirm_live(scenarios, self.settings, False, True, lambda _: answer,
                                              self.output.append), expected)

    def test_live_without_terminal_refuses_unless_yes(self) -> None:
        def fail(_: str) -> str:
            raise AssertionError("не должен спрашивать")
        scenarios = [BY_NAME["sms"]]
        self.assertFalse(confirm_live(scenarios, self.settings, False, False, fail, self.output.append))
        self.assertTrue(confirm_live(scenarios, self.settings, True, False, fail, self.output.append))

    def test_confirmation_lists_only_runnable_services(self) -> None:
        confirm_live([BY_NAME["sms"], BY_NAME["voice"]], self.settings, True, True, input, self.output.append)
        text = "\n".join(self.output)
        self.assertIn("SMS", text)
        self.assertNotIn("Voice", text)

    def test_main_refuses_without_credentials(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("REDSMS_LOGIN", "REDSMS_API_KEY")}
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch("sys.stderr", new=unittest.mock.MagicMock()):
            self.assertEqual(main(["--to", TO]), 2)

    def test_main_live_refused_sends_nothing(self) -> None:
        env = {"REDSMS_LOGIN": "u", "REDSMS_API_KEY": "k"}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch("sys.stdin.isatty", return_value=True, create=True), \
                unittest.mock.patch("builtins.input", return_value="нет"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--to", TO, "--live", "--services", "hlr", "--base-url", self.fake.base_url]), 2)
        self.assertEqual(self.fake.calls, [])


    def run_main(self, *extra: str) -> int:
        env = {"REDSMS_LOGIN": "u", "REDSMS_API_KEY": "k"}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch(f"{__name__}.setup_log_file", return_value="test.log"), \
                contextlib.redirect_stdout(io.StringIO()):
            return main(["--to", TO, "--to2", TO2, "--voice-from", "V", "--vk-from", "V", "--vk-text", "T",
                         "--base-url", self.fake.base_url, "--interval", "0", *extra])

    def test_main_default_mode_marks_every_send_as_test(self) -> None:
        self.assertEqual(self.run_main(), 0)
        posts = self.fake.posts()
        self.assertEqual(len(posts), len(select_scenarios(None)))
        self.assertTrue(all(p["body"].get("test") is True for p in posts))
        self.assertTrue(all("fields=all" in p["path"] for p in posts))

    def test_main_returns_one_on_failure(self) -> None:
        self.fake.send_replies = [BAD_NUMBER]
        self.assertEqual(self.run_main("--services", "wcall"), 1)

    def test_main_rejects_number_without_plus(self) -> None:
        env = {"REDSMS_LOGIN": "u", "REDSMS_API_KEY": "k"}
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--to", "79993332211", "--base-url", self.fake.base_url]), 2)
        self.assertEqual(self.fake.calls, [])


class LoggingTest(Base):
    def test_log_has_support_details_and_no_secrets(self) -> None:
        self.fake.send_replies = [sent(replacedFrom=CALL_NUMBER)]
        self.fake.statuses[UUID] = [{"status": "progress"}, {"status": "wcall_delivered"}]
        with self.assertLogs(log, level="DEBUG") as logs:
            self.run_one("wcall")
        output = "\n".join(logs.output)
        for expected in ("POST /message", UUID, CALL_NUMBER, "HTTP 200", "progress -> wcall_delivered", "+7999***11"):
            self.assertIn(expected, output)
        self.assertNotIn(TO, output)
        self.assertNotIn("api-key", output)
        self.assertNotIn(self.fake.calls[0]["headers"]["secret"], output)

    def test_mask_covers_mass_cascade_and_ip(self) -> None:
        masked = json.dumps(mask({"to": f"{TO},{TO2}", "sms_to": TO, "ip": "1.2.3.4", "replacedFrom": CALL_NUMBER,
                                  "errors": [{"to": TO}]}))
        self.assertNotIn(TO, masked)
        self.assertNotIn(TO2, masked)
        self.assertNotIn("1.2.3.4", masked)
        self.assertIn(CALL_NUMBER, masked)

    def test_status_change_logged_once(self) -> None:
        self.fake.statuses[UUID] = [{"status": "progress"}, {"status": "progress"}, {"status": "delivered"}]
        with self.assertLogs(log, level="INFO") as logs:
            self.client.follow(UUID, ("sms",), wait=5, interval=0)
        self.assertEqual(len([line for line in logs.output if "-> progress" in line]), 1)

    def test_log_file_goes_next_to_script_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler = logging.FileHandler(os.path.join(directory, "t.log"), encoding="utf-8")
            log.addHandler(handler)
            level = log.level
            log.setLevel(logging.DEBUG)
            try:
                self.run_one("hlr", live=False)
            finally:
                log.removeHandler(handler)
                handler.close()
                log.setLevel(level)
            with open(os.path.join(directory, "t.log"), encoding="utf-8") as f:
                content = f.read()
        self.assertIn("POST /message?fields=all", content)
        self.assertNotIn("api-key", content)
        self.assertNotIn(TO, content)


if __name__ == "__main__":
    sys.exit(main())
