"""Офлайн-тесты окна redsms_gui.py: python3 test_redsms_gui.py -v"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redsms_gui as gui  # noqa: E402
import redsms_test as core  # noqa: E402

TO = "+79993332211"
KEY = "super-secret-api-key"


class GuiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = core.FakeRedsms()
        self.addCleanup(self.fake.close)
        self.app = gui.App(base_url=self.fake.base_url, interval=0, open_log=False)
        self.server = gui.make_server(self.app, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, token: Optional[str] = "default",
             host: Optional[str] = None) -> Tuple[int, Any, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Token"] = self.app.token if token == "default" else token
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        kind = response.getheader("Content-Type") or ""
        return response.status, json.loads(raw) if "json" in kind else raw.decode(), response

    def form(self, **extra: Any) -> Dict[str, Any]:
        return {"login": "user", "api_key": KEY, "to": TO, "services": ["hlr"], "mode": "dry", **extra}

    def wait_done(self) -> Dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, snap, _ = self.call("GET", "/api/status")
            if snap["state"] != "running":
                return snap
            time.sleep(0.01)
        self.fail("проверка не завершилась")

    def posts(self) -> list:
        return self.fake.posts()

    # --- защита сервера ---

    def test_server_listens_only_on_loopback(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_page_contains_session_token(self) -> None:
        status, page, response = self.call("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, page)
        self.assertNotIn("__CATALOG__", page)
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def test_foreign_host_rejected(self) -> None:
        for method, path in (("GET", "/"), ("GET", "/api/status"), ("POST", "/api/start")):
            with self.subTest(path):
                body = self.form() if method == "POST" else None
                self.assertEqual(self.call(method, path, body, host="evil.example.com")[0], 403)
        self.assertEqual(self.fake.calls, [])

    def test_start_requires_token(self) -> None:
        self.assertEqual(self.call("POST", "/api/start", self.form(), token=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/start", self.form(), token="wrong")[0], 403)
        self.assertEqual(self.fake.calls, [])

    def test_status_and_stop_require_token(self) -> None:
        self.assertEqual(self.call("GET", "/api/status", token=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/stop", {}, token=None)[0], 403)

    def test_oversized_body_rejected(self) -> None:
        huge = self.form(sms_text="x" * (gui.MAX_BODY + 1))
        self.assertEqual(self.call("POST", "/api/start", huge)[0], 413)
        self.assertEqual(self.fake.calls, [])

    # --- проверка формы ---

    def test_bad_input_rejected_without_requests(self) -> None:
        cases = {
            "нет логина": self.form(login=""),
            "нет ключа": self.form(api_key=""),
            "номер без плюса": self.form(to="79993332211"),
            "нет услуг": self.form(services=[]),
            "неизвестная услуга": self.form(services=["nope"]),
            "плохой срок": self.form(validity="abc"),
            "плохой второй номер": self.form(to2="123"),
            "поле не строка": self.form(login=123),
        }
        for name, form in cases.items():
            with self.subTest(name):
                status, reply, _ = self.call("POST", "/api/start", form)
                self.assertEqual(status, 400)
                self.assertTrue(reply["error"])
        self.assertEqual(self.fake.calls, [])

    def test_invalid_json_is_400(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/start", "{not json", {"X-Token": self.app.token})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    # --- подтверждение настоящей отправки ---

    def test_live_requires_exact_confirmation_on_server(self) -> None:
        for confirm in (None, "", "да", "yes"):
            with self.subTest(confirm):
                form = self.form(mode="live")
                if confirm is not None:
                    form["confirm"] = confirm
                self.assertEqual(self.call("POST", "/api/start", form)[0], 400)
        self.assertEqual(self.fake.calls, [])

    def test_live_with_confirmation_sends_without_test_flag(self) -> None:
        status, _, _ = self.call("POST", "/api/start", self.form(mode="live", confirm="ДА"))
        self.assertEqual(status, 200)
        snap = self.wait_done()
        self.assertEqual(snap["state"], "finished")
        self.assertNotIn("test", self.posts()[0]["body"])
        self.assertEqual({r["name"]: r["outcome"] for r in snap["results"]}["hlr"], "OK")

    def test_dry_mode_is_default_and_marks_test(self) -> None:
        form = self.form()
        del form["mode"]
        self.call("POST", "/api/start", form)
        self.wait_done()
        self.assertIs(self.posts()[0]["body"]["test"], True)

    def test_plan_lists_skips_and_sends_nothing(self) -> None:
        form = self.form(services=["hlr", "voice", "sms_scheduled"], mode="live")
        status, reply, _ = self.call("POST", "/api/plan", form)
        self.assertEqual(status, 200)
        self.assertTrue(reply["live"])
        skips = {p["name"]: p["skip"] for p in reply["plan"]}
        self.assertIsNone(skips["hlr"])
        self.assertIn("имя отправителя Voice", skips["voice"])
        self.assertNotIn("--", skips["voice"])
        self.assertIn("только имитация", skips["sms_scheduled"])
        self.assertEqual(self.fake.calls, [])

    # --- ключ ---

    def test_key_used_for_signature_and_never_exposed(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "t.log")
            original = core.setup_log_file

            def fake_setup() -> str:
                handler = core.logging.FileHandler(path, encoding="utf-8")
                core.log.addHandler(handler)
                core.log.setLevel(core.logging.DEBUG)
                return path

            core.setup_log_file = fake_setup
            self.addCleanup(setattr, core, "setup_log_file", original)
            self.app.open_log = True
            self.call("POST", "/api/start", self.form())
            snap = self.wait_done()
            with open(path, encoding="utf-8") as f:
                log_text = f.read()
        self.assertTrue(log_text)
        self.assertNotIn(KEY, log_text)
        self.assertNotIn(TO, log_text)
        self.assertNotIn(KEY, json.dumps(snap, ensure_ascii=False))
        headers = self.fake.calls[0]["headers"]
        self.assertEqual(headers["secret"], hashlib.md5((headers["ts"] + KEY).encode()).hexdigest())
        self.assertNotIn(KEY, json.dumps(self.fake.calls))
        self.assertEqual(len(core.log.handlers), 1)  # файл лога закрыт после проверки

    # --- ход проверки ---

    def test_bad_auth_shown_in_results(self) -> None:
        self.fake.client_info_reply = (401, {"error_message": "unauthorized", "success": False})
        self.call("POST", "/api/start", self.form())
        snap = self.wait_done()
        self.assertEqual(snap["results"][0]["outcome"], "ОШИБКА")
        self.assertIn("белый список", snap["results"][0]["detail"])
        self.assertEqual(self.posts(), [])

    def test_prompt_with_call_number_reaches_window(self) -> None:
        self.fake.send_replies = [core.sent(replacedFrom="79160001122")]
        self.call("POST", "/api/start", self.form(services=["wcall"], mode="live", confirm="ДА"))
        snap = self.wait_done()
        self.assertTrue(any(line.startswith(core.PROMPT_PREFIX) and "+79160001122" in line for line in snap["lines"]))

    def test_second_run_rejected_while_first_runs_and_stop_ends_it(self) -> None:
        self.fake.statuses[core.UUID] = [{"status": "progress"}]
        status, _, _ = self.call("POST", "/api/start", self.form(services=["hlr", "sms"], mode="live", confirm="ДА"))
        self.assertEqual(status, 200)
        status, reply, _ = self.call("POST", "/api/start", self.form(mode="live", confirm="ДА"))
        self.assertEqual(status, 400)
        self.assertIn("уже идёт", reply["error"])
        self.call("POST", "/api/stop", {})
        snap = self.wait_done()
        self.assertEqual(snap["state"], "stopped")
        self.assertEqual(len(self.posts()), 1)  # услуга после остановки не отправлена
        status, _, _ = self.call("POST", "/api/start", self.form())
        self.assertEqual(status, 200)
        self.wait_done()

    def test_crash_does_not_leave_running_state(self) -> None:
        original = core.run_all

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("сломалось")

        core.run_all = boom
        self.addCleanup(setattr, core, "run_all", original)
        self.call("POST", "/api/start", self.form())
        snap = self.wait_done()
        self.assertEqual(snap["state"], "crashed")
        self.assertIn("сломалось", snap["lines"][-1])

    def test_catalog_covers_every_scenario(self) -> None:
        names = [item["name"] for group in json.loads(gui.catalog_json()) for item in group["items"]]
        self.assertEqual(sorted(names), sorted(core.BY_NAME))


if __name__ == "__main__":
    unittest.main()
