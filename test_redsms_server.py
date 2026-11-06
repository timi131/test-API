"""Офлайн-тесты серверного режима окна: python3 test_redsms_server.py -v"""
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
BASE = "/apps/testredsms"


class ServerModeTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = directory.name
        self.fake = core.FakeRedsms()
        self.addCleanup(self.fake.close)
        self.app = gui.App(base_url=self.fake.base_url, interval=0, server_mode=True, base_path=BASE, data_dir=self.data)
        self.server = gui.make_server(self.app, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, user: Optional[str] = "anna",
             token: bool = True, host: str = "apps.example.ru") -> Tuple[int, Any, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host}
        if user:
            headers["X-User"] = user
        if token:
            headers["X-Token"] = self.app.token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        is_json = "json" in (response.getheader("Content-Type") or "")
        return response.status, json.loads(raw) if is_json else raw.decode(), response

    def run_check(self, user: str = "anna", **extra: Any) -> Dict[str, Any]:
        form = {"login": "api-user", "api_key": "api-key-value", "to": TO, "services": ["hlr"], **extra}
        self.assertEqual(self.call("POST", f"{BASE}/api/start", form, user=user)[0], 200)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snap = self.call("GET", f"{BASE}/api/status", user=user)[1]
            if snap["state"] != "running":
                return snap
            time.sleep(0.01)
        self.fail("проверка не завершилась")

    # --- адреса за прокси ---

    def test_page_opens_under_prefix_with_any_host(self) -> None:
        status, page, _ = self.call("GET", f"{BASE}/")
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, page)
        self.assertIn(gui.SERVER_PRIVACY, page)
        self.assertNotIn("__PRIVACY__", page)

    def test_prefix_without_slash_redirects(self) -> None:
        status, _, response = self.call("GET", BASE)
        self.assertEqual(status, 302)
        self.assertEqual(response.getheader("Location"), f"{BASE}/")

    def test_page_uses_relative_api_addresses(self) -> None:
        self.assertIn("fetch(path.slice(1), options)", gui.PAGE)
        self.assertNotIn('fetch("/', gui.PAGE)

    def test_paths_outside_prefix_are_404(self) -> None:
        for method, path, body in (("GET", "/", None), ("GET", "/api/status", None), ("POST", "/api/start", {}),
                                   ("GET", f"{BASE}x/", None)):
            with self.subTest(path):
                self.assertEqual(self.call(method, path, body)[0], 404)
        self.assertEqual(self.fake.calls, [])

    def test_root_mount_works_without_prefix(self) -> None:
        self.app.base_path = ""
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/api/status")[0], 200)

    # --- доступ ---

    def test_requests_without_user_are_refused(self) -> None:
        for method, path, body in (("GET", f"{BASE}/", None), ("GET", f"{BASE}/api/status", None),
                                   ("GET", f"{BASE}/api/templates", None), ("POST", f"{BASE}/api/start", {})):
            with self.subTest(path):
                self.assertEqual(self.call(method, path, body, user=None)[0], 401)
        self.assertEqual(self.fake.calls, [])

    def test_blank_user_is_refused(self) -> None:
        self.assertEqual(self.call("GET", f"{BASE}/", user="   ")[0], 401)

    def test_token_still_required(self) -> None:
        self.assertEqual(self.call("POST", f"{BASE}/api/start", {}, token=False)[0], 403)
        self.assertEqual(self.call("GET", f"{BASE}/api/status", token=False)[0], 403)

    def test_live_confirmation_still_required(self) -> None:
        form = {"login": "u", "api_key": "k", "to": TO, "services": ["hlr"], "mode": "live"}
        self.assertEqual(self.call("POST", f"{BASE}/api/start", form)[0], 400)
        self.assertEqual(self.fake.calls, [])

    # --- разделение пользователей ---

    def test_status_and_live_log_are_per_user(self) -> None:
        self.run_check("anna")
        self.assertEqual(self.call("GET", f"{BASE}/api/status", user="boris")[1], {"state": "idle"})
        self.assertEqual(self.call("GET", f"{BASE}/api/log", user="boris")[1]["entries"], [])
        self.assertTrue(self.call("GET", f"{BASE}/api/log", user="anna")[1]["entries"])

    def test_log_files_are_per_user(self) -> None:
        self.run_check("anna")
        files = self.call("GET", f"{BASE}/api/logs", user="anna")[1]["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(self.call("GET", f"{BASE}/api/logs", user="boris")[1]["files"], [])
        status, _, _ = self.call("GET", f"{BASE}/api/logs/view?name={files[0]['name']}", user="boris")
        self.assertEqual(status, 400)

    def test_templates_are_per_user(self) -> None:
        template = {"name": "Код", "channel": "sms", "text": "Код %d"}
        saved = self.call("POST", f"{BASE}/api/templates", template, user="anna")[1]["template"]
        self.assertEqual(self.call("GET", f"{BASE}/api/templates", user="boris")[1]["templates"], [])
        self.assertEqual(self.call("POST", f"{BASE}/api/templates/delete", {"id": saved["id"]}, user="boris")[0], 400)
        self.assertEqual(len(self.call("GET", f"{BASE}/api/templates", user="anna")[1]["templates"]), 1)

    def test_stop_affects_only_own_run(self) -> None:
        self.fake.statuses[core.UUID] = [{"status": "progress"}]
        form = {"login": "u", "api_key": "k", "to": TO, "services": ["hlr"], "mode": "live", "confirm": "ДА"}
        self.call("POST", f"{BASE}/api/start", form, user="anna")
        self.call("POST", f"{BASE}/api/stop", {}, user="boris")
        time.sleep(0.05)
        self.assertEqual(self.call("GET", f"{BASE}/api/status", user="anna")[1]["state"], "running")
        self.call("POST", f"{BASE}/api/stop", {}, user="anna")
        deadline = time.monotonic() + 5
        while self.call("GET", f"{BASE}/api/status", user="anna")[1]["state"] == "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_simultaneous_runs_do_not_mix_logs(self) -> None:
        self.fake.statuses[core.UUID] = [{"status": "progress"}] * 30 + [{"status": "delivered"}]
        forms = {"anna": "+79990000044", "boris": "+79990000055"}
        for user, to in forms.items():
            form = {"login": "u", "api_key": "k", "to": to, "services": ["hlr"], "mode": "live", "confirm": "ДА"}
            self.assertEqual(self.call("POST", f"{BASE}/api/start", form, user=user)[0], 200)
        deadline = time.monotonic() + 5
        for user in forms:
            while self.call("GET", f"{BASE}/api/status", user=user)[1]["state"] == "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        anna = json.dumps(self.call("GET", f"{BASE}/api/log", user="anna")[1], ensure_ascii=False)
        boris = json.dumps(self.call("GET", f"{BASE}/api/log", user="boris")[1], ensure_ascii=False)
        self.assertIn("***44", anna)
        self.assertNotIn("***55", anna)
        self.assertIn("***55", boris)
        self.assertNotIn("***44", boris)
        self.assertEqual(len(core.log.handlers), 1)

    def test_same_user_cannot_run_twice_but_other_can(self) -> None:
        self.fake.statuses[core.UUID] = [{"status": "progress"}]
        form = {"login": "u", "api_key": "k", "to": TO, "services": ["hlr"], "mode": "live", "confirm": "ДА"}
        self.call("POST", f"{BASE}/api/start", form, user="anna")
        self.assertEqual(self.call("POST", f"{BASE}/api/start", form, user="anna")[0], 400)
        self.assertEqual(self.call("POST", f"{BASE}/api/start", form, user="boris")[0], 200)
        for user in ("anna", "boris"):
            self.call("POST", f"{BASE}/api/stop", {}, user=user)
            deadline = time.monotonic() + 5
            while self.call("GET", f"{BASE}/api/status", user=user)[1]["state"] == "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

    # --- файлы ---

    def test_user_folder_is_a_hash_inside_data_dir(self) -> None:
        self.call("GET", f"{BASE}/", user="../../evil")
        folders = os.listdir(self.data)
        self.assertEqual(len(folders), 1)
        self.assertRegex(folders[0], r"^[0-9a-f]{32}$")
        self.assertEqual(os.stat(os.path.join(self.data, folders[0])).st_mode & 0o777, 0o700)

    def test_key_not_in_files_and_container_path_hidden(self) -> None:
        snap = self.run_check()
        self.assertIsNone(snap["log_path"])
        for root, _, files in os.walk(self.data):
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    content = f.read()
                self.assertNotIn("api-key-value", content)
                self.assertNotIn(TO, content)

    def test_old_logs_are_pruned(self) -> None:
        space = self.app.space("anna")
        for day in range(1, 41):
            with open(os.path.join(space.log_dir, f"redsms_test_202601{day % 28 + 1:02d}-0000{day:02d}.log"), "w") as f:
                f.write("x")
        self.run_check()
        self.assertLessEqual(len(space.log_files()), gui.MAX_USER_LOGS)


class LocalModeUnchangedTest(unittest.TestCase):
    def test_local_mode_needs_no_user_but_checks_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = gui.App(open_log=False, log_dir=directory, templates_path=os.path.join(directory, "t.json"))
            server = gui.make_server(app, 0)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
            port = server.server_address[1]
            for host, expected in ((f"127.0.0.1:{port}", 200), ("evil.example.com", 403)):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/", headers={"Host": host})
                self.assertEqual(conn.getresponse().status, expected)
                conn.close()
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertIn(gui.LOCAL_PRIVACY, gui.PAGE.replace("__PRIVACY__", gui.LOCAL_PRIVACY))


if __name__ == "__main__":
    unittest.main()
