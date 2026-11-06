#!/usr/bin/env python3
"""Окно для redsms_test.py: python3 redsms_gui.py

Запускает локальный сервер только на 127.0.0.1 и открывает страницу в браузере. Нужен только стандартный Python.
Логин и ключ живут в памяти на время проверки и уходят только в API REDSMS. На диск они не пишутся.
Запросы к серверу требуют токен, который создаётся при запуске, поэтому чужой сайт не сможет запустить
платную отправку через этот сервер.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import redsms_templates as templates
import redsms_test as core

DEFAULT_PORT = 8770
MAX_BODY = 64 * 1024
MAX_LIVE_LOG = 20000
MAX_LOG_VIEW = 2 * 1024 * 1024
LOG_NAME = re.compile(r"redsms_test_\d{8}-\d{6}\.log")
GROUPS = (
    ("Сообщения", ("sms", "sms_scheduled", "voice", "vk")),
    ("Авторизация", ("tgauth", "wcall", "fcall", "pushok")),
    ("Проверка номера", ("hlr",)),
    ("Каскады", ("cascade_hlr_sms", "cascade_pushok_sms", "cascade_tgauth_sms")),
    ("Массовая отправка", ("mass",)),
    ("Без страницы в документации", ("ping", "viber")),
)


FLAG_LABELS = {
    "--voice-from": "имя отправителя Voice",
    "--vk-from": "имя отправителя VK",
    "--vk-text": "текст VK",
    "--to2": "второй номер",
}


def friendly(text: str) -> str:
    """Подсказки ядра написаны для командной строки, в окне вместо флагов нужны названия полей."""
    for flag, label in FLAG_LABELS.items():
        text = text.replace(flag, label)
    return text


class Stopped(Exception):
    pass


class StoppableClient(core.Client):
    """Перед каждым запросом проверяет кнопку «Остановить», поэтому новых платных запросов после неё нет."""

    def __init__(self, stop: threading.Event, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.stop = stop

    def _request(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if self.stop.is_set():
            raise Stopped()
        return super()._request(*args, **kwargs)


class RunLogHandler(logging.Handler):
    """Копит записи лога текущей проверки для вкладки «Логи»."""

    def __init__(self, entries: List[Dict[str, str]]):
        super().__init__(logging.DEBUG)
        self.entries = entries

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.entries) < MAX_LIVE_LOG:
            self.entries.append({"time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                                 "level": record.levelname, "text": record.getMessage()})


class Run:
    def __init__(self, plan: List[Dict[str, Any]], live: bool):
        self.id = secrets.token_hex(4)
        self.log_entries: List[Dict[str, str]] = []
        self.plan = plan
        self.live = live
        self.lines: List[str] = []
        self.results: List[core.Result] = []
        self.state = "running"  # running, finished, stopped, crashed
        self.log_path: Optional[str] = None
        self.stop = threading.Event()

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "plan": self.plan, "live": self.live, "lines": list(self.lines),
                "results": [{**asdict(r), "detail": friendly(r.detail)} for r in list(self.results)], "state": self.state,
                "stopping": self.stop.is_set(), "log_path": self.log_path}


def text_field(data: Dict[str, Any], name: str) -> Optional[str]:
    value = data.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Поле {name} должно быть строкой")
    return value.strip() or None


def parse_form(data: Dict[str, Any], interval: float) -> Tuple[core.Settings, List[core.Scenario], bool]:
    to = text_field(data, "to")
    if not to or not re.fullmatch(r"\+\d{10,15}", to):
        raise ValueError("Укажите номер получателя в формате +7XXXXXXXXXX")
    to2 = text_field(data, "to2")
    if to2 and not re.fullmatch(r"\+\d{10,15}", to2):
        raise ValueError("Второй номер нужен в формате +7XXXXXXXXXX")
    names = data.get("services")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ValueError("Выберите хотя бы одну услугу")
    scenarios = core.select_scenarios(",".join(names))
    pushok_text = text_field(data, "pushok_text") or "phone"
    if pushok_text not in ("enter", "reg", "phone"):
        raise ValueError("Неверный текст окна PushOK")
    expect_traffic = text_field(data, "expect_traffic")
    if expect_traffic and expect_traffic not in templates.TRAFFIC_TYPES:
        raise ValueError("Неизвестный тип шаблона")
    raw_validity = text_field(data, "validity")
    if raw_validity is not None and not raw_validity.isdigit():
        raise ValueError("Срок жизни должен быть целым числом секунд")
    settings = core.Settings(
        to=to, to2=to2, sms_from=text_field(data, "sms_from"), sms_text=text_field(data, "sms_text"),
        voice_from=text_field(data, "voice_from"), vk_from=text_field(data, "vk_from"),
        vk_text=text_field(data, "vk_text"), viber_from=text_field(data, "viber_from"), pushok_text=pushok_text,
        expect_traffic=expect_traffic,
        validity=int(raw_validity) if raw_validity else None, interval=interval)
    return settings, scenarios, data.get("mode") == "live"


def make_plan(settings: core.Settings, scenarios: List[core.Scenario], live: bool) -> List[Dict[str, Any]]:
    plan = []
    for s in scenarios:
        reason = core.skip_reason(s, settings, live)
        plan.append({"name": s.name, "title": s.title, "skip": friendly(reason) if reason else None})
    return plan


class App:
    def __init__(self, base_url: str = core.DEFAULT_BASE_URL, interval: float = 5.0, open_log: bool = True,
                 log_dir: Optional[str] = None, templates_path: Optional[str] = None):
        here = os.path.dirname(os.path.abspath(core.__file__))
        self.log_dir = log_dir or here
        self.templates = templates.TemplateStore(templates_path or os.path.join(here, "redsms_templates.json"))
        self.token = secrets.token_urlsafe(32)
        self.base_url = base_url
        self.interval = interval
        self.open_log = open_log
        self.lock = threading.Lock()
        self.run: Optional[Run] = None

    def start(self, data: Dict[str, Any]) -> Dict[str, Any]:
        settings, scenarios, live = parse_form(data, self.interval)
        login, api_key = text_field(data, "login"), text_field(data, "api_key")
        if not login or not api_key:
            raise ValueError("Укажите логин и API-ключ")
        plan = make_plan(settings, scenarios, live)
        if live and data.get("confirm") != "ДА":
            raise ValueError("Для настоящей отправки нужно подтверждение: введите ДА")
        with self.lock:
            if self.run and self.run.state == "running":
                raise ValueError("Проверка уже идёт")
            run = Run(plan, live)
            self.run = run
        client = StoppableClient(run.stop, login, api_key, base_url=self.base_url)
        threading.Thread(target=self._work, args=(run, client, settings, scenarios, live), daemon=True).start()
        return run.snapshot()

    def _work(self, run: Run, client: core.Client, settings: core.Settings, scenarios: List[core.Scenario],
              live: bool) -> None:
        handlers_before = list(core.log.handlers)
        core.log.setLevel(logging.DEBUG)
        core.log.addHandler(RunLogHandler(run.log_entries))
        try:
            if self.open_log:
                run.log_path = core.setup_log_file()
            core.run_all(client, settings, scenarios, live, run.lines.append, run.results)
            run.state = "finished"
        except Stopped:
            run.lines.append("Остановлено. Уже созданные сообщения отменить нельзя.")
            core.log.warning("проверка остановлена пользователем")
            run.state = "stopped"
        except Exception as e:  # окно должно показать сбой, а не зависнуть в состоянии «идёт»
            core.log.exception("сбой проверки")
            run.lines.append(f"Сбой: {type(e).__name__}: {e}")
            run.state = "crashed"
        finally:
            for handler in list(core.log.handlers):
                if handler not in handlers_before:
                    core.log.removeHandler(handler)
                    handler.close()


    def live_log(self, since: int) -> Dict[str, Any]:
        run = self.run
        if not run:
            return {"run": None, "entries": [], "total": 0, "truncated": False}
        entries = list(run.log_entries)
        return {"run": run.id, "entries": entries[max(since, 0):], "total": len(entries),
                "truncated": len(entries) >= MAX_LIVE_LOG}

    def log_files(self) -> List[Dict[str, Any]]:
        files = []
        for name in os.listdir(self.log_dir):
            if LOG_NAME.fullmatch(name):
                stat = os.stat(os.path.join(self.log_dir, name))
                files.append({"name": name, "size": stat.st_size,
                              "modified": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(stat.st_mtime))})
        return sorted(files, key=lambda f: f["name"], reverse=True)

    def read_log_file(self, name: str) -> Dict[str, Any]:
        # Имя проверяется по строгому шаблону, поэтому прочитать файл вне папки логов нельзя
        if not LOG_NAME.fullmatch(name):
            raise ValueError("Недопустимое имя файла")
        path = os.path.join(self.log_dir, name)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(size - MAX_LOG_VIEW, 0))
                raw = f.read()
        except FileNotFoundError:
            raise ValueError("Файл лога не найден") from None
        return {"name": name, "text": raw.decode("utf-8", errors="replace"), "truncated": size > MAX_LOG_VIEW}


class Handler(BaseHTTPRequestHandler):
    server_version = "redsms-gui"
    sys_version = ""
    app: App

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        # Защита от DNS rebinding: страница чужого домена не должна достучаться до сервера
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Token", "").encode(), self.app.token.encode())

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self._host_ok():
            self._json(403, {"error": "Недопустимый адрес"})
        elif url.path == "/":
            page = PAGE.replace("__TOKEN__", self.app.token).replace("__CATALOG__", catalog_json()) \
                .replace("__TRAFFIC_TYPES__", json.dumps(templates.TRAFFIC_TYPES, ensure_ascii=False))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif not url.path.startswith("/api/"):
            self._json(404, {"error": "Не найдено"})
        elif not self._token_ok():
            self._json(403, {"error": "Неверный токен"})
        else:
            try:
                self._api_get(url.path, query)
            except ValueError as e:
                self._json(400, {"error": str(e)})

    def _api_get(self, path: str, query: Dict[str, List[str]]) -> None:
        if path == "/api/status":
            run = self.app.run
            self._json(200, run.snapshot() if run else {"state": "idle"})
        elif path == "/api/log":
            since = query.get("since", ["0"])[0]
            self._json(200, self.app.live_log(int(since) if since.isdigit() else 0))
        elif path == "/api/logs":
            self._json(200, {"files": self.app.log_files()})
        elif path == "/api/logs/view":
            self._json(200, self.app.read_log_file(query.get("name", [""])[0]))
        elif path == "/api/templates":
            self._json(200, {"templates": [templates.describe(t) for t in self.app.templates.list()]})
        else:
            self._json(404, {"error": "Не найдено"})

    def do_POST(self) -> None:
        if not self._host_ok() or not self._token_ok():
            self._json(403, {"error": "Доступ запрещён"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "Слишком большой запрос"})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Ожидался JSON-объект")
            if self.path == "/api/plan":
                settings, scenarios, live = parse_form(data, self.app.interval)
                self._json(200, {"plan": make_plan(settings, scenarios, live), "live": live})
            elif self.path == "/api/start":
                self._json(200, self.app.start(data))
            elif self.path == "/api/templates":
                self._json(200, {"template": templates.describe(self.app.templates.save(data))})
            elif self.path == "/api/templates/delete":
                self.app.templates.delete(data.get("id"))
                self._json(200, {"ok": True})
            elif self.path == "/api/templates/check":
                self._json(200, check_template(data))
            elif self.path == "/api/stop":
                if self.app.run:
                    self.app.run.stop.set()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "Не найдено"})
        except ValueError as e:  # в том числе ошибка разбора JSON
            self._json(400, {"error": str(e)})


def check_template(data: Dict[str, Any]) -> Dict[str, Any]:
    template, text = data.get("template"), data.get("text")
    if not isinstance(template, str) or (text is not None and not isinstance(text, str)):
        raise ValueError("Шаблон и текст должны быть строками")
    result: Dict[str, Any] = {"problems": templates.lint_template(template), "parts": templates.split_template(template)}
    if text:
        result["match"] = templates.match_template(template, text)
    return result


def catalog_json() -> str:
    groups = []
    for title, names in GROUPS:
        items = []
        for name in names:
            s = core.BY_NAME[name]
            items.append({"name": s.name, "title": s.title, "extra": s.extra, "live_ok": s.live_ok,
                          "needs": [attr for attr, _ in s.needs], "russia_only": s.russia_only})
        groups.append({"title": title, "items": items})
    return json.dumps(groups, ensure_ascii=False).replace("<", "\\u003c")


def make_server(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Проверка REDSMS</title>
<style>
:root { color-scheme: light dark; --bg:#f4f4f2; --panel:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#dcdcd6;
  --accent:#c8102e; --accent-text:#fff; --ok:#1a7f4b; --bad:#c62828; --skip:#7a7a72; --note:#fff6d6; --note-line:#e4c95a;
  --input:#fff; --soft:#ecece8; }
@media (prefers-color-scheme: dark) { :root { --bg:#17171a; --panel:#222226; --text:#ececea; --muted:#a2a29c; --line:#3a3a40;
  --accent:#e0344c; --ok:#4fc486; --bad:#ff7a70; --skip:#9a9a92; --note:#3a3216; --note-line:#8a7520; --input:#1b1b1e; --soft:#2c2c31; } }
* { box-sizing:border-box; }
[hidden] { display:none !important; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width:1120px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 20px; }
.layout { display:grid; grid-template-columns:minmax(0,420px) minmax(0,1fr); gap:16px; align-items:start; }
@media (max-width:860px) { .layout { grid-template-columns:minmax(0,1fr); } }
section { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
h2 { font-size:13px; margin:0 0 12px; color:var(--muted); font-weight:600; }
h3 { font-size:13px; margin:14px 0 6px; font-weight:600; }
h3:first-of-type { margin-top:0; }
label.field { display:block; margin-bottom:10px; font-size:13px; color:var(--muted); }
input[type=text],input[type=password],input[type=tel],input[type=number],select { display:block; width:100%; margin-top:4px; padding:8px 10px; font:inherit;
  color:var(--text); background:var(--input); border:1px solid var(--line); border-radius:6px; }
input:focus-visible,select:focus-visible,button:focus-visible,summary:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
.keyrow { display:flex; gap:6px; align-items:flex-end; }
.keyrow label { flex:1; margin:0; }
button { font:inherit; padding:8px 14px; border-radius:6px; border:1px solid var(--line); background:var(--soft); color:var(--text); cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
button.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-text); font-weight:600; }
button.link { border:0; background:none; padding:0; color:var(--muted); text-decoration:underline; font-size:13px; }
.modes { display:grid; gap:8px; }
.mode { display:flex; gap:10px; padding:10px; border:1px solid var(--line); border-radius:8px; cursor:pointer; }
.mode:has(input:checked) { border-color:var(--accent); }
.mode b { display:block; font-weight:600; }
.mode small { color:var(--muted); }
.svc { display:flex; gap:8px; align-items:baseline; padding:3px 0; cursor:pointer; }
.svc small { color:var(--muted); }
.tools { display:flex; gap:12px; margin-bottom:10px; }
details summary { cursor:pointer; font-size:13px; color:var(--muted); margin-bottom:10px; }
.actions { display:flex; gap:8px; flex-wrap:wrap; }
.err { color:var(--bad); margin:10px 0 0; font-size:14px; }
.err:empty { display:none; }
table { width:100%; border-collapse:collapse; font-size:14px; }
td { padding:7px 8px 7px 0; border-top:1px solid var(--line); vertical-align:top; overflow-wrap:anywhere; }
tr:first-child td { border-top:0; }
td.st { white-space:nowrap; font-weight:600; width:120px; }
.OK { color:var(--ok); } .ОШИБКА { color:var(--bad); } .ПРОПУЩЕНО,.wait { color:var(--skip); } .now { color:var(--accent); }
.detail { color:var(--muted); font-size:13px; }
.uuid { font-size:12px; color:var(--muted); font-family:ui-monospace,Menlo,monospace; }
.action { position:sticky; top:8px; z-index:5; background:var(--note); border:1px solid var(--note-line); border-radius:8px; padding:12px; font-size:18px; font-weight:600; margin-bottom:12px; }
.action:empty { display:none; }
pre { margin:0; max-height:280px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.5 ui-monospace,Menlo,monospace; color:var(--muted); }
.empty { color:var(--muted); }
.logpath { font-size:12px; color:var(--muted); margin-top:8px; overflow-wrap:anywhere; }
.badge { display:inline-block; font-size:12px; font-weight:600; padding:2px 8px; border-radius:99px; background:var(--soft); margin-left:8px; }
.badge.live { background:var(--accent); color:var(--accent-text); }
.tabs { display:flex; gap:4px; margin-bottom:16px; border-bottom:1px solid var(--line); overflow-x:auto; }
.tabs button { border:0; border-bottom:2px solid transparent; border-radius:0; background:none; padding:8px 14px; color:var(--muted); white-space:nowrap; }
.tabs button[aria-selected=true] { color:var(--text); border-bottom-color:var(--accent); font-weight:600; }
textarea { display:block; width:100%; margin-top:4px; padding:8px 10px; font:inherit; color:var(--text); background:var(--input); border:1px solid var(--line); border-radius:6px; resize:vertical; min-height:72px; }
textarea:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
input[readonly] { color:var(--muted); background:var(--soft); }
.note { font-size:13px; color:var(--muted); margin:8px 0 0; }
.good { color:var(--ok); } .bad { color:var(--bad); }
.status { font-size:13px; margin:8px 0 0; overflow-wrap:anywhere; }
.status:empty { display:none; }
.preview { background:var(--soft); border-radius:6px; padding:8px 10px; font-size:14px; margin-top:8px; white-space:pre-wrap; overflow-wrap:anywhere; }
.preview:empty { display:none; }
.tpl { border-top:1px solid var(--line); padding:12px 0; }
.tpl:first-child { border-top:0; padding-top:0; }
.tpl:last-child { padding-bottom:0; }
.tpl b { font-weight:600; }
.tpl .meta { font-size:12px; color:var(--muted); margin:2px 0 6px; }
.tpl .body { font-size:14px; white-space:pre-wrap; overflow-wrap:anywhere; margin-bottom:8px; }
.vars code, .tpl code { font:12px ui-monospace,Menlo,monospace; }
.vars { font-size:13px; }
.vars td { padding:4px 8px 4px 0; }
.logbar { display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center; margin-bottom:10px; }
.logbar select, .logbar input[type=search] { margin:0; width:auto; min-width:0; flex:0 1 auto; padding:6px 8px; font:inherit; font-size:13px; color:var(--text); background:var(--input); border:1px solid var(--line); border-radius:6px; }
.logbar select { max-width:100%; }
.logbar label { font-size:13px; display:flex; gap:5px; align-items:center; }
.logbar button { padding:6px 10px; font-size:13px; }
#logView { height:min(62vh,640px); overflow:auto; font:12px/1.55 ui-monospace,Menlo,monospace; background:var(--input); border:1px solid var(--line); border-radius:6px; padding:8px 10px; }
#logView div { white-space:pre-wrap; overflow-wrap:anywhere; padding:1px 0; }
#logView .t { color:var(--muted); }
#logView .WARNING,#logView .ERROR { color:var(--bad); }
#logView .DEBUG { color:var(--muted); }
#runNote { font-size:14px; margin:12px 0 0; }
#runNote:empty { display:none; }
dialog { border:1px solid var(--line); border-radius:10px; background:var(--panel); color:var(--text); padding:20px; max-width:440px; width:calc(100% - 32px); }
dialog::backdrop { background:rgba(0,0,0,.5); }
dialog h2 { font-size:17px; color:var(--text); margin-bottom:8px; }
dialog ul { padding-left:20px; margin:8px 0; }
dialog .skipped { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<main>
<h1>Проверка услуг REDSMS</h1>
<p class="sub">Логин и ключ хранятся только в памяти этого окна и сервера и отправляются только в API REDSMS.</p>
<div class="action" id="action" role="status"></div>
<nav class="tabs" role="tablist" aria-label="Разделы">
  <button type="button" role="tab" data-tab="run" aria-selected="true">Проверка</button>
  <button type="button" role="tab" data-tab="templates" aria-selected="false">Шаблоны</button>
  <button type="button" role="tab" data-tab="logs" aria-selected="false">Логи</button>
</nav>
<div id="tab-run">
<div class="layout">
<form id="form" autocomplete="off" novalidate>
  <section>
    <h2>Доступ</h2>
    <label class="field">Логин<input type="text" id="login" autocomplete="off" spellcheck="false"></label>
    <div class="keyrow">
      <label class="field">API-ключ<input type="password" id="api_key" autocomplete="new-password" spellcheck="false"></label>
      <button type="button" id="toggleKey">Показать</button>
    </div>
  </section>
  <section>
    <h2>Режим</h2>
    <div class="modes">
      <label class="mode"><input type="radio" name="mode" value="dry" checked><span><b>Имитация</b><small>Сообщения не отправляются. Проверяет доступ и приём запроса, но не доставку.</small></span></label>
      <label class="mode"><input type="radio" name="mode" value="live"><span><b>Настоящая отправка</b><small>Платно. Сообщения и звонки придут на указанный номер.</small></span></label>
    </div>
  </section>
  <section>
    <h2>Получатель</h2>
    <label class="field" style="margin:0">Номер<input type="tel" id="to" placeholder="+7XXXXXXXXXX" spellcheck="false"></label>
    <label class="field" id="f_to2" style="margin:10px 0 0" hidden>Второй номер для массовой отправки<input type="tel" id="to2" placeholder="+7XXXXXXXXXX" spellcheck="false"></label>
    <label class="field" id="f_voice_from" style="margin:10px 0 0" hidden>Имя отправителя Voice<input type="text" id="voice_from"></label>
    <label class="field" id="f_vk_from" style="margin:10px 0 0" hidden>Имя отправителя VK<input type="text" id="vk_from"></label>
    <label class="field" id="f_vk_text" style="margin:10px 0 0" hidden>Текст VK по зарегистрированному шаблону<input type="text" id="vk_text"></label>
  </section>
  <section>
    <h2>Текст сообщения</h2>
    <label class="field" style="margin:0">Шаблон<select id="tplSelect"></select></label>
    <div id="tplVars" style="margin-top:10px"></div>
    <div class="preview" id="tplPreview"></div>
    <p class="status" id="tplStatus"></p>
    <p class="note" id="tplNote">Без шаблона текст SMS создаётся автоматически и тарифицируется как рекламный.</p>
  </section>
  <section>
    <h2>Что проверить</h2>
    <div class="tools"><button type="button" class="link" id="selAll">Все основные</button><button type="button" class="link" id="selNone">Снять все</button></div>
    <div id="services"></div>
  </section>
  <section>
    <details>
      <summary>Дополнительные параметры</summary>
      <label class="field">Имя отправителя SMS<input type="text" id="sms_from" placeholder="по умолчанию из настроек"></label>
      <label class="field">Текст SMS<input type="text" id="sms_text" placeholder="создаётся автоматически"></label>
      <label class="field">Имя отправителя Viber<input type="text" id="viber_from"></label>
      <label class="field">Текст окна PushOK<select id="pushok_text"><option value="phone">Подтвердите номер телефона</option><option value="enter">Подтвердите вход</option><option value="reg">Подтвердите регистрацию</option></select></label>
      <label class="field" style="margin:0">Срок жизни, секунд (SMS от 600, остальные от 60)<input type="number" id="validity" min="60" max="86400" placeholder="по умолчанию для услуги"></label>
    </details>
  </section>
  <div class="actions">
    <button type="submit" class="primary" id="start">Запустить проверку</button>
    <button type="button" id="stop" disabled>Остановить</button>
  </div>
  <p class="err" id="error" role="alert"></p>
</form>
<div>
  <section id="resultsBox" aria-live="polite">
    <h2>Результаты<span class="badge" id="badge" hidden></span></h2>
    <div id="results"><p class="empty" style="margin:0">Выберите услуги и запустите проверку.</p></div>
    <p id="runNote"></p>
    <p class="logpath" id="logpath" hidden></p>
    <p style="margin:12px 0 0"><button type="button" class="link" id="openLogs">Открыть подробный лог</button></p>
  </section>
</div>
</div>
</div>
<div id="tab-templates" hidden>
<div class="layout">
  <form id="tplForm" autocomplete="off" novalidate>
    <section>
      <h2 id="tplFormTitle">Новый шаблон</h2>
      <label class="field">Название<input type="text" id="t_name" maxlength="100"></label>
      <label class="field">Канал<select id="t_channel"><option value="sms">SMS</option><option value="vk">VK</option></select></label>
      <label class="field">Имя отправителя<input type="text" id="t_sender" maxlength="50" placeholder="как зарегистрировано у оператора"></label>
      <label class="field" id="f_t_type">Тип шаблона<select id="t_type"><option value="">Не указан</option></select></label>
      <label class="field">Текст шаблона<textarea id="t_text" maxlength="2000" rows="4" placeholder="Ваш код %d для входа на сайт"></textarea></label>
      <label class="field" id="f_t_example">Пример текста, как у оператора<textarea id="t_example" maxlength="2000" rows="3" placeholder="Ваш код 4821 для входа на сайт"></textarea></label>
      <p class="status" id="t_check"></p>
      <div class="actions" style="margin-top:12px">
        <button type="submit" class="primary" id="t_save">Сохранить</button>
        <button type="button" id="t_clear">Очистить</button>
      </div>
      <p class="err" id="t_error" role="alert"></p>
    </section>
    <section class="vars">
      <h2>Переменные SMS</h2>
      <table>
        <tr><td><code>%w</code>, <code>%d</code></td><td>Одно значение без пробелов</td></tr>
        <tr><td><code>%d+</code></td><td>Числа через пробел</td></tr>
        <tr><td><code>%w{1,n}</code></td><td>От 1 до n слов, n до 20</td></tr>
        <tr><td><code>%d{1,n}</code></td><td>От 1 до n чисел, n от 2 до 20</td></tr>
      </table>
      <p class="note">Шаблоны регистрируются в личном кабинете REDSMS, здесь хранится локальная копия. Проверка текста приблизительная: окончательно решает оператор, поэтому смотрите на тип тарификации в результате имитации.</p>
    </section>
  </form>
  <section>
    <h2>Сохранённые шаблоны</h2>
    <div id="tplList"><p class="empty" style="margin:0">Шаблонов пока нет.</p></div>
    <p class="err" id="tplListError" role="alert"></p>
  </section>
</div>
</div>
<div id="tab-logs" hidden>
  <section>
    <div class="logbar">
      <select id="logSource" aria-label="Источник лога"></select>
      <input type="search" id="logSearch" placeholder="Поиск" aria-label="Поиск по логу">
      <label><input type="checkbox" id="logDebug">Опрос статусов</label>
      <label><input type="checkbox" id="logErrors">Только ошибки</label>
      <label><input type="checkbox" id="logFollow" checked>Прокручивать вниз</label>
      <button type="button" id="logRefresh">Обновить</button>
      <button type="button" id="logCopy">Копировать</button>
      <button type="button" id="logDownload">Скачать</button>
    </div>
    <div id="logView" tabindex="0" aria-label="Содержимое лога"></div>
    <p class="note" id="logNote">API-ключа и secret в логах нет, номера получателей замаскированы.</p>
  </section>
</div>
</main>
<dialog id="confirm">
  <form method="dialog" id="confirmForm">
    <h2>Настоящая платная отправка</h2>
    <div id="confirmBody"></div>
    <label class="field" style="margin-top:12px">Введите ДА, чтобы отправить<input type="text" id="confirmInput" autocomplete="off"></label>
    <div class="actions">
      <button type="button" class="primary" id="confirmGo" disabled>Отправить</button>
      <button type="button" id="confirmCancel">Отмена</button>
    </div>
  </form>
</dialog>
<script>
const TOKEN = "__TOKEN__";
const CATALOG = __CATALOG__;
const TRAFFIC_TYPES = __TRAFFIC_TYPES__;
const $ = id => document.getElementById(id);
let polling = null;

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

for (const group of CATALOG) {
  $("services").append(el("h3", "", group.title));
  for (const s of group.items) {
    const label = el("label", "svc");
    const box = document.createElement("input");
    box.type = "checkbox"; box.value = s.name; box.checked = !s.extra; box.dataset.extra = s.extra ? "1" : "";
    box.dataset.needs = s.needs.join(",");
    box.addEventListener("change", syncNeeds);
    const name = el("span", "", s.title);
    label.append(box, name);
    if (!s.live_ok) label.append(el("small", "", "только имитация"));
    if (s.russia_only) label.append(el("small", "", "номера РФ"));
    $("services").append(label);
  }
}
function boxes() { return [...document.querySelectorAll("#services input")]; }
function syncNeeds() {
  const needed = new Set(boxes().filter(b => b.checked).flatMap(b => b.dataset.needs ? b.dataset.needs.split(",") : []));
  for (const f of ["to2", "voice_from", "vk_from", "vk_text"]) $("f_" + f).hidden = !needed.has(f);
}
syncNeeds();
$("selAll").onclick = () => { boxes().forEach(b => b.checked = !b.dataset.extra); syncNeeds(); };
$("selNone").onclick = () => { boxes().forEach(b => b.checked = false); syncNeeds(); };
$("toggleKey").onclick = () => {
  const hidden = $("api_key").type === "password";
  $("api_key").type = hidden ? "text" : "password";
  $("toggleKey").textContent = hidden ? "Скрыть" : "Показать";
};

function formData() {
  const data = { mode: document.querySelector("input[name=mode]:checked").value, services: boxes().filter(b => b.checked).map(b => b.value) };
  for (const f of ["login", "api_key", "to", "to2", "voice_from", "vk_from", "vk_text", "sms_from", "sms_text", "viber_from", "pushok_text", "validity"]) data[f] = $(f).value;
  data.expect_traffic = chosenTemplate && chosenTemplate.channel === "sms" ? chosenTemplate.traffic_type : "";
  return data;
}
async function api(path, body) {
  const options = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const r = await fetch(path, options);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "Ошибка сервера");
  return data;
}
function showError(text) { $("error").textContent = text || ""; }

$("form").addEventListener("submit", async e => {
  e.preventDefault();
  showError("");
  const data = formData();
  if (!data.login.trim() || !data.api_key.trim()) { showError("Укажите логин и API-ключ"); return; }
  if (chosenTemplate && chosenTemplate.channel === "sms" && !templateFilled()) { showError("Заполните все значения шаблона"); return; }
  try {
    const { plan, live } = await api("/api/plan", data);
    if (live) { openConfirm(plan, data); return; }
    await start(data);
  } catch (err) { showError(err.message); }
});
function openConfirm(plan, data) {
  const body = $("confirmBody");
  body.replaceChildren();
  const run = plan.filter(p => !p.skip), skip = plan.filter(p => p.skip);
  if (!run.length) { showError("Ни одну из выбранных услуг нельзя запустить: " + skip.map(p => p.title + " (" + p.skip + ")").join("; ")); return; }
  const recipients = data.to + (data.to2 && run.some(p => p.name === "mass") ? " и " + data.to2 : "");
  body.append(el("p", "", "Будет отправлено на " + recipients + ":"));
  const list = el("ul");
  run.forEach(p => list.append(el("li", "", p.title)));
  body.append(list);
  if (skip.length) body.append(el("p", "skipped", "Будут пропущены: " + skip.map(p => p.title + " (" + p.skip + ")").join("; ")));
  $("confirmInput").value = "";
  $("confirmGo").disabled = true;
  $("confirmGo").onclick = async () => {
    const confirm = $("confirmInput").value.trim();
    $("confirm").close();
    try { await start({ ...data, confirm }); } catch (err) { showError(err.message); }
  };
  $("confirm").showModal();
  $("confirmInput").focus();
}
$("confirmInput").addEventListener("input", () => { $("confirmGo").disabled = $("confirmInput").value.trim() !== "ДА"; });
$("confirmCancel").onclick = () => $("confirm").close();
$("confirmForm").addEventListener("submit", e => { e.preventDefault(); if (!$("confirmGo").disabled) $("confirmGo").click(); });

async function start(data) {
  const snap = await api("/api/start", data);
  render(snap);
  $("resultsBox").scrollIntoView({ behavior: "smooth", block: "start" });
  if (!polling) polling = setInterval(poll, 1000);
  refreshLiveLog();
}
async function poll() {
  try {
    const snap = await api("/api/status");
    render(snap);
    await refreshLiveLog();
    if (snap.state !== "running") { clearInterval(polling); polling = null; loadLogFiles(); }
  } catch (err) { showError("Нет связи с сервером скрипта: " + err.message); }
}
$("stop").onclick = async () => { $("stop").disabled = true; try { await api("/api/stop", {}); } catch (err) { showError(err.message); } };

function render(snap) {
  const running = snap.state === "running";
  $("start").disabled = running;
  $("stop").disabled = !running || snap.stopping;
  $("stop").textContent = snap.stopping && running ? "Останавливаем..." : "Остановить";
  const badge = $("badge");
  badge.hidden = false;
  badge.textContent = snap.live ? "настоящая отправка" : "имитация";
  badge.className = "badge" + (snap.live ? " live" : "");

  const byName = Object.fromEntries(snap.results.map(r => [r.name, r]));
  const rows = [];
  if (byName.client_info) rows.push(byName.client_info);
  else rows.push({ title: "Доступ и баланс", outcome: running ? "идёт" : "не завершено", detail: "" });
  let current = running && byName.client_info && snap.plan.find(p => !byName[p.name]);
  for (const p of snap.plan) {
    const r = byName[p.name];
    rows.push(r || { title: p.title, outcome: current && current.name === p.name ? "идёт" : (running ? "ждёт" : "не завершено"), detail: "" });
  }
  if (byName.balance_guard) rows.push(byName.balance_guard);
  const table = el("table");
  for (const r of rows) {
    const tr = el("tr");
    const cls = r.outcome === "идёт" ? "now" : (r.outcome === "ждёт" || r.outcome === "не завершено") ? "wait" : r.outcome;
    tr.append(el("td", "st " + cls, r.outcome));
    const td = el("td");
    td.append(el("div", "", r.title));
    if (r.detail) td.append(el("div", "detail", r.detail));
    if (r.outcome === "ОШИБКА" && r.uuids && r.uuids.length) td.append(el("div", "uuid", "uuid: " + r.uuids.join(", ")));
    tr.append(td);
    table.append(tr);
  }
  $("results").replaceChildren(table);

  // Подсказка актуальна, только пока после неё нет новых строк: следующая строка уже итог этой услуги
  const lastLine = snap.lines[snap.lines.length - 1] || "";
  $("action").textContent = running && lastLine.startsWith("  → ") ? lastLine.slice(4) : "";
  const note = snap.state === "stopped" || snap.state === "crashed" ? lastLine : running ? "" : (snap.lines.find(l => l.trim().startsWith("Баланс:")) || "").trim();
  $("runNote").textContent = note;
  if (snap.log_path) { $("logpath").hidden = false; $("logpath").textContent = "Лог: " + snap.log_path; }
}
// --- Вкладки ---
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  for (const tab of ["run", "templates", "logs"]) $("tab-" + tab).hidden = tab !== name;
  window.scrollTo(0, 0);
  if (name === "logs") { loadLogFiles(); refreshLiveLog().then(renderLog); }
  if (name === "templates") loadTemplates();
}
document.querySelectorAll(".tabs button").forEach(b => { b.onclick = () => showTab(b.dataset.tab); });
$("openLogs").onclick = () => { $("logSource").value = "live"; logSourceChanged(); showTab("logs"); };

// --- Шаблоны ---
let allTemplates = [];
let chosenTemplate = null;
let editingId = null;
for (const [value, title] of Object.entries(TRAFFIC_TYPES)) {
  const o = el("option", "", title); o.value = value; $("t_type").append(o);
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function setStatus(id, text, good) { $(id).textContent = text; $(id).className = "status " + (good ? "good" : "bad"); }

async function loadTemplates() {
  try {
    allTemplates = (await api("/api/templates")).templates;
    $("tplListError").textContent = "";
  } catch (err) { $("tplListError").textContent = err.message; return; }
  renderTemplateList();
  renderTemplateSelect();
}
function renderTemplateList() {
  const list = $("tplList");
  list.replaceChildren();
  if (!allTemplates.length) { list.append(el("p", "empty", "Шаблонов пока нет.")); return; }
  for (const t of allTemplates) {
    const card = el("div", "tpl");
    card.append(el("b", "", t.name));
    const meta = [t.channel === "vk" ? "VK" : "SMS", TRAFFIC_TYPES[t.traffic_type], t.sender].filter(Boolean).join(" · ");
    card.append(el("div", "meta", meta));
    card.append(el("div", "body", t.text));
    if (t.problems.length) card.append(el("p", "status bad", t.problems.join(". ")));
    const actions = el("div", "actions");
    const use = el("button", "", "Использовать в проверке"); use.type = "button";
    use.onclick = () => { $("tplSelect").value = t.id; chooseTemplate(); showTab("run"); };
    const edit = el("button", "", "Изменить"); edit.type = "button"; edit.onclick = () => editTemplate(t);
    const del = el("button", "", "Удалить"); del.type = "button"; del.onclick = () => deleteTemplate(t);
    actions.append(use, edit, del);
    card.append(actions);
    list.append(card);
  }
}
function renderTemplateSelect() {
  const select = $("tplSelect");
  const current = chosenTemplate ? chosenTemplate.id : "";
  select.replaceChildren();
  const auto = el("option", "", "Без шаблона"); auto.value = ""; select.append(auto);
  for (const t of allTemplates) { const o = el("option", "", (t.channel === "vk" ? "VK: " : "SMS: ") + t.name); o.value = t.id; select.append(o); }
  select.value = allTemplates.some(t => t.id === current) ? current : "";
  chooseTemplate();
}
function clearChosen() {
  if (chosenTemplate && chosenTemplate.channel === "sms") {
    $("sms_text").value = ""; $("sms_text").readOnly = false;
    if ($("sms_from").value === chosenTemplate.sender) $("sms_from").value = "";
  }
  if (chosenTemplate && chosenTemplate.channel === "vk") {
    if ($("vk_text").value === chosenTemplate.text) $("vk_text").value = "";
    if ($("vk_from").value === chosenTemplate.sender) $("vk_from").value = "";
  }
  chosenTemplate = null;
  $("tplVars").replaceChildren(); $("tplPreview").textContent = ""; $("tplStatus").textContent = "";
}
function chooseTemplate() {
  const t = allTemplates.find(x => x.id === $("tplSelect").value) || null;
  const previousValues = [...document.querySelectorAll("#tplVars input")].map(i => i.value);
  const same = chosenTemplate && t && chosenTemplate.id === t.id && chosenTemplate.text === t.text;
  clearChosen();
  chosenTemplate = t;
  $("tplNote").hidden = !!t;
  if (!t) return;
  if (t.channel === "vk") {
    $("vk_text").value = t.text;
    if (t.sender) $("vk_from").value = t.sender;
    const box = boxes().find(b => b.value === "vk"); box.checked = true; syncNeeds();
    setStatus("tplStatus", "Текст подставлен в поле VK. Замените в нём переменные на значения.", true);
    return;
  }
  if (t.sender) $("sms_from").value = t.sender;
  $("sms_text").readOnly = true;
  let index = 0;
  for (const part of t.parts) {
    if (!part.var) continue;
    const label = el("label", "field", "Значение для " + part.var);
    const input = document.createElement("input");
    input.type = "text"; input.dataset.index = index;
    input.value = (same ? previousValues[index] : t.example_values[index]) || "";
    input.addEventListener("input", updateFilled);
    label.append(input);
    $("tplVars").append(label);
    index++;
  }
  updateFilled();
}
function templateFilled() { return [...document.querySelectorAll("#tplVars input")].every(i => i.value.trim()); }
function filledText() {
  const inputs = [...document.querySelectorAll("#tplVars input")];
  let index = 0;
  return chosenTemplate.parts.map(p => p.var ? inputs[index++].value.trim() : p.literal).join("");
}
function updateFilled() {
  const text = filledText();
  $("sms_text").value = text;
  $("tplPreview").textContent = text;
  checkFilled();
}
const checkFilled = debounce(async () => {
  const t = chosenTemplate;
  if (!t || t.channel !== "sms") return;
  if (!templateFilled()) { setStatus("tplStatus", "Заполните все значения шаблона.", false); return; }
  try {
    const { match } = await api("/api/templates/check", { template: t.text, text: filledText() });
    if (chosenTemplate !== t) return;
    if (match.ok) {
      const kind = TRAFFIC_TYPES[t.traffic_type];
      setStatus("tplStatus", "Текст совпадает с шаблоном." + (kind ? " Имитация проверит тип тарификации: " + kind.toLowerCase() + "." : ""), true);
    } else setStatus("tplStatus", match.reason, false);
  } catch (err) { setStatus("tplStatus", err.message, false); }
}, 200);
$("tplSelect").addEventListener("change", chooseTemplate);

function syncTemplateForm() {
  const sms = $("t_channel").value === "sms";
  $("f_t_type").hidden = !sms; $("f_t_example").hidden = !sms;
  checkTemplateForm();
}
const checkTemplateForm = debounce(async () => {
  if ($("t_channel").value !== "sms" || !$("t_text").value.trim()) { $("t_check").textContent = ""; return; }
  try {
    const r = await api("/api/templates/check", { template: $("t_text").value.trim(), text: $("t_example").value.trim() });
    if (r.problems.length) setStatus("t_check", r.problems.join(". "), false);
    else if (r.match && !r.match.ok) setStatus("t_check", "Пример не совпадает: " + r.match.reason, false);
    else if (r.match) setStatus("t_check", "Пример совпадает с шаблоном.", true);
    else setStatus("t_check", "Переменных в шаблоне: " + r.parts.filter(p => p.var).length + ".", true);
  } catch (err) { setStatus("t_check", err.message, false); }
}, 200);
["t_text", "t_example"].forEach(id => $(id).addEventListener("input", checkTemplateForm));
$("t_channel").addEventListener("change", syncTemplateForm);
function clearTemplateForm() {
  editingId = null;
  for (const id of ["t_name", "t_sender", "t_type", "t_text", "t_example"]) $(id).value = "";
  $("t_channel").value = "sms";
  $("tplFormTitle").textContent = "Новый шаблон";
  $("t_error").textContent = ""; $("t_check").textContent = "";
  syncTemplateForm();
}
function editTemplate(t) {
  editingId = t.id;
  $("t_name").value = t.name; $("t_channel").value = t.channel; $("t_sender").value = t.sender;
  $("t_type").value = t.traffic_type; $("t_text").value = t.text; $("t_example").value = t.example;
  $("tplFormTitle").textContent = "Изменить шаблон";
  $("t_error").textContent = "";
  syncTemplateForm();
  $("tplForm").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function deleteTemplate(t) {
  if (!window.confirm("Удалить шаблон «" + t.name + "»? В личном кабинете REDSMS он останется.")) return;
  try {
    await api("/api/templates/delete", { id: t.id });
    if (editingId === t.id) clearTemplateForm();
    await loadTemplates();
  } catch (err) { $("tplListError").textContent = err.message; }
}
$("t_clear").onclick = clearTemplateForm;
$("tplForm").addEventListener("submit", async e => {
  e.preventDefault();
  $("t_error").textContent = "";
  try {
    await api("/api/templates", { id: editingId, name: $("t_name").value, channel: $("t_channel").value, sender: $("t_sender").value,
      traffic_type: $("t_channel").value === "sms" ? $("t_type").value : "", text: $("t_text").value,
      example: $("t_channel").value === "sms" ? $("t_example").value : "" });
    clearTemplateForm();
    await loadTemplates();
  } catch (err) { $("t_error").textContent = err.message; }
});

// --- Логи ---
let liveLog = [], liveRun = null, liveTruncated = false, fileLog = [], fileTruncated = false;
const LEVELS_NOISY = "DEBUG";
function fileEntries(text) {
  const entries = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ ([A-Z]+) (.*)$/);
    if (m) entries.push({ time: m[1], level: m[2], text: m[3] });
    else if (line && entries.length) entries[entries.length - 1].text += "\n" + line;
  }
  return entries;
}
async function refreshLiveLog() {
  try {
    const since = liveLog.length;
    const r = await api("/api/log?since=" + since);
    if (r.run !== liveRun) {
      const fresh = await api("/api/log?since=0");
      liveRun = fresh.run; liveLog = fresh.entries; liveTruncated = fresh.truncated;
    } else if (r.entries.length) { liveLog = liveLog.concat(r.entries); liveTruncated = r.truncated; }
    else return;
    if ($("logSource").value === "live" && !$("tab-logs").hidden) renderLog();
  } catch (err) { /* поллинг покажет ошибку связи */ }
}
async function loadLogFiles() {
  const select = $("logSource");
  const current = select.value || "live";
  try {
    const { files } = await api("/api/logs");
    select.replaceChildren();
    const live = el("option", "", "Текущий запуск"); live.value = "live"; select.append(live);
    for (const f of files) { const o = el("option", "", f.modified + " (" + Math.ceil(f.size / 1024) + " КБ)"); o.value = f.name; select.append(o); }
    select.value = [...select.options].some(o => o.value === current) ? current : "live";
  } catch (err) { $("logNote").textContent = err.message; }
}
async function logSourceChanged() {
  const name = $("logSource").value;
  if (name && name !== "live") {
    try {
      const r = await api("/api/logs/view?name=" + encodeURIComponent(name));
      fileLog = fileEntries(r.text); fileTruncated = r.truncated;
    } catch (err) { fileLog = []; $("logNote").textContent = err.message; }
  }
  renderLog();
}
function visibleLog() {
  const source = $("logSource").value;
  const entries = !source || source === "live" ? liveLog : fileLog;
  const query = $("logSearch").value.trim().toLowerCase();
  return entries.filter(e => {
    if (e.level === LEVELS_NOISY && !$("logDebug").checked) return false;
    if ($("logErrors").checked && e.level !== "WARNING" && e.level !== "ERROR") return false;
    return !query || e.text.toLowerCase().includes(query);
  });
}
function renderLog() {
  const view = $("logView");
  const source = $("logSource").value;
  const entries = visibleLog();
  view.replaceChildren(...entries.map(e => {
    const row = el("div", e.level);
    row.append(el("span", "t", e.time + " "), document.createTextNode(e.text));
    return row;
  }));
  if (!entries.length) view.append(el("p", "empty", source === "live" || !source ? (liveLog.length ? "По фильтрам записей нет." : "Запустите проверку, и здесь появится лог.") : "По фильтрам записей нет."));
  const cut = source === "live" || !source ? liveTruncated : fileTruncated;
  $("logNote").textContent = (cut ? "Показана часть лога. " : "") + "API-ключа и secret в логах нет, номера получателей замаскированы.";
  if ($("logFollow").checked) view.scrollTop = view.scrollHeight;
}
function logAsText() { return visibleLog().map(e => e.time + " " + e.level + " " + e.text).join("\n"); }
$("logSource").addEventListener("change", logSourceChanged);
["logSearch", "logDebug", "logErrors"].forEach(id => $(id).addEventListener("input", renderLog));
$("logRefresh").onclick = async () => { await loadLogFiles(); if ($("logSource").value === "live") await refreshLiveLog(); await logSourceChanged(); };
function flashButton(button, text) { const old = button.textContent; button.textContent = text; setTimeout(() => { button.textContent = old; }, 1500); }
$("logCopy").onclick = async () => {
  try { await navigator.clipboard.writeText(logAsText()); flashButton($("logCopy"), "Скопировано"); }
  catch (err) { flashButton($("logCopy"), "Не удалось"); }
};
$("logDownload").onclick = () => {
  const source = $("logSource").value;
  const name = source && source !== "live" ? source : "redsms_test_current.log";
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([logAsText()], { type: "text/plain;charset=utf-8" }));
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
};

clearTemplateForm();
loadTemplates();
loadLogFiles();
api("/api/status").then(snap => { if (snap.state !== "idle") { render(snap); if (snap.state === "running") polling = setInterval(poll, 1000); } }).catch(() => {});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Окно проверки услуг REDSMS")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт на 127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    args = parser.parse_args()
    app = App(base_url=os.environ.get("REDSMS_API_BASE", core.DEFAULT_BASE_URL))
    try:
        server = make_server(app, args.port)
    except OSError:
        server = make_server(app, 0)  # порт занят: берём свободный
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Окно проверки: {url}\nЧтобы закрыть, нажмите Ctrl+C.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    logging.getLogger().addHandler(logging.NullHandler())
    raise SystemExit(main())
