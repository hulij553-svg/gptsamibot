"""WebSocket fan-out для прогресса генерации картинок.

Расширение оригинального ConnectionManager: вместо одного прогресса шлём стадии
queued/estimating/dispatching/generating/saving/done/failed/partial + счётчик
done/total и опц. массив свежих превью по мере готовности.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState


class ConnectionManager:
    def __init__(self):
        self.connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, telegram_id: int, websocket: WebSocket):
        await websocket.accept()
        self.connections.setdefault(telegram_id, []).append(websocket)

    def disconnect(self, telegram_id: int, websocket: Optional[WebSocket] = None):
        if telegram_id not in self.connections:
            return

        if websocket is None:
            self.connections[telegram_id] = [
                item for item in self.connections[telegram_id] if item.client_state == WebSocketState.CONNECTED
            ]
        else:
            self.connections[telegram_id] = [item for item in self.connections[telegram_id] if item is not websocket]

        if not self.connections[telegram_id]:
            del self.connections[telegram_id]

    async def send_progress(
        self,
        telegram_id: int,
        job_id: str,
        stage: str,
        message: str,
        *,
        progress: int = 0,
        done_count: int = 0,
        total_count: int = 0,
        previews: Optional[List[str]] = None,
        tokens: Optional[int] = None,
        cost_rub: Optional[float] = None,
    ):
        sockets = self.connections.get(telegram_id)
        if not sockets:
            return

        payload: Dict[str, Any] = {
            "job_id": job_id,
            "stage": stage,
            "message": message,
            "progress": max(0, min(progress, 100)),
            "done_count": done_count,
            "total_count": total_count,
            "previews": previews or [],
            "timestamp": time.time(),
        }
        if tokens is not None:
            payload["tokens"] = tokens
        if cost_rub is not None:
            payload["cost_rub"] = cost_rub
        disconnected: List[WebSocket] = []

        for websocket in sockets:
            try:
                await websocket.send_json(payload)
            except Exception:
                disconnected.append(websocket)

        for websocket in disconnected:
            self.disconnect(telegram_id, websocket)


manager = ConnectionManager()
