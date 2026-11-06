"""Офлайн-тесты шаблонов и вкладки логов: python3 test_redsms_templates.py -v"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redsms_gui as gui  # noqa: E402
import redsms_templates as tpl  # noqa: E402
import redsms_test as core  # noqa: E402

TO = "+79993332211"
KEY = "super-secret-api-key"


class MatchTest(unittest.TestCase):
    def assert_match(self, template: str, text: str, *values: str) -> None:
        result = tpl.match_template(template, text)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["values"], list(values))

    def test_examples_from_documentation(self) -> None:
        self.assert_match(
            "%w{1,2}, заказ %d отправлен. Трек-номер: %w. Отследить по ссылке: %w",
            "Иван Иванович, заказ №12345 отправлен. Трек-номер: RU1234566789. Отследить по ссылке: pochta.ru/id/ru1234566789",
            "Иван Иванович", "№12345", "RU1234566789", "pochta.ru/id/ru1234566789")
        self.assert_match(
            "%w{1,3}, Вы записаны к врачу %d{1,2}, адрес: %w{1,4}, кабинет: %d.",
            "Иванова Людмила Петровна, Вы записаны к врачу 09.12.2020 17:30, адрес: ул. Пушкина, дом 12, кабинет: 415.",
            "Иванова Людмила Петровна", "09.12.2020 17:30", "ул. Пушкина, дом 12", "415")

    def test_single_value_cannot_contain_space(self) -> None:
        for variable in ("%w", "%d"):
            self.assertFalse(tpl.match_template(f"Код {variable}", "Код 12 34")["ok"])

    def test_number_variables_reject_letters(self) -> None:
        self.assert_match("Сумма %d+ руб", "Сумма 1 000 000 руб", "1 000 000")
        self.assertFalse(tpl.match_template("Сумма %d+ руб", "Сумма сто руб")["ok"])
        self.assertFalse(tpl.match_template("Дата %d{1,2}", "Дата завтра")["ok"])

    def test_word_limit_is_enforced(self) -> None:
        self.assertTrue(tpl.match_template("Имя %w{1,2}", "Имя Иван Иванов")["ok"])
        self.assertFalse(tpl.match_template("Имя %w{1,2}", "Имя Иван Иванович Иванов")["ok"])

    def test_special_characters_in_template_are_literal(self) -> None:
        self.assert_match("Цена (%d) + 5?", "Цена (10) + 5?", "10")
        self.assertFalse(tpl.match_template("Цена (%d) + 5?", "Цена 10 + 5")["ok"])

    def test_percent_without_variable_is_text(self) -> None:
        self.assert_match("Скидка 50% на %w", "Скидка 50% на всё", "всё")

    def test_mismatch_reason_names_the_problem(self) -> None:
        self.assertIn("для входа", tpl.match_template("Код %d для входа", "Код 123 на вход")["reason"])
        self.assertIn("%d", tpl.match_template("Код %d", "Код ")["reason"])
        self.assertIn("лишнее", tpl.match_template("Код %d.", "Код 12. Спасибо")["reason"])

    def test_whole_text_must_match(self) -> None:
        self.assertFalse(tpl.match_template("Код %d", "Ваш Код 12")["ok"])

    def test_catastrophic_input_finishes_quickly(self) -> None:
        started = time.monotonic()
        tpl.match_template("%w{1,20} %w{1,20} x", "а " * 500 + "б")
        self.assertLess(time.monotonic() - started, 2)


class LintTest(unittest.TestCase):
    def test_valid_template_has_no_problems(self) -> None:
        self.assertEqual(tpl.lint_template("Код %d, скидка 50%, имя %w{1,3}, даты %d{1,2}, суммы %d+"), [])

    def test_documented_limits(self) -> None:
        self.assertTrue(tpl.lint_template("%w{1,21}"))
        self.assertTrue(tpl.lint_template("%d{1,1}"))
        self.assertTrue(tpl.lint_template("%d{1,21}"))
        self.assertEqual(tpl.lint_template("%w{1,20} %d{1,20}"), [])

    def test_adjacent_word_variables_forbidden(self) -> None:
        self.assertTrue(tpl.lint_template("%w{1,7} %w{1,4}"))
        self.assertEqual(tpl.lint_template("%w{1,7}, адрес %w{1,4}"), [])

    def test_unknown_variable_reported(self) -> None:
        self.assertIn("%s", tpl.lint_template("Привет %s")[0])


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "t.json")
        self.store = tpl.TemplateStore(self.path)

    def good(self, **extra: Any) -> Dict[str, Any]:
        return {"name": "Код", "channel": "sms", "text": "Код %d", "traffic_type": "auth", **extra}

    def test_create_update_delete_persist(self) -> None:
        saved = self.store.save(self.good())
        self.assertEqual(tpl.TemplateStore(self.path).list()[0]["text"], "Код %d")
        self.store.save(self.good(id=saved["id"], name="Новое"))
        self.assertEqual([t["name"] for t in self.store.list()], ["Новое"])
        self.store.delete(saved["id"])
        self.assertEqual(self.store.list(), [])

    def test_validation(self) -> None:
        for bad in (self.good(name=""), self.good(text=" "), self.good(channel="x"), self.good(traffic_type="x"),
                    self.good(name=1), self.good(text="x" * (tpl.MAX_TEXT + 1))):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.store.save(bad)
        self.assertFalse(os.path.exists(self.path))

    def test_unknown_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save(self.good(id="nope"))
        with self.assertRaises(ValueError):
            self.store.delete("nope")

    def test_corrupt_file_is_not_overwritten(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{сломано")
        with self.assertRaisesRegex(ValueError, "прочитать"):
            self.store.save(self.good())
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{сломано")

    def test_extra_fields_are_not_stored(self) -> None:
        self.store.save(self.good(api_key="secret"))
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("secret", f.read())

    def test_describe_extracts_example_values(self) -> None:
        described = tpl.describe({"channel": "sms", "text": "Код %d для %w", "example": "Код 12 для Ани"})
        self.assertEqual(described["example_values"], ["12", "Ани"])
        self.assertEqual([p for p in described["parts"] if "var" in p], [{"var": "%d"}, {"var": "%w"}])

    def test_describe_keeps_vk_text_whole(self) -> None:
        described = tpl.describe({"channel": "vk", "text": "Заказ {order} %d", "example": ""})
        self.assertEqual(described["parts"], [{"literal": "Заказ {order} %d"}])


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fake = core.FakeRedsms()
        self.addCleanup(self.fake.close)
        self.app = gui.App(base_url=self.fake.base_url, interval=0, open_log=False, log_dir=self.directory.name,
                           templates_path=os.path.join(self.directory.name, "t.json"))
        self.server = gui.make_server(self.app, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, token: bool = True) -> Tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Token"] = self.app.token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def run_check(self, **extra: Any) -> Dict[str, Any]:
        form = {"login": "user", "api_key": KEY, "to": TO, "services": ["hlr"], **extra}
        self.assertEqual(self.call("POST", "/api/start", form)[0], 200)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snap = self.call("GET", "/api/status")[1]
            if snap["state"] != "running":
                return snap
            time.sleep(0.01)
        self.fail("проверка не завершилась")

    def write_log(self, name: str, text: str = "x") -> str:
        path = os.path.join(self.directory.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    # --- логи ---

    def test_every_log_endpoint_requires_token(self) -> None:
        for path in ("/api/log", "/api/logs", "/api/logs/view?name=redsms_test_20260101-000000.log", "/api/templates"):
            with self.subTest(path):
                self.assertEqual(self.call("GET", path, token=False)[0], 403)

    def test_live_log_empty_before_first_run(self) -> None:
        self.assertEqual(self.call("GET", "/api/log")[1], {"run": None, "entries": [], "total": 0, "truncated": False})

    def test_live_log_has_requests_levels_and_no_secrets(self) -> None:
        self.run_check()
        reply = self.call("GET", "/api/log")[1]
        text = "\n".join(e["text"] for e in reply["entries"])
        self.assertIn("POST /message?fields=all", text)
        self.assertIn("HTTP 200", text)
        self.assertIn("+7999***11", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn(TO, text)
        self.assertNotIn(self.fake.calls[0]["headers"]["secret"], text)
        self.assertTrue(all(e["level"] and e["time"] for e in reply["entries"]))

    def test_live_log_since_returns_only_new_entries(self) -> None:
        self.run_check()
        total = self.call("GET", "/api/log")[1]["total"]
        self.assertEqual(self.call("GET", f"/api/log?since={total}")[1]["entries"], [])
        self.assertEqual(len(self.call("GET", f"/api/log?since={total - 1}")[1]["entries"]), 1)

    def test_new_run_gets_new_log(self) -> None:
        first = self.run_check()
        count = self.call("GET", "/api/log")[1]["total"]
        second = self.run_check()
        reply = self.call("GET", "/api/log")[1]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(reply["run"], second["id"])
        self.assertEqual(reply["total"], count)

    def test_polling_is_logged_at_debug(self) -> None:
        self.fake.statuses[core.UUID] = [{"status": "progress"}, {"status": "delivered"}]
        self.run_check(mode="live", confirm="ДА")
        levels = {e["level"] for e in self.call("GET", "/api/log")[1]["entries"]}
        self.assertIn("DEBUG", levels)
        self.assertIn("INFO", levels)

    def test_handler_removed_after_run(self) -> None:
        self.run_check()
        self.assertEqual(len(core.log.handlers), 1)

    def test_lists_only_real_log_files_newest_first(self) -> None:
        self.write_log("redsms_test_20260101-000000.log")
        self.write_log("redsms_test_20260202-000000.log")
        self.write_log("secret.txt")
        self.write_log("wcall_live_20260101-000000.log")
        names = [f["name"] for f in self.call("GET", "/api/logs")[1]["files"]]
        self.assertEqual(names, ["redsms_test_20260202-000000.log", "redsms_test_20260101-000000.log"])

    def test_reads_log_file(self) -> None:
        self.write_log("redsms_test_20260101-000000.log", "содержимое")
        status, reply = self.call("GET", "/api/logs/view?name=redsms_test_20260101-000000.log")
        self.assertEqual(status, 200)
        self.assertEqual(reply["text"], "содержимое")

    def test_large_log_returns_tail(self) -> None:
        self.write_log("redsms_test_20260101-000000.log", "a" * gui.MAX_LOG_VIEW + "конец")
        reply = self.call("GET", "/api/logs/view?name=redsms_test_20260101-000000.log")[1]
        self.assertTrue(reply["truncated"])
        self.assertTrue(reply["text"].endswith("конец"))

    def test_path_traversal_rejected(self) -> None:
        self.write_log("secret.txt", "тайна")
        for name in ("../secret.txt", "secret.txt", "/etc/passwd", "redsms_test_20260101-000000.log/../secret.txt",
                     "redsms_test_20260101-000000.log\n", ""):
            with self.subTest(name):
                status, reply = self.call("GET", "/api/logs/view?name=" + urllib.parse.quote(name))
                self.assertEqual(status, 400)
                self.assertNotIn("тайна", json.dumps(reply, ensure_ascii=False))

    def test_missing_log_is_400(self) -> None:
        self.assertEqual(self.call("GET", "/api/logs/view?name=redsms_test_20990101-000000.log")[0], 400)

    # --- шаблоны ---

    def test_template_endpoints_require_token(self) -> None:
        for path in ("/api/templates", "/api/templates/delete", "/api/templates/check"):
            with self.subTest(path):
                self.assertEqual(self.call("POST", path, {}, token=False)[0], 403)

    def test_template_crud_through_server(self) -> None:
        status, reply = self.call("POST", "/api/templates", {"name": "Код", "channel": "sms", "text": "Код %d",
                                                             "traffic_type": "auth", "example": "Код 12"})
        self.assertEqual(status, 200)
        template = reply["template"]
        self.assertEqual(template["example_values"], ["12"])
        listed = self.call("GET", "/api/templates")[1]["templates"]
        self.assertEqual([t["id"] for t in listed], [template["id"]])
        self.assertEqual(self.call("POST", "/api/templates/delete", {"id": template["id"]})[0], 200)
        self.assertEqual(self.call("GET", "/api/templates")[1]["templates"], [])

    def test_invalid_template_is_400(self) -> None:
        self.assertEqual(self.call("POST", "/api/templates", {"channel": "sms", "text": "x"})[0], 400)

    def test_check_endpoint(self) -> None:
        reply = self.call("POST", "/api/templates/check", {"template": "Код %d", "text": "Код 12"})[1]
        self.assertTrue(reply["match"]["ok"])
        reply = self.call("POST", "/api/templates/check", {"template": "%w{1,99}"})[1]
        self.assertTrue(reply["problems"])
        self.assertNotIn("match", reply)
        self.assertEqual(self.call("POST", "/api/templates/check", {"template": 5})[0], 400)

    def test_checks_never_call_redsms(self) -> None:
        self.call("POST", "/api/templates", {"name": "К", "channel": "sms", "text": "Код %d"})
        self.call("POST", "/api/templates/check", {"template": "Код %d", "text": "Код 1"})
        self.call("GET", "/api/templates")
        self.assertEqual(self.fake.calls, [])

    def test_filled_template_text_is_sent_and_traffic_checked(self) -> None:
        self.fake.send_replies = [core.sent(traffic_type="market")]
        snap = self.run_check(services=["sms"], sms_text="Код 12", sms_from="Company", expect_traffic="auth")
        body = self.fake.posts()[0]["body"]
        self.assertEqual((body["text"], body["from"]), ("Код 12", "Company"))
        result = {r["name"]: r for r in snap["results"]}["sms"]
        self.assertEqual(result["outcome"], "ОШИБКА")
        self.assertIn("не совпал с шаблоном", result["detail"])

    def test_bad_expected_traffic_rejected(self) -> None:
        form = {"login": "u", "api_key": "k", "to": TO, "services": ["sms"], "expect_traffic": "x"}
        self.assertEqual(self.call("POST", "/api/start", form)[0], 400)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
