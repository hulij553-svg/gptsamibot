"""AIGate API client: image generation (gpt-image-2) + balance.

Видео-функционал удалён. Оставлена HTTP-обвязка (_request/AIGateError/get_balance/
format_balance) — она переиспользуется и для image-генерации, и для проверки
ключей в пуле. Поверх неё построен GPTImageClient.
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


def stringify_error(value: Any) -> str:
    if value is None:
        return "AIGate request failed"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [stringify_error(item) for item in value]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("message", "detail", "msg", "type", "error"):
            if value.get(key):
                return stringify_error(value[key])
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


@dataclass
class AIGateError(Exception):
    message: str
    status_code: Optional[int] = None
    code: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        prefix = f"[{self.status_code}] " if self.status_code else ""
        suffix = f" ({self.code})" if self.code else ""
        return f"{prefix}{self.message}{suffix}"


class AIGateClient:
    """HTTP-обвязка над AIGate-шлюзом. Базовый URL и авторизация — общие."""

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.base_url = settings.PROVIDER_API_BASE.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        timeout: int = 30,
        **kwargs,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.request(method, url, headers=self.headers, **kwargs) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    data = {"error": {"message": text or resp.reason}}

                if resp.status >= 400:
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        message = stringify_error(err.get("message") or err.get("type") or err)
                        code = err.get("code")
                    else:
                        message = stringify_error(err or data or "AIGate request failed")
                        code = None
                    raise AIGateError(message=message, status_code=resp.status, code=code, payload=data)

                if not isinstance(data, dict):
                    raise AIGateError("Unexpected AIGate response format", payload={"response": data})
                return data

    async def get_balance(self) -> Dict[str, Any]:
        return await self._request("GET", "/balance", timeout=30)

    async def _request_multipart(
        self,
        method: str,
        endpoint: str,
        form: "aiohttp.FormData",
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Запрос с multipart/form-data (для /images/edits с файлами референсов).

        Важно: не задаём Content-Type вручную — aiohttp сам поставит с boundary.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        # Авторизация через заголовок; Content-Type формирует aiohttp.
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.request(method, url, headers=headers, data=form) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    data = {"error": {"message": text or resp.reason}}

                if resp.status >= 400:
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        message = stringify_error(err.get("message") or err.get("type") or err)
                        code = err.get("code")
                    else:
                        message = stringify_error(err or data or "AIGate request failed")
                        code = None
                    raise AIGateError(message=message, status_code=resp.status, code=code, payload=data)

                if not isinstance(data, dict):
                    raise AIGateError("Unexpected AIGate response format", payload={"response": data})
                return data


def format_balance(data: Dict[str, Any]) -> str:
    parts: List[str] = []
    if "balance" in data:
        parts.append(f"Баланс: {data['balance']}")
    if "used" in data:
        parts.append(f"Потрачено: {data['used']}")

    token = data.get("token") or {}
    if token.get("remaining") is not None:
        parts.append(f"Остаток токена: {token['remaining']}")
    if token.get("unlimited_quota"):
        parts.append("Безлимитная квота")

    return "\n".join(parts) if parts else str(data)


# ============================================================
# Image generation: openai/gpt-image-2 через AIGate-шлюз
# ============================================================

@dataclass
class ImageRequest:
    prompt: str
    size: str = "1024x1024"
    quality: str = "medium"            # low|medium|high
    n: int = 1
    resolution: Optional[str] = None   # 1k|2k|4k — опц. подсказка из доки AIGate
    output_format: Optional[str] = None  # png|jpeg|webp — forward модели "when supported"
    # Референсы «как у официалов»: файлы напрямую (multipart). Порядок = @Image1...
    reference_images: List[tuple] = field(default_factory=list)  # [(bytes, filename, content_type), ...]


@dataclass
class ImageResult:
    images_b64: List[str]
    model: str
    usage: Dict[str, Any]
    raw: Dict[str, Any]


def _extract_b64(data: Dict[str, Any]) -> List[str]:
    items = data.get("data") or []
    out: List[str] = []
    for item in items:
        b64 = item.get("b64_json") if isinstance(item, dict) else None
        out.append(b64 or "")
    return out


class GPTImageClient(AIGateClient):
    """Генерация картинок через /images/generations и /images/edits."""

    async def generate(self, req: ImageRequest) -> ImageResult:
        if req.reference_images:
            return await self._edits(req)
        return await self._generations(req)

    async def _generations(self, req: ImageRequest) -> ImageResult:
        payload: Dict[str, Any] = {
            "model": settings.IMAGE_MODEL,
            "prompt": req.prompt,
            "n": req.n,
            "size": req.size,
            "quality": req.quality,
        }
        if req.resolution:
            payload["resolution"] = req.resolution
        if req.output_format:
            payload["output_format"] = req.output_format
        data = await self._request("POST", "/images/generations", timeout=settings.GENERATION_TIMEOUT, json=payload)
        return ImageResult(
            images_b64=_extract_b64(data),
            model=settings.IMAGE_MODEL,
            usage=data.get("usage", {}),
            raw=data,
        )

    async def _edits(self, req: ImageRequest) -> ImageResult:
        """Референсы через /images/edits — multipart с файлами напрямую.

        Порядок reference_images = @Image1, @Image2, ... (как у официалов).
        """
        form = aiohttp.FormData()
        form.add_field("model", settings.IMAGE_MODEL)
        form.add_field("prompt", req.prompt)
        form.add_field("n", str(req.n))
        if req.size:
            form.add_field("size", req.size)
        if req.quality:
            form.add_field("quality", req.quality)
        if req.resolution:
            form.add_field("resolution", req.resolution)
        if req.output_format:
            form.add_field("output_format", req.output_format)

        refs = req.reference_images[: settings.MAX_REFERENCE_IMAGES]
        for index, item in enumerate(refs):
            content, filename, content_type = item
            # image[] — массив файлов; AIGate/OpenAI-совместимый стиль.
            form.add_field("image[]", content, filename=filename, content_type=content_type)

        data = await self._request_multipart(
            "POST", "/images/edits", form, timeout=settings.GENERATION_TIMEOUT
        )
        return ImageResult(
            images_b64=_extract_b64(data),
            model=settings.IMAGE_MODEL,
            usage=data.get("usage", {}),
            raw=data,
        )
