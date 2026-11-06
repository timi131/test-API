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
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import redsms_test as core

DEFAULT_PORT = 8770
MAX_BODY = 64 * 1024
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


class Run:
    def __init__(self, plan: List[Dict[str, Any]], live: bool):
        self.plan = plan
        self.live = live
        self.lines: List[str] = []
        self.results: List[core.Result] = []
        self.state = "running"  # running, finished, stopped, crashed
        self.log_path: Optional[str] = None
        self.stop = threading.Event()

    def snapshot(self) -> Dict[str, Any]:
        return {"plan": self.plan, "live": self.live, "lines": list(self.lines),
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
    raw_validity = text_field(data, "validity")
    if raw_validity is not None and not raw_validity.isdigit():
        raise ValueError("Срок жизни должен быть целым числом секунд")
    settings = core.Settings(
        to=to, to2=to2, sms_from=text_field(data, "sms_from"), sms_text=text_field(data, "sms_text"),
        voice_from=text_field(data, "voice_from"), vk_from=text_field(data, "vk_from"),
        vk_text=text_field(data, "vk_text"), viber_from=text_field(data, "viber_from"), pushok_text=pushok_text,
        validity=int(raw_validity) if raw_validity else None, interval=interval)
    return settings, scenarios, data.get("mode") == "live"


def make_plan(settings: core.Settings, scenarios: List[core.Scenario], live: bool) -> List[Dict[str, Any]]:
    plan = []
    for s in scenarios:
        reason = core.skip_reason(s, settings, live)
        plan.append({"name": s.name, "title": s.title, "skip": friendly(reason) if reason else None})
    return plan


class App:
    def __init__(self, base_url: str = core.DEFAULT_BASE_URL, interval: float = 5.0, open_log: bool = True):
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
        try:
            if self.open_log:
                run.log_path = core.setup_log_file()
            core.run_all(client, settings, scenarios, live, run.lines.append, run.results)
            run.state = "finished"
        except Stopped:
            run.lines.append("Остановлено. Уже созданные сообщения отменить нельзя.")
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
        if not self._host_ok():
            self._json(403, {"error": "Недопустимый адрес"})
        elif self.path == "/":
            page = PAGE.replace("__TOKEN__", self.app.token).replace("__CATALOG__", catalog_json())
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            if not self._token_ok():
                self._json(403, {"error": "Неверный токен"})
            else:
                run = self.app.run
                self._json(200, run.snapshot() if run else {"state": "idle"})
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
            elif self.path == "/api/stop":
                if self.app.run:
                    self.app.run.stop.set()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "Не найдено"})
        except ValueError as e:  # в том числе ошибка разбора JSON
            self._json(400, {"error": str(e)})


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
.action { background:var(--note); border:1px solid var(--note-line); border-radius:8px; padding:12px; font-size:18px; font-weight:600; margin-bottom:12px; }
.action:empty { display:none; }
pre { margin:0; max-height:280px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.5 ui-monospace,Menlo,monospace; color:var(--muted); }
.empty { color:var(--muted); }
.logpath { font-size:12px; color:var(--muted); margin-top:8px; overflow-wrap:anywhere; }
.badge { display:inline-block; font-size:12px; font-weight:600; padding:2px 8px; border-radius:99px; background:var(--soft); margin-left:8px; }
.badge.live { background:var(--accent); color:var(--accent-text); }
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
    <div class="action" id="action"></div>
    <div id="results"><p class="empty" style="margin:0">Выберите услуги и запустите проверку.</p></div>
    <p class="logpath" id="logpath" hidden></p>
  </section>
  <section>
    <details id="logbox"><summary style="margin:0">Ход выполнения</summary><pre id="lines" style="margin-top:10px"></pre></details>
  </section>
</div>
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
}
async function poll() {
  try {
    const snap = await api("/api/status");
    render(snap);
    if (snap.state !== "running") { clearInterval(polling); polling = null; }
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
  const pre = $("lines");
  pre.textContent = snap.lines.join("\n");
  pre.scrollTop = pre.scrollHeight;
  if (snap.state === "stopped" || snap.state === "crashed") { $("logbox").open = true; }
  if (snap.log_path) { $("logpath").hidden = false; $("logpath").textContent = "Лог: " + snap.log_path; }
}
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
