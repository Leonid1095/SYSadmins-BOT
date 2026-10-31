#!/usr/bin/env python3
"""Вызов модели: провайдер выбирается данными, а не кодом.

Раньше здесь был единственный путь — Claude Agent SDK. Теперь провайдеров три,
и переключение между ними не требует правки кода (см. `ai_config.py` и
`ai.json`).

Что важно сохранить:

* **Граница read-only.** У Anthropic — `Read`/`Grep`/`Glob` и запрет всего
  остального (настройки в `sandbox.py`). У MiniMax и у OpenAI-совместимого
  адреса инструментов нет вообще: содержимое каталога инцидента подкладывает
  наш код. Модель в этом случае физически не может ни прочитать файл сама, ни
  что-то выполнить.
* **Одна попытка разбора.** Запрос идёт по цепочке: отказал первый провайдер —
  берётся следующий. В отчёт попадает, кто именно ответил. Раньше отказ
  Anthropic означал «разбора нет», и владелец оставался с голыми событиями.
* **Тишина при неудаче — не вариант.** Если не ответил никто, вызывающий сам
  присыпает запасной вердикт (см. `analyst.py`).
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai_config  # noqa: E402
import sandbox    # noqa: E402

# ── Сборка содержимого инцидента для провайдеров без инструментов ──────────

MAX_FILE_CHARS = 24_000
MAX_TOTAL_CHARS = 120_000
MAX_FILES = 40

# Токены и ключи в логах — в модель им не надо, а в переписке владельцу тем
# более. Схема та же, что у сторожа PLGamesBot (`watch_notify.py`).
_SECRET = re.compile(
    r"(oauth:|bearer\s+|(?:access_|refresh_)?token[\"']?\s*[=:]\s*[\"']?|key=|secret=|password=)"
    r"[^\s\"'&,;}]+", re.I)
_BOT_TOKEN = re.compile(r"bot\d{6,}:[\w-]{20,}")
_LONG = re.compile(r"[A-Za-z0-9_\-+/=]{40,}")


def scrub(text: str) -> str:
    text = _BOT_TOKEN.sub("bot…", text)
    text = _SECRET.sub(r"\1…", text)
    return _LONG.sub("…", text)


def _incident_files(cwd: str) -> list[str]:
    """Файлы каталога инцидента в порядке важности: сначала наши, потом контекст."""
    root = os.path.abspath(cwd)
    ordered = []
    for name in ("events.json", "facts.json", "verdict.json"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            ordered.append(path)
    context = os.path.join(root, "context")
    if os.path.isdir(context):
        try:
            names = sorted(os.listdir(context))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(context, name)
            if os.path.isfile(path):
                ordered.append(path)
    return ordered[:MAX_FILES]


def bundle_incident(cwd: str) -> str:
    """Содержимое каталога инцидента — текстом, в рамках бюджета.

    Для провайдеров без инструментов это единственный способ показать модели
    факты. Бюджет жёсткий: разбор не должен упираться в размер контекста.
    """
    root = os.path.abspath(cwd)
    parts, used = [], 0
    for path in _incident_files(root):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_FILE_CHARS)
        except OSError:
            continue
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n… (файл обрезан)"
        rel = os.path.relpath(path, root)
        block = f"<<<FILE:{rel}>>>\n{scrub(text)}\n<<<END>>>"
        if used + len(block) > MAX_TOTAL_CHARS:
            parts.append(f"<<<FILE:{rel}>>>\n… (не влезло в бюджет контекста)")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) if parts else "(файлов инцидента нет)"


def _schema_hint(schema: dict) -> str:
    """Схема ответа словами — для провайдеров, которые не понимают JSON Schema."""
    return ("\n\nВерни ответ строго одним JSON-объектом по этой схеме, без пояснений "
            "и без markdown-ограды:\n" + json.dumps(schema, ensure_ascii=False))


# ── Провайдеры ──────────────────────────────────────────────────────────────

async def _anthropic(spec, prompt, options):
    """Claude Agent SDK — как раньше: подписка владельца, Read/Grep/Glob."""
    from claude_agent_sdk import (  # ленивый импорт: SDK нужен не всем
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    system_prompt = options.system_prompt
    if options.output_schema:
        system_prompt += _schema_hint(options.output_schema)

    # Модель — из спецификации этого провайдера, а не из общих настроек.
    # Общие настройки описывают выбранный вызывающим уровень (обычный или
    # сильный), и для запасного провайдера в цепочке они неверны: 30.10.2026
    # Anthropic вызывался с именем модели MiniMax и падал с «issue with the
    # selected model», то есть запасной был нерабочим по построению.
    model = spec.get("model") or options.model

    sdk_options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=sandbox.ALLOWED_TOOLS,
        disallowed_tools=sandbox.DISALLOWED_TOOLS,
        permission_mode="dontAsk",
        cwd=options.cwd,
        model=model,
        max_turns=options.max_turns,
        setting_sources=[],
    )

    chunks, meta = [], {}
    async for message in query(prompt=prompt, options=sdk_options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            meta = {
                "turns": getattr(message, "num_turns", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "is_error": getattr(message, "is_error", None),
            }
    return "\n".join(chunks), meta


async def _minimax(spec, prompt, options):
    """MiniMax Code в режиме lightweight — у модели нет ни одного инструмента.

    Файлы инцидента подкладывает наш код (`bundle_incident`), поэтому модель не
    может ни прочитать файл, ни выполнить команду. Это строже, чем у
    Anthropic: там инструменты чтения есть, но запрет записи задан явно.
    """
    mcode = options.mcode or spec.get("mcode") or "/home/plg/.minimax-code/bin/mcode"
    data_dir = options.data_dir or spec.get("data_dir")
    timeout = int(options.timeout_sec or 300)

    payload = "\n\n".join([
        options.system_prompt,
        bundle_incident(options.cwd),
        prompt,
    ])

    env = dict(os.environ)
    if data_dir:
        env["MINIMAX_DATA_DIR"] = data_dir

    handle, out_file = tempfile.mkstemp(prefix="watchdog-mm-", suffix=".txt")
    os.close(handle)

    def build_cmd(schema_file=None):
        cmd = [mcode, "exec", "--mode", "lightweight", "--input", "-",
               "--timeout", f"{timeout}s", "--permission", "off",
               "--max-steps", str(options.max_steps)]
        # Модель — из спецификации этого провайдера: у MiniMax своя, и в
        # цепочке запасным может стоять модель другого семейства.
        model = spec.get("model") or options.model
        if model:
            cmd += ["--model", model]
        if options.effort:
            cmd += ["--effort", options.effort]
        if schema_file:
            cmd += ["--output-schema", schema_file]
        cmd += ["-o", out_file]
        return cmd

    schema_file = None
    if options.output_schema:
        schema_file = out_file + ".schema.json"
        with open(schema_file, "w", encoding="utf-8") as f:
            json.dump(options.output_schema, f, ensure_ascii=False)

    async def run_once(use_schema):
        cmd = build_cmd(schema_file if use_schema else None)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=options.cwd, env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(payload.encode()),
                                            timeout + 30)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"MiniMax не ответил за {timeout} с")
        stdout = raw.decode(errors="replace") if raw else ""
        try:
            with open(out_file, encoding="utf-8") as f:
                answer = f.read().strip()
        except OSError:
            answer = ""
        if proc.returncode != 0 or not answer:
            detail = (stdout or "").strip().splitlines()
            tail = detail[-1][:200] if detail else "без пояснения"
            raise RuntimeError(f"MiniMax: код {proc.returncode}: {tail}")
        return answer

    try:
        try:
            answer = await run_once(bool(schema_file))
        except RuntimeError as exc:
            if not schema_file:
                raise
            # Схема — это страховка, а не приговор. Если модель ответила не по
            # схеме, повторяем без неё: промпт и так требует JSON, а разбор
            # лучше лишний раз без машиночитаемой рамки, чем никакого.
            answer = await run_once(False)
            if not options.extra.get("schema_failed"):
                options.extra["schema_failed"] = str(exc)[:200]
        return answer, {"duration_ms": None, "is_error": False}
    finally:
        for path in (out_file, out_file + ".schema.json"):
            try:
                os.unlink(path)
            except OSError:
                pass


async def _openai(spec, prompt, options):
    """Любой OpenAI-совместимый адрес: локальный ollama, vLLM, groq."""
    import httpx

    base_url = (options.base_url or spec.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("для провайдера openai нужен base_url")

    key = None
    if options.api_key_env:
        key = os.environ.get(options.api_key_env)

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    body = {
        "model": spec.get("model") or options.model,
        "messages": [
            {"role": "system", "content": options.system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    if spec.get("max_tokens"):
        body["max_tokens"] = int(spec["max_tokens"])

    timeout = int(options.timeout_sec or 180)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(f"{base_url}/chat/completions",
                                     json=body, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"openai: HTTP {response.status_code}")
        data = response.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"openai: неожиданный ответ ({type(exc).__name__})") from exc
    return text, {"duration_ms": None, "is_error": False}


PROVIDERS = {
    "anthropic": _anthropic,
    "minimax": _minimax,
    "openai": _openai,
}


# ── Предохранитель: не тратить время на заведомо мёртвого провайдера ────────
#
# 30.10.2026: Anthropic требует верификации организации и, скорее всего, её
# не пройдёт. Пока Anthropic стоял в цепочке запасным, это было почти безобидно
# — до него просто не доходило. Но стоило основному провайдеру споткнуться, и
# цепочка упиралась в Anthropic на все 600 секунд таймаута, чтобы услышать то
# же самое «нужна верификация». Разбор при этом не состоялся.
#
# Теперь у каждого провайдера есть состояние: когда он последний раз отвечал и
# когда последний раз отказал. Провайдер, который недавно отказал, пропускается
# на время остывания (`cooldown_sec` в `ai.json`) — и цепочка сразу переходит к
# следующему. Ничего не пропадает: если остывают все, цепочка пробует всех.
#
# Побочная польза: провайдер, который ожил (например, после верификации
# Anthropic), вернётся в работу сам, без правки конфигурации.

HEALTH_FILE = os.environ.get(
    "WATCHDOG_AI_HEALTH_FILE",
    os.path.join(os.environ.get("WATCHDOG_STATE_DIR", "/var/lib/watchdog"),
                 "ai-health.json"))

DEFAULT_COOLDOWN_SEC = 6 * 3600


def _read_health() -> dict:
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_health(health: dict) -> None:
    try:
        os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
        tmp = HEALTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(health, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HEALTH_FILE)
    except OSError:
        pass   # состояние — не повод ронять разбор


def _cooldown_sec(spec: dict) -> int:
    try:
        return int(spec.get("cooldown_sec", DEFAULT_COOLDOWN_SEC))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SEC


def _in_cooldown(health: dict, provider: str) -> bool:
    record = health.get(provider)
    if not isinstance(record, dict):
        return False
    until = record.get("cooldown_until")
    if not isinstance(until, (int, float)):
        return False
    return time.time() < until


def _mark(health: dict, provider: str, ok: bool, error: str = "",
          cooldown_sec: int = DEFAULT_COOLDOWN_SEC) -> None:
    now = time.time()
    record = {"last_seen": int(now)}
    if ok:
        record["ok"] = True
        record["last_ok"] = int(now)
        record.pop("cooldown_until", None)
        record.pop("last_error", None)
    else:
        record["ok"] = False
        record["last_fail"] = int(now)
        record["last_error"] = error[:300]
        record["cooldown_until"] = int(now + cooldown_sec)
    health[provider] = record
    _write_health(health)


def health_report() -> list[dict]:
    """Кто отвечал, кто молчит и до каких пор. Для владельца и для `ai_check.py`."""
    health = _read_health()
    rows = []
    for spec in ai_config.chain("normal"):
        provider = spec.get("provider")
        record = health.get(provider) or {}
        rows.append({
            "provider": provider,
            "model": spec.get("model"),
            "last_ok": record.get("last_ok"),
            "last_fail": record.get("last_fail"),
            "in_cooldown": _in_cooldown(health, provider),
            "cooldown_until": record.get("cooldown_until"),
            "last_error": record.get("last_error"),
        })
    return rows


# ── Диспетчер ───────────────────────────────────────────────────────────────

class AiUnavailable(Exception):
    """Ни один провайдер не ответил."""


async def run(prompt, options):
    """Позвать модель по цепочке провайдеров. Возвращает (текст, meta).

    `meta["provider"]` и `meta["model"]` — кто именно ответил; `meta["failed"]`
    — список тех, кто не ответил и почему; `meta["skipped"]` — кого не стали
    даже спрашивать (остывающий после неудачи). Разбор не пропадает из-за
    одного сломанного провайдера.
    """
    specs = ai_config.chain(options.tier)
    health = _read_health()

    # Остывающих пропускаем — но только если есть кого спросить вместо них.
    alive = [s for s in specs
             if not _in_cooldown(health, s.get("provider"))]
    skipped = [s.get("provider") for s in specs if s not in alive]
    if alive:
        specs = alive
    else:
        skipped = []

    failed = []

    for spec in specs:
        handler = PROVIDERS.get(spec.get("provider"))
        if handler is None:
            failed.append(f"{spec.get('provider')}: неизвестный провайдер")
            continue
        # Модель выбирает вызывающий (обычная или сильная) — и она достаётся
        # тому провайдеру, которого выбрал вызывающий. Именно ему, а не
        # «первому в цикле»: если основной отвалился или остыл, первым в
        # цикле окажется запасной, и модель основного уехала бы к нему.
        if spec.get("provider") == options.provider:
            spec = dict(spec, model=options.model or spec.get("model"))
        try:
            text, meta = await handler(spec, prompt, options)
        except Exception as exc:  # провайдер не ответил — идём к следующему
            message = f"{type(exc).__name__}: {exc}"
            failed.append(f"{spec.get('provider')}/{spec.get('model')}: {message}")
            _mark(health, spec.get("provider"), ok=False, error=message,
                  cooldown_sec=_cooldown_sec(spec))
            continue
        meta = dict(meta or {})
        meta["provider"] = spec.get("provider")
        meta["model"] = spec.get("model")
        _mark(health, spec.get("provider"), ok=True)
        if failed:
            meta["failed"] = failed
        if skipped:
            meta["skipped"] = skipped
        return text or "", meta

    raise AiUnavailable("; ".join(failed) or "нет доступных провайдеров")
