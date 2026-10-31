#!/usr/bin/env python3
"""Переключатель ИИ для сторожа: выбор модели — это данные, а не код.

Зачем. Раньше модель была вплетена в код: `sandbox.py` собирал
`ClaudeAgentOptions`, а юнит вписывал `WATCHDOG_MODEL=claude-sonnet-5`. Чтобы
поменять модель, надо было править код или юнит и передеплоивать. 30.10.2026
подписка Anthropic потребовала верификации организации — API стал отдавать
«Your account can't receive replies from Claude right now», и сторож остался
без разбора. Менять модель в такой момент хочется **быстро**, а не «сначала
придумай патч на пол-экрана».

Теперь всё, что нужно знать про модель, лежит в `ai.json` рядом с этим файлом:

    {
      "normal":  {"provider": "minimax", "model": "minimax/MiniMax-M3.1-Flash"},
      "crit":    {"provider": "minimax", "model": "minimax/MiniMax-M3.1-Flash",
                  "effort": "high"},
      "fallback": [{"provider": "anthropic", "model": "claude-sonnet-5"}]
    }

Поменять ИИ = поставить другую строку. Сторож одноразовый (раз в пять минут),
поэтому новая модель подхватывается следующим проходом, без рестартов.

Провайдеры:

* `anthropic` — Claude Agent SDK, как раньше: подписка владельца, доступ к
  файлам каталога инцидента инструментами Read/Grep/Glob.
* `minimax` — MiniMax Code в режиме lightweight: у модели нет **ни одного**
  инструмента, содержимое инцидента подкладывает наш код. Граница read-only
  здесь даже строже, чем у Anthropic: модель физически не может ни прочитать
  файл, ни что-то выполнить.
* `openai` — любой OpenAI-совместимый адрес: локальный ollama, vLLM, groq.

Заказ идёт по цепочке: отказал первый — берётся следующий, и в отчёте видно,
кто именно ответил. Это то, чего не было: раньше падение Anthropic означало
«разбора нет».

Что ещё можно менять без правки кода:

* `WATCHDOG_AI_CONFIG` — путь к другому файлу конфигурации;
* `WATCHDOG_AI_PROVIDER` — зафиксировать провайдера для обоих уровней
  (быстрый переключатель «на MiniMax» / «на Anthropic»);
* `WATCHDOG_MODEL`, `WATCHDOG_MODEL_CRIT` — имена моделей Anthropic (как было).
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "ai.json")

# Значения по умолчанию — на случай, если файла нет или он битый.
# Провайдер здесь anthropic, потому что так было раньше, и это же поведение
# должно сохраниться, если конфигурации нет.
DEFAULTS = {
    "normal": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "crit": {"provider": "anthropic", "model": "claude-opus-5"},
}

# Настройки, которые провайдер умеет сам, если их не указали.
PROVIDER_DEFAULTS = {
    "anthropic": {
        "timeout_sec": 600,
        "max_turns": 12,
    },
    "minimax": {
        # Профиль MiniMax Code. По умолчанию — профиль владельца: отдельный
        # требует `mcode login`, а шарить рабочий профиль безопасно
        # (проверено: два процесса одновременно работают без вышибания входа).
        "data_dir": "/home/plg/.minimax",
        "mcode": "/home/plg/.minimax-code/bin/mcode",
        "model": "minimax/MiniMax-M3.1-Flash",
        "effort": "medium",
        "timeout_sec": 300,
        "max_steps": 6,
    },
    "openai": {
        "base_url": "http://127.0.0.1:11434/v1",
        "timeout_sec": 180,
        "max_tokens": 2000,
    },
}

KNOWN_PROVIDERS = tuple(PROVIDER_DEFAULTS)

# Запас на случай, если файла конфигурации нет или он битый. Раньше сторож
# в этом случае просто не разбирал ничего; теперь у него есть хотя бы одна
# запасная модель.
BUILTIN_FALLBACK = {
    "normal": [{"provider": "minimax", "model": "minimax/MiniMax-M3.1-Flash"}],
    "crit": [{"provider": "minimax", "model": "minimax/MiniMax-M3.1-Flash"}],
}


def config_path() -> str:
    """Где лежит конфигурация. Проверяют по очереди: переменная, потом файл рядом."""
    return os.environ.get("WATCHDOG_AI_CONFIG") or DEFAULT_CONFIG


def load() -> dict:
    """Читает конфигурацию. Битый файл — не повод валить сторож."""
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _entry(cfg: dict, tier: str) -> dict:
    entry = cfg.get(tier)
    return dict(entry) if isinstance(entry, dict) else {}


def _env_model(tier: str) -> str | None:
    """Имена моделей Anthropic остались в переменных — их многие уже знают."""
    if tier == "crit":
        return os.environ.get("WATCHDOG_MODEL_CRIT")
    return os.environ.get("WATCHDOG_MODEL")


def resolve(tier: str = "normal", model: str | None = None,
            provider: str | None = None) -> dict:
    """Одно описание модели для уровня `tier` ("normal" или "crit").

    Порядок: файл конфигурации → переменные → умолчания. Явный `model`
    аргументом побеждает всё: им пользуется `analyst.choose_model`, который
    решает, обычная модель или сильная.
    """
    cfg = load()
    tier = tier if tier in ("normal", "crit") else "normal"

    default = DEFAULTS.get(tier, {})
    entry = _entry(cfg, tier)          # из файла конфигурации

    prov = (provider or os.environ.get("WATCHDOG_AI_PROVIDER")
            or entry.get("provider") or default.get("provider"))
    prov = str(prov).strip().lower()

    spec = dict(PROVIDER_DEFAULTS.get(prov, {}))
    for key, value in entry.items():
        if key == "provider":
            continue
        spec[key] = value

    # Модель: аргумент вызывающего → файл конфигурации → переменная окружения
    # → умолчание провайдера. Порядок именно такой: вызывающий решил, на какой
    # модели разбирать (обычная или сильная), и файл не должен ему мешать.
    name = model or entry.get("model")
    if not name and prov == "anthropic":
        name = _env_model(tier)
    if not name:
        name = spec.get("model") or default.get("model")
    if name:
        spec["model"] = str(name)

    spec["provider"] = prov
    spec["tier"] = tier
    return spec


def chain(tier: str = "normal") -> list[dict]:
    """Список моделей по очереди: сначала выбранная, потом запасные.

    Запасные берутся из `fallback` (обычный уровень) или `fallback_crit`
    (критический). Если и их нет — откат на дефолтный Anthropic, чтобы
    сторож не остался вовсе без разбора ни при каком содержимом конфигурации.
    """
    cfg = load()
    tier = tier if tier in ("normal", "crit") else "normal"

    primary = resolve(tier)
    specs = [primary]

    keys = ["fallback_crit", "fallback"] if tier == "crit" else ["fallback"]
    configured = False
    for key in keys:
        items = cfg.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            prov = str(item.get("provider", "anthropic")).strip().lower()
            if prov not in KNOWN_PROVIDERS:
                continue
            configured = True
            spec = dict(PROVIDER_DEFAULTS.get(prov, {}))
            spec.update({k: v for k, v in item.items() if k != "provider"})
            spec["provider"] = prov
            spec["tier"] = tier
            # У запасного провайдера модель, если она не названа, берётся из
            # переменной окружения, как и раньше: `WATCHDOG_MODEL` в юните
            # по-прежнему значит «какая модель Anthropic» — и теперь ещё
            # «какая модель в запасе».
            if not spec.get("model") and prov == "anthropic":
                spec["model"] = _env_model(tier) or DEFAULTS[tier]["model"]
            specs.append(spec)

    if not configured:
        for item in BUILTIN_FALLBACK.get(tier, []):
            prov = str(item.get("provider", "anthropic")).strip().lower()
            spec = dict(PROVIDER_DEFAULTS.get(prov, {}))
            spec.update({k: v for k, v in item.items() if k != "provider"})
            spec["provider"] = prov
            spec["tier"] = tier
            specs.append(spec)

    # Один и тот же провайдер с той же моделью дважды не нужен.
    seen, unique = set(), []
    for spec in specs:
        key = (spec.get("provider"), spec.get("model"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(spec)
    return unique


def describe() -> str:
    """Человеческое описание выбора — для отчёта и для журнала."""
    lines = []
    for tier in ("normal", "crit"):
        spec = resolve(tier)
        lines.append(f"{tier}: {spec.get('provider')} / {spec.get('model')}")
    return "; ".join(lines)
