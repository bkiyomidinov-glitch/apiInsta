"""Background task manager для InstaPI — WebSocket стриминг логов."""
import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


class TaskManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=6)
        self.active_tasks = {}  # task_id -> {channel, started, description}
        self.stop_flags = {}    # task_id -> threading.Event
        self.connections = set()

    def register(self, ws): self.connections.add(ws)
    def unregister(self, ws): self.connections.discard(ws)

    def get_active_tasks(self):
        return {tid: {"channel": info["channel"],
                      "started": info["started"].isoformat(),
                      "description": info.get("description", "")}
                for tid, info in self.active_tasks.items()}

    def _channel_busy(self, channel):
        return any(info["channel"] == channel for info in self.active_tasks.values())

    def _make_callback(self, task_id, channel, loop):
        def cb(message: str):
            level = "info"
            if any(e in message for e in ["\u274c", "error", "Error", "\u2757"]):
                level = "error"
            elif "\u2705" in message:
                level = "success"
            elif "\u26a0" in message or "\u1F512" in message:
                level = "warning"
            msg = {
                "type": "log", "task_id": task_id, "channel": channel,
                "message": message, "level": level,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), loop)
        return cb

    async def _broadcast(self, message: dict):
        dead = set()
        for ws in self.connections:
            try: await ws.send_json(message)
            except Exception: dead.add(ws)
        self.connections -= dead

    def _finish_task(self, task_id):
        self.active_tasks.pop(task_id, None)
        self.stop_flags.pop(task_id, None)

    def stop_task(self, task_id):
        f = self.stop_flags.get(task_id)
        if f: f.set()

    # ================== ZALIV ==================

    async def start_zaliv(self, ws, data):
        channel = data.get("channel", "zaliv")
        if self._channel_busy(channel):
            await ws.send_json({"type": "error", "message": "Залив уже запущен"})
            return
        task_id = f"zaliv_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_event_loop()
        cb = self._make_callback(task_id, channel, loop)
        ev = threading.Event()
        self.stop_flags[task_id] = ev
        self.active_tasks[task_id] = {"channel": channel, "started": datetime.now(),
                                       "description": f"Залив {len(data.get('accounts', []))} акк"}

        def run():
            from web_api import run_zaliv_web
            try:
                result = run_zaliv_web(
                    accounts=data.get("accounts", []),
                    description=data.get("description", ""),
                    videos_per_account=int(data.get("videos_per_account", 1)),
                    video_folder=data.get("video_folder"),
                    unique_videos=bool(data.get("unique_videos", False)),
                    move_to_done=bool(data.get("move_to_done", True)),
                    uniqualize=bool(data.get("uniqualize", False)),
                    new_uniqalize=bool(data.get("new_uniqalize", False)),
                    use_proxies=bool(data.get("use_proxies", False)),
                    delay_between_videos=int(data.get("delay_between_videos", 2)),
                    delay_after_all_videos=int(data.get("delay_after_all_videos", 30)),
                    callback=cb,
                    stop_check=lambda: ev.is_set(),
                    task_id=task_id,
                )
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "task_completed", "task_id": task_id,
                                     "channel": channel, "result": result}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "task_error", "task_id": task_id,
                                     "channel": channel, "error": str(e)}), loop)
            finally:
                self._finish_task(task_id)

        await self._broadcast({"type": "task_started", "task_id": task_id, "channel": channel})
        self.executor.submit(run)

    # ================== STORY ZALIV ==================

    async def start_story_zaliv(self, ws, data):
        channel = data.get("channel", "story")
        if self._channel_busy(channel):
            await ws.send_json({"type": "error", "message": "Залив сторис уже запущен"})
            return
        task_id = f"story_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_event_loop()
        cb = self._make_callback(task_id, channel, loop)
        ev = threading.Event()
        self.stop_flags[task_id] = ev
        self.active_tasks[task_id] = {"channel": channel, "started": datetime.now(),
                                       "description": f"Сторис {len(data.get('accounts', []))} акк"}

        def run():
            from web_api import run_story_web
            try:
                result = run_story_web(
                    accounts=data.get("accounts", []),
                    stories_per_account=int(data.get("stories_per_account", 1)),
                    media_folder=data.get("media_folder"),
                    unique_media=bool(data.get("unique_media", False)),
                    move_to_done=bool(data.get("move_to_done", True)),
                    besties=bool(data.get("besties", False)),
                    links=data.get("links", []),
                    link_url=data.get("link_url", ""),
                    link_label=data.get("link_label", ""),
                    unique_links=bool(data.get("unique_links", False)),
                    link_pos=data.get("link_pos"),
                    link_variant=data.get("link_variant", "default"),
                    use_proxies=bool(data.get("use_proxies", False)),
                    delay_between_stories=int(data.get("delay_between_stories", 2)),
                    delay_after_all=int(data.get("delay_after_all", 30)),
                    callback=cb,
                    stop_check=lambda: ev.is_set(),
                    task_id=task_id,
                )
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "task_completed", "task_id": task_id,
                                     "channel": channel, "result": result}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "task_error", "task_id": task_id,
                                     "channel": channel, "error": str(e)}), loop)
            finally:
                self._finish_task(task_id)

        await self._broadcast({"type": "task_started", "task_id": task_id, "channel": channel})
        self.executor.submit(run)

    # ================== ADD ACCOUNTS ==================

    async def start_add_accounts(self, ws, data):
        channel = data.get("channel", "add_accounts")
        if self._channel_busy(channel):
            await ws.send_json({"type": "error", "message": "Добавление уже идёт"})
            return
        task_id = f"addacc_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_event_loop()
        cb = self._make_callback(task_id, channel, loop)
        ev = threading.Event()
        self.stop_flags[task_id] = ev
        self.active_tasks[task_id] = {"channel": channel, "started": datetime.now(),
                                       "description": f"Login {len(data.get('accounts', []))} акк"}

        def run():
            from web_api import add_accounts_web_batch
            try:
                result = add_accounts_web_batch(
                    accounts=data.get("accounts", []),
                    callback=cb,
                    stop_check=lambda: ev.is_set(),
                    use_proxies=bool(data.get("use_proxies", False)),
                    task_id=task_id,
                )
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "task_completed", "task_id": task_id,
                                     "channel": channel, "result": result}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast({"type": "task_error", "task_id": task_id,
                                     "channel": channel, "error": str(e)}), loop)
            finally:
                self._finish_task(task_id)

        await self._broadcast({"type": "task_started", "task_id": task_id, "channel": channel})
        self.executor.submit(run)
