#!/usr/bin/env python3
"""Кто из провайдеров отвечает прямо сейчас.

Сторож сам разбирается в цепочке, но владельцу нужно видеть это без чтения
журнала: после верификации Anthropic он должен увидеть, что провайдер ожил,
не дожидаясь следующего инцидента.

    cd /home/plg/telegram-server-bot
    venv/bin/python watchdog/ai_check.py          # проверка всех из цепочки
    venv/bin/python watchdog/ai_check.py anthropic # проверить одного

Проверка настоящая: каждому провайдеру задаётся тот же простой вопрос, какой
задаёт сторож, и ответ печатается. Провайдер, который не ответил, попадает в
остывающее состояние — как и в бою.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai          # noqa: E402
import ai_config   # noqa: E402
import sandbox     # noqa: E402

QUESTION = "Ответь ровно одним словом: работаешь"


async def probe(spec, options):
    """Спросить конкретного провайдера. Возвращает (текст, ошибка)."""
    handler = ai.PROVIDERS.get(spec.get("provider"))
    if handler is None:
        return None, f"неизвестный провайдер: {spec.get('provider')}"
    provider = spec.get("provider")
    try:
        text, _meta = await handler(spec, QUESTION, options)
        # Проба — это настоящий запрос, значит её результат честно записывается
        # в состояние: провайдер, который ожил (например, после верификации
        # Anthropic), сразу становится доступен запасным, без правки конфигурации.
        ai._mark(ai._read_health(), provider, ok=True)
        return (text or "").strip(), None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        ai._mark(ai._read_health(), provider, ok=False, error=message,
                 cooldown_sec=ai._cooldown_sec(spec))
        return None, message


async def main(argv):
    only = argv[1] if len(argv) > 1 else None
    specs = ai_config.chain("normal")
    if only:
        specs = [s for s in specs if s.get("provider") == only]
        if not specs:
            print(f"провайдер «{only}» в цепочке не найден")
            print("цепочка сейчас:", ", ".join(s.get("provider") for s in ai_config.chain("normal")))
            return 2

    options = sandbox.options(cwd=os.getcwd(), system_prompt=QUESTION,
                              max_turns=2, tier="normal")

    print("Цепочка:", " → ".join(f"{s.get('provider')}/{s.get('model')}" for s in specs))
    print()

    alive = 0
    for spec in specs:
        started = time.time()
        text, error = await probe(spec, options)
        took = time.time() - started
        if error is None:
            alive += 1
            print(f"  ✓ {spec.get('provider'):<10} {spec.get('model'):<32} "
                  f"{took:5.1f} с  →  {text[:60]!r}")
        else:
            print(f"  ✗ {spec.get('provider'):<10} {spec.get('model'):<32} "
                  f"{took:5.1f} с  →  {error[:120]}")

    print()
    if alive:
        print(f"Отвечает: {alive} из {len(specs)}. Сторож возьмёт первого, кто ответит.")
    else:
        print(f"Не отвечает никто из {len(specs)}. Разбора не будет — "
              f"проверьте вход и сеть у провайдера.")
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
