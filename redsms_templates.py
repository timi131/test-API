"""Шаблоны операторов REDSMS: разбор переменных, проверка текста и локальное хранилище.

Правила переменных: https://docs.redsms.ru/knowledge-base/templates/sms/
Регистрация шаблонов идёт только через личный кабинет, API для неё нет. Поэтому здесь хранится локальная копия,
чтобы подставлять значения и заранее проверять, что отправляемый текст совпадает с шаблоном.
Проверка приблизительная: окончательно решает оператор, а результат виден по типу тарификации сообщения.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from typing import Any, Dict, List, Optional

CHANNELS = ("sms", "vk")
TRAFFIC_TYPES = {
    "auth": "Авторизационный",
    "service": "Сервисный",
    "info": "Информационный",
    "transact": "Транзакционный",
    "market_template": "Рекламный по шаблону",
}
MAX_NAME = 100
MAX_SENDER = 50
MAX_TEXT = 2000
MAX_TEMPLATES = 500

VARIABLE = re.compile(r"%w\{1,(?P<w>\d+)\}|%d\{1,(?P<d>\d+)\}|%d\+|%w|%d")
UNKNOWN_VARIABLE = re.compile(r"%[a-zA-Z]")
ADJACENT_WORDS = re.compile(r"%w\{1,\d+\}\s*%w\{1,\d+\}")
WORD = r"\S+"
NUMBER = r"[^\sA-Za-zА-Яа-яЁё]+"  # цифры и спецсимволы


def split_template(template: str) -> List[Dict[str, str]]:
    """Части шаблона по порядку: {"literal": текст} или {"var": переменная}."""
    parts: List[Dict[str, str]] = []
    position = 0
    for match in VARIABLE.finditer(template):
        if match.start() > position:
            parts.append({"literal": template[position:match.start()]})
        parts.append({"var": match.group()})
        position = match.end()
    if position < len(template):
        parts.append({"literal": template[position:]})
    return parts


def variable_pattern(variable: str) -> str:
    if variable in ("%w", "%d"):
        return WORD
    if variable == "%d+":
        return rf"{NUMBER}(?: +{NUMBER})*"
    match = VARIABLE.fullmatch(variable)
    count = int(match.group("w") or match.group("d"))
    token = WORD if match.group("w") else NUMBER
    return rf"{token}(?: +{token}){{0,{max(count - 1, 0)}}}"


def _pattern(parts: List[Dict[str, str]]) -> str:
    return "".join(re.escape(p["literal"]) if "literal" in p else f"({variable_pattern(p['var'])})" for p in parts)


def lint_template(template: str) -> List[str]:
    """Нарушения правил из документации, из-за которых шаблон могут не принять."""
    problems = []
    for match in VARIABLE.finditer(template):
        if match.group("w") and not 1 <= int(match.group("w")) <= 20:
            problems.append(f"{match.group()}: количество слов должно быть от 1 до 20")
        if match.group("d") and not 2 <= int(match.group("d")) <= 20:
            problems.append(f"{match.group()}: количество чисел должно быть от 2 до 20")
    if ADJACENT_WORDS.search(template):
        problems.append("Нельзя ставить несколько переменных %w{1,n} подряд")
    unknown = sorted(set(UNKNOWN_VARIABLE.findall(VARIABLE.sub("", template))))
    if unknown:
        problems.append("Неизвестные переменные: " + ", ".join(unknown))
    return problems


def _first_mismatch(parts: List[Dict[str, str]], text: str) -> str:
    for index, part in enumerate(parts):
        if not re.match(_pattern(parts[:index + 1]), text):
            if "literal" in part:
                return f"В тексте нет или изменён фрагмент шаблона: «{part['literal'].strip() or part['literal']}»"
            return f"Значение не подходит для переменной {part['var']}"
    return "В тексте есть лишнее после конца шаблона"


def match_template(template: str, text: str) -> Dict[str, Any]:
    """Совпадает ли текст с шаблоном. values — значения переменных, reason — где расхождение."""
    parts = split_template(template)
    match = re.fullmatch(_pattern(parts), text)
    if match:
        return {"ok": True, "values": list(match.groups()), "reason": ""}
    return {"ok": False, "values": [], "reason": _first_mismatch(parts, text)}


def describe(template: Dict[str, Any]) -> Dict[str, Any]:
    """Шаблон для окна: части для подстановки и значения из примера."""
    result = dict(template)
    if template["channel"] == "sms":
        result["parts"] = split_template(template["text"])
        example = template.get("example") or ""
        result["example_values"] = match_template(template["text"], example)["values"] if example else []
    else:  # в шаблонах VK переменные записываются в произвольном виде, поэтому их не разбираем
        result["parts"] = [{"literal": template["text"]}]
        result["example_values"] = []
    result["problems"] = lint_template(template["text"]) if template["channel"] == "sms" else []
    return result


def _text(data: Dict[str, Any], name: str, limit: int) -> str:
    value = data.get(name) or ""
    if not isinstance(value, str):
        raise ValueError(f"Поле {name} должно быть строкой")
    if len(value) > limit:
        raise ValueError(f"Поле {name} длиннее {limit} символов")
    return value.strip()


def clean_template(data: Dict[str, Any]) -> Dict[str, str]:
    channel = data.get("channel")
    if channel not in CHANNELS:
        raise ValueError("Канал шаблона должен быть SMS или VK")
    traffic_type = data.get("traffic_type") or ""
    if traffic_type and traffic_type not in TRAFFIC_TYPES:
        raise ValueError("Неизвестный тип шаблона")
    cleaned = {"channel": channel, "traffic_type": traffic_type, "name": _text(data, "name", MAX_NAME),
               "sender": _text(data, "sender", MAX_SENDER), "text": _text(data, "text", MAX_TEXT),
               "example": _text(data, "example", MAX_TEXT)}
    if not cleaned["name"]:
        raise ValueError("Укажите название шаблона")
    if not cleaned["text"]:
        raise ValueError("Укажите текст шаблона")
    return cleaned


class TemplateStore:
    """Шаблоны в JSON-файле. Повреждённый файл не перезаписывается, чтобы не потерять данные."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def _read(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise ValueError(f"Не удалось прочитать файл шаблонов {self.path}: {e}") from None
        if not isinstance(data, list):
            raise ValueError(f"Файл шаблонов {self.path} повреждён: ожидался список")
        return data

    def _write(self, templates: List[Dict[str, Any]]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(templates, f, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)  # файл либо старый, либо новый целиком
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self._read()

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = clean_template(data)
        template_id: Optional[str] = data.get("id") or None
        with self.lock:
            templates = self._read()
            if template_id:
                for index, existing in enumerate(templates):
                    if existing.get("id") == template_id:
                        templates[index] = {"id": template_id, **cleaned}
                        self._write(templates)
                        return templates[index]
                raise ValueError("Шаблон не найден")
            if len(templates) >= MAX_TEMPLATES:
                raise ValueError(f"Шаблонов не может быть больше {MAX_TEMPLATES}")
            template = {"id": secrets.token_hex(8), **cleaned}
            templates.append(template)
            self._write(templates)
            return template

    def delete(self, template_id: Any) -> None:
        with self.lock:
            templates = self._read()
            remaining = [t for t in templates if t.get("id") != template_id]
            if len(remaining) == len(templates):
                raise ValueError("Шаблон не найден")
            self._write(remaining)
