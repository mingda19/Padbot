"""A stand-in for api.telegram.org so the real Application, ConversationHandler
and handlers run in tests: no network, no token. PTB's documented extension
point is BaseRequest; everything above it is the production code."""

import json

from telegram.request import BaseRequest

USER = {"id": 42, "is_bot": False, "first_name": "Tester"}
CHAT = {"id": 42, "type": "private"}


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.reject_photos: set[str] = set()  # sendPhoto values answered with HTTP 400
        self.reject_urls = False  # Telegram "can't fetch that URL"
        self._message_id = 100
        self._files = 0

    # BaseRequest interface
    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5.0

    def sent(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data and "reply_markup" in request_data.json_parameters:
            params["reply_markup"] = json.loads(request_data.json_parameters["reply_markup"])
        self.calls.append((name, params))

        def message(**extra):
            self._message_id += 1
            return {"message_id": self._message_id, "date": 1700000000, "chat": {"id": params.get("chat_id", 42), "type": "private"}, **extra}

        if name == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Padbot", "username": "padbot_test_bot"}
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            result = message(text=params.get("text", ""))
        elif name == "sendPhoto":
            photo = params["photo"]
            by_url = photo.startswith("http")
            if photo in self.reject_photos or (by_url and self.reject_urls):
                body = {"ok": False, "error_code": 400, "description": "Bad Request: wrong file identifier/HTTP URL specified"}
                return 400, json.dumps(body).encode()
            self._files += 1
            file_id = f"FILE_{self._files}" if by_url else photo
            result = message(photo=[{"file_id": file_id, "file_unique_id": "u", "width": 1, "height": 1}])
        else:  # answerCallbackQuery, setMyCommands, ...
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


class Conversation:
    """Builds the updates a Telegram client would send, and feeds them in."""

    def __init__(self, app):
        self.app, self._id = app, 0

    def _next(self) -> int:
        self._id += 1
        return self._id

    async def _send(self, payload: dict) -> None:
        from telegram import Update

        await self.app.process_update(Update.de_json({"update_id": self._next(), **payload}, self.app.bot))

    async def command(self, text: str) -> None:
        name = text.split()[0]
        await self._send({"message": {"message_id": self._next(), "date": 1700000000, "chat": CHAT, "from": USER, "text": text,
                                      "entities": [{"type": "bot_command", "offset": 0, "length": len(name)}]}})  # fmt: skip

    async def say(self, text: str) -> None:
        await self._send({"message": {"message_id": self._next(), "date": 1700000000, "chat": CHAT, "from": USER, "text": text}})

    async def tap(self, data: str, markup: dict | None = None) -> None:
        message = {"message_id": 7, "date": 1700000000, "chat": CHAT, "text": "earlier message"}
        if markup:
            message["reply_markup"] = markup
        await self._send({"callback_query": {"id": str(self._next()), "from": USER, "chat_instance": "ci", "data": data, "message": message}})
