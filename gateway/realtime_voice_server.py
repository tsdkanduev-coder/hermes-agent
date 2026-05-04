"""Standalone OpenAI/xAI Realtime voice server with GigaCaller-compatible WSS interface.

Используется для локальной разработки и бенчмаркинга без VPN и без доступа
к реальному GigaCaller-шлюзу.

Протокол тот же, что у GigaCaller:
  1. Сервер отправляет приветствие.
  2. Клиент присылает initialRequest (systemPrompt, phoneNumber, voice, model).
  3. Сервер проводит разговор через OpenAI/xAI Realtime и отдаёт транскрипции:
       {"type": "transcription", "data": {"source": "peer"|"model", "text": "...", "seqNum": N}}
  4. Закрывает соединение после завершения разговора.

Запуск:
  # Интерактивный режим — вы играете сотрудника заведения:
  OPENAI_API_KEY=sk-... python gateway/realtime_voice_server.py

  # Скриптованный режим:
  OPENAI_API_KEY=sk-... python gateway/realtime_voice_server.py \\
    --operator "Ресторан Sage" --operator "На какое время?" --operator "Записали"

  # xAI вместо OpenAI:
  XAI_API_KEY=... python gateway/realtime_voice_server.py --provider xai

Укажи клиенту адрес сервера вместо реального GigaCaller:
  GIGACALLER_WSS_URL=ws://localhost:8766/v1/ws/ \\
  GIGACALLER_INSECURE_SSL=1 \\
  VOICE_CALL_BACKEND=gigacaller \\
  python scripts/render_gateway_proxy.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8766
WS_PATH = "/v1/ws/"

DEFAULT_OPENAI_MODEL = "gpt-4o-realtime-preview"
DEFAULT_XAI_MODEL = "grok-2-vision-1212"


# ---------------------------------------------------------------------------
# Текстовый мост к OpenAI/xAI Realtime
# ---------------------------------------------------------------------------

class _TextRealtimeBridge:
    """RealtimeVoiceBridge в текстовом режиме (без аудио).

    Принимает реплику оператора как текст → возвращает ответ голосовой модели.
    Архитектурно повторяет RealtimeVoiceBridge, но:
      - modalities: ["text"] вместо ["text", "audio"]
      - turn_detection отключён (мы сами управляем очерёдностью)
      - вместо аудио-callback'ов — простой await reply(text)
    """

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        instructions: str,
        language: str = "ru",
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.instructions = instructions
        self.language = language

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None

        # Синхронизация: ответ готов
        self._reply_ready = asyncio.Event()
        self._reply_text = ""
        self._reply_error: str | None = None

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        if self.provider == "xai":
            await self._connect_xai()
        else:
            await self._connect_openai()
        self._reader = asyncio.create_task(self._read_events(), name="realtime-reader")

    async def _connect_openai(self) -> None:
        assert self._session
        self._ws = await self._session.ws_connect(
            f"wss://api.openai.com/v1/realtime?model={self.model}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
            heartbeat=20,
        )
        await self._ws.send_json({
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "instructions": self.instructions,
                "temperature": 0.7,
                "turn_detection": None,  # ручное управление
            },
        })

    async def _connect_xai(self) -> None:
        assert self._session
        base = os.environ.get("XAI_REALTIME_URL", "wss://api.x.ai/v1/realtime").strip()
        sep = "&" if "?" in base else "?"
        self._ws = await self._session.ws_connect(
            f"{base}{sep}model={self.model}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            heartbeat=20,
        )
        await self._ws.send_json({
            "type": "session.update",
            "session": {
                "instructions": self.instructions,
                "turn_detection": None,
            },
        })

    async def reply(self, operator_text: str, timeout: float = 30.0) -> str:
        """Отправить реплику оператора, дождаться и вернуть ответ модели."""
        assert self._ws and not self._ws.closed
        self._reply_ready.clear()
        self._reply_text = ""
        self._reply_error = None

        await self._ws.send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": operator_text}],
            },
        })
        await self._ws.send_json({"type": "response.create"})

        try:
            await asyncio.wait_for(self._reply_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return "[таймаут ответа модели]"

        if self._reply_error:
            return f"[ошибка: {self._reply_error}]"
        return self._reply_text

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _read_events(self) -> None:
        assert self._ws
        acc = ""
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            t = str(event.get("type") or "")

            if t in ("response.text.delta", "response.output_text.delta"):
                acc += event.get("delta", "")

            elif t in ("response.text.done", "response.output_text.done"):
                acc = str(event.get("text") or event.get("transcript") or acc).strip()

            elif t == "response.done":
                self._reply_text = acc.strip()
                acc = ""
                self._reply_ready.set()

            elif t == "error":
                self._reply_error = str(event.get("error") or event)
                self._reply_ready.set()


# ---------------------------------------------------------------------------
# GigaCaller-совместимый WebSocket-обработчик
# ---------------------------------------------------------------------------

def _transcription_msg(source: str, text: str, seq: int) -> str:
    return json.dumps(
        {"type": "transcription", "data": {"source": source, "text": text, "seqNum": seq}},
        ensure_ascii=False,
    )


async def _read_line_async(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


FAREWELL = ("до свидания", "спасибо", "всего доброго", "пока", "до встречи")


async def _run_call(
    ws: web.WebSocketResponse,
    system_prompt: str,
    phone: str,
    operator_lines: list[str],
    provider: str,
    api_key: str,
    model: str,
) -> None:
    """Провести один звонок: оператор ↔ Realtime-модель → транскрипции клиенту."""
    bridge = _TextRealtimeBridge(
        provider=provider,
        api_key=api_key,
        model=model,
        instructions=system_prompt,
    )
    try:
        await bridge.connect()
    except Exception as exc:
        logger.error("realtime_voice_server: не удалось подключиться к Realtime API: %s", exc)
        return

    seq = 1
    interactive = not operator_lines

    try:
        turn = 0
        while True:
            # ── Реплика оператора ──────────────────────────────────────────
            if interactive:
                try:
                    op_line = await _read_line_async(
                        "Алло" if turn == 0 else "Сотрудник (Enter = завершить): "
                    )
                except EOFError:
                    break
                if not op_line.strip():
                    if turn == 0:
                        op_line = "Алло"
                    else:
                        break
            else:
                if turn >= len(operator_lines):
                    break
                op_line = operator_lines[turn]

            print(f"[server] Оператор [{turn+1}]: {op_line}")

            # Отправляем транскрипцию оператора клиенту
            await ws.send_str(_transcription_msg("peer", op_line, seq))
            seq += 1

            # ── Ответ голосовой модели ─────────────────────────────────────
            model_reply = await bridge.reply(op_line)
            print(f"[server] Модель   [{turn+1}]: {model_reply}")

            await ws.send_str(_transcription_msg("model", model_reply, seq))
            seq += 1

            turn += 1

            if any(m in model_reply.lower() for m in FAREWELL):
                print("[server] Модель попрощалась, завершаем звонок")
                break

    finally:
        await bridge.close()


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    operator_lines: list[str] = request.app["operator_lines"]
    provider: str = request.app["provider"]
    model: str = request.app["model"]
    api_key: str = request.app["api_key"]

    # 1. Приветствие
    await ws.send_str(json.dumps({"type": "greeting", "data": {"server": "realtime-voice-server"}}))

    # 2. initialRequest
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("realtime_voice_server: таймаут ожидания initialRequest")
        return ws

    if msg.type != aiohttp.WSMsgType.TEXT:
        return ws

    try:
        payload = json.loads(msg.data)
    except json.JSONDecodeError:
        return ws

    if payload.get("type") != "initialRequest":
        logger.warning("realtime_voice_server: ожидался initialRequest, получен %s", payload.get("type"))
        return ws

    data: dict[str, Any] = payload.get("data") or {}
    system_prompt = str(data.get("systemPrompt") or "")
    phone = str(data.get("phoneNumber") or "unknown")
    # gigachatModel — новый ключ; model — старый (backwards compat)
    req_model = str(data.get("gigachatModel") or data.get("model") or "").strip() or model
    asr_model = str(data.get("asrModel") or "")
    enable_denoiser = data.get("enableDenoiser")

    extras = []
    if asr_model:
        extras.append(f"asr={asr_model}")
    if enable_denoiser is not None:
        extras.append(f"denoiser={enable_denoiser}")
    extras_str = "  " + " ".join(extras) if extras else ""
    print(f"[server] Звонок на {phone} | модель {req_model}{extras_str} | промпт {len(system_prompt)} симв.")

    # 3. Разговор
    await _run_call(ws, system_prompt, phone, operator_lines, provider, api_key, req_model)

    await ws.close()
    return ws


# ---------------------------------------------------------------------------
# Запуск сервера
# ---------------------------------------------------------------------------

async def _main_async(args: argparse.Namespace) -> None:
    provider = args.provider
    if provider == "xai":
        api_key = os.environ.get("XAI_API_KEY", "").strip()
        if not api_key:
            print("Нужен XAI_API_KEY.", file=sys.stderr)
            sys.exit(1)
        model = args.model or DEFAULT_XAI_MODEL
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            print("Нужен OPENAI_API_KEY.", file=sys.stderr)
            sys.exit(1)
        model = args.model or DEFAULT_OPENAI_MODEL

    app = web.Application()
    app["operator_lines"] = args.operator or []
    app["provider"] = provider
    app["model"] = model
    app["api_key"] = api_key
    app.router.add_get(WS_PATH, _ws_handler)

    mode = "скриптованный" if args.operator else "интерактивный"
    print(f"[server] realtime-voice-server | провайдер={provider} | модель={model} | режим={mode}")
    print(f"[server] Слушаю ws://{args.host}:{args.port}{WS_PATH}")
    print()
    print("Укажи клиенту:")
    print(f"  GIGACALLER_WSS_URL=ws://{args.host}:{args.port}{WS_PATH}")
    print()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args()
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\n[server] Остановлен")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--provider", choices=["openai", "xai"], default="openai",
        help="Провайдер Realtime API (по умолч. openai)",
    )
    p.add_argument(
        "--model", default="",
        help=f"Модель (по умолч. {DEFAULT_OPENAI_MODEL} / {DEFAULT_XAI_MODEL})",
    )
    p.add_argument(
        "--operator", action="append", metavar="LINE",
        help="Реплика оператора (повторить для нескольких реплик). Без флага — интерактивный режим.",
    )
    return p.parse_args()


if __name__ == "__main__":
    main()
