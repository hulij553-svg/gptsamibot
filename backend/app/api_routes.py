"""FastAPI routes: image generation через gpt-image-2 (AIGate-шлюз).

Этапы 1–3 (MVP): text→image на общем ключе, батч ≤ max_batch тарифа, живой
просчёт цены, WebSocket-стадии, превью + выгрузка фото в чат бота.
Референсы/пул ключей/диспетчер — Этапы 4–7 (заглушки не плодим, добавим сверху).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl

import aiohttp
from fastapi import APIRouter, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.api_client import (
    AIGateClient,
    AIGateError,
    GeminiImageClient,
    GPTImageClient,
    ImageRequest,
    format_balance,
)
from app.config import settings
from app.database import db
from app.websocket import manager

router = APIRouter()
logger = logging.getLogger(__name__)

# In-memory флаг отмены джоб. При отмене — job_id добавляется сюда;
# чанки в run_generation проверяют его и останавливаются.
_cancelled_jobs: set = set()


def is_job_cancelled(job_id: str) -> bool:
    return job_id in _cancelled_jobs


# ============================================================
# Авторизация Mini App (переиспользуется из оригинала)
# ============================================================

def validate_telegram_init_data(init_data: str) -> Dict:
    if settings.ALLOW_DEV_AUTH and (not init_data or init_data == "dev"):
        return {"id": settings.DEV_TELEGRAM_ID, "first_name": "Demo", "username": "demo_user"}

    if not settings.BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")

    parsed = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=403, detail="Telegram auth hash is missing")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=403, detail="Invalid Telegram Mini App signature")

    auth_date_raw = parsed.get("auth_date")
    if auth_date_raw:
        try:
            if time.time() - int(auth_date_raw) > settings.TELEGRAM_AUTH_MAX_AGE_SECONDS:
                raise HTTPException(status_code=403, detail="Telegram auth data is expired")
        except ValueError:
            raise HTTPException(status_code=403, detail="Invalid Telegram auth date")

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=403, detail="Invalid Telegram user payload")

    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Telegram user id is missing")
    return user


async def get_or_create_user(init_data: str) -> Dict:
    tg_user = validate_telegram_init_data(init_data)
    telegram_id = int(tg_user["id"])
    user = await db.get_user(telegram_id)

    if not user:
        full_name = " ".join(part for part in [tg_user.get("first_name"), tg_user.get("last_name")] if part).strip()
        await db.create_user(telegram_id, tg_user.get("username"), full_name)
        user = await db.get_user(telegram_id)

    if not user:
        raise HTTPException(status_code=500, detail="Cannot create local user")

    # Access control: админы всегда; если ADMIN_IDS задан — остальные только по is_allowed.
    if settings.ADMIN_IDS and telegram_id not in settings.ADMIN_IDS:
        if not user.get("is_allowed"):
            raise HTTPException(status_code=403, detail="Access denied")
    return user


# ============================================================
# Helpers
# ============================================================

def form_text(form, key: str, default: str = "") -> str:
    value = form.get(key)
    return default if value is None else str(value)


def form_int(form, key: str, default: int) -> int:
    value = form_text(form, key, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}: {value}") from None


def form_optional_int(form, key: str) -> Optional[int]:
    value = form_text(form, key).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}: {value}") from None


def ensure_supported(value, allowed, label: str):
    if value not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported {label}: {value}")


def form_files(form, key: str) -> List[Any]:
    items = form.getlist(key)
    return [item for item in items if getattr(item, "filename", None)]


async def read_reference_files(files: List[Any]) -> List[tuple]:
    """Читает загруженные референсы в [(bytes, filename, content_type), ...].
    Порядок сохраняется = @Image1, @Image2, ...
    """
    out: List[tuple] = []
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    for file in files[: settings.MAX_REFERENCE_IMAGES]:
        if not file or not file.filename:
            continue
        content_type = file.content_type or "image/png"
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"Неверный тип файла референса: {file.filename}")
        content = await file.read()
        if len(content) > max_bytes:
            raise HTTPException(status_code=400, detail=f"Файл {file.filename} больше {settings.MAX_UPLOAD_MB} MB")
        out.append((content, file.filename, content_type))
        await file.seek(0)
    return out


def _ref_ext(content_type: str, filename: str) -> str:
    """Расширение файла референса по content_type или имени."""
    ct_map = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp"}
    ext = ct_map.get((content_type or "").lower())
    if ext:
        return ext
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1][:4].lower() or "png"
    return "png"


async def persist_references(
    user_db_id: int,
    job_id: str,
    refs: List[tuple],
) -> List[tuple]:
    """Сохраняет референсы в библиотеку (с sha256-дедупом) и привязывает к job'у.
    refs = [(bytes, filename, content_type), ...]. Возвращает тот же формат (bytes не меняются),
    чтобы run_generation получил их как раньше. Порядок = @Image1, @Image2… сохраняется."""
    if not refs:
        return refs
    refs_dir = Path(settings.MEDIA_DIR) / "refs" / str(user_db_id)
    refs_dir.mkdir(parents=True, exist_ok=True)

    links: List[tuple] = []  # [(asset_id, position), ...]
    for position, (content, filename, content_type) in enumerate(refs):
        sha = hashlib.sha256(content).hexdigest()
        ext = _ref_ext(content_type, filename)
        rel_path = f"refs/{user_db_id}/{sha}.{ext}"
        abs_path = Path(settings.MEDIA_DIR) / rel_path
        # Пишем только если файла ещё нет (дедуп на диске).
        if not abs_path.exists():
            await asyncio.to_thread(abs_path.write_bytes, content)
        asset_id = await db.save_reference_asset(
            user_db_id,
            filename=filename,
            content_type=content_type,
            path=rel_path,
            sha256=sha,
            size=len(content),
        )
        links.append((asset_id, position))

    await db.link_job_references(job_id, links)
    # Чистим старые сверх лимита.
    await db.cleanup_old_references(user_db_id, settings.MAX_LIBRARY_REFS)
    return refs


def public_url(path: str) -> str:
    """Всегда отдаёт ОТНОСИТЕЛЬНЫЙ URL (/media/...).
    Фронт на том же домене (Railway) сам подставит домен через absoluteUrl().
    Для sendPhoto файл читается с диска напрямую (см. _download)."""
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return path if path.startswith("/") else f"/{path}"


def images_dir(job_id: str) -> Path:
    directory = Path(settings.MEDIA_DIR) / "images" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def estimate(size: str, quality: str, n: int, model_key: str = "gpt") -> Dict[str, Any]:
    """Просчёт цены до запуска.
    gpt — фикс. $ за картинку × множитель площади × n (по факту кабинета).
    banana — оценка по токенам (по размеру) × ставку $/1k токенов."""
    usd = settings.estimate_price_usd(size, quality, n, model_key)
    tokens = settings.estimate_tokens(size, quality, n, model_key)
    return {
        "n": n,
        "total": usd,
        "total_rub": settings.usd_to_rub(usd),
        "currency": "USD",
        "tokens_estimated": tokens,
        "model": model_key,
        "engine": settings.model_engine(model_key),
    }


def is_transient(exc: AIGateError) -> bool:
    return exc.status_code in settings.transient_statuses


def is_dead_key(exc: AIGateError) -> bool:
    return exc.status_code in settings.dead_statuses


def is_rate_limited(exc: AIGateError) -> bool:
    return exc.status_code == 429


def _parse_keys_multiline(raw: str) -> List[str]:
    """Парсит многострочный ввод ключей: трим, дедуп, пустые — прочь."""
    seen = set()
    out: List[str] = []
    for line in (raw or "").splitlines():
        k = line.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


async def _validate_keys(keys: List[str]) -> tuple[List[tuple], List[dict]]:
    """Проверяет каждый ключ через get_balance().
    Возвращает (valid=[(key, balance_dict), ...], invalid=[{key_tail, reason}, ...])."""
    valid: List[tuple] = []
    invalid: List[dict] = []
    for k in keys:
        try:
            balance = await AIGateClient(k).get_balance()
            valid.append((k, balance))
        except AIGateError as exc:
            invalid.append({"key_tail": k[-4:], "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 — любая сетевая/прочая ошибка
            invalid.append({"key_tail": k[-4:], "reason": str(exc) or "network error"})
    return valid, invalid


class KeyPool:
    """Раздаёт ключи чанкам round-robin и управляет failover на время одного джоба.

    Потокобезопасность: asyncio.run в одном процессе; чанки конкурентны через gather,
    но назначения делаются в event-loop (нет抢占 между await внутри assign), поэтому
    простой счётчик + множества достаточны без lock.
    """

    def __init__(self, pool: List[Dict[str, Any]]):
        # pool — строки из БД (id, api_key, status, ...). На вход приходят только активные.
        self._items: List[tuple] = [(int(p["id"]), p["api_key"]) for p in pool]
        self._idx = 0
        self._suspended: set = set()   # key_id в cooldown (временно недоступны)
        self._dead: set = set()        # key_id мёртвые (до конца джоба)

    def __len__(self) -> int:
        return len(self._items)

    def _available(self) -> List[tuple]:
        return [(kid, k) for kid, k in self._items if kid not in self._suspended and kid not in self._dead]

    def assign(self) -> Optional[tuple]:
        """Выдаёт следующий доступный ключ (key_id, api_key). None, если доступных нет."""
        avail = self._available()
        if not avail:
            return None
        # round-robin по доступным
        n = len(self._items)
        for _ in range(n):
            kid, k = self._items[self._idx % n]
            self._idx = (self._idx + 1) % n
            if kid not in self._suspended and kid not in self._dead:
                return kid, k
        return None

    def fallback(self, excluded_key_id: Optional[int]) -> Optional[tuple]:
        """Ключ для ретрая, отличный от excluded. None, если доступных нет."""
        avail = self._available()
        if excluded_key_id is not None:
            avail = [(kid, k) for kid, k in avail if kid != excluded_key_id]
        return avail[0] if avail else None

    def suspend(self, key_id: int):
        self._suspended.add(key_id)

    def kill(self, key_id: int):
        self._dead.add(key_id)

    def has_available(self) -> bool:
        return bool(self._available())


# ============================================================
# Эндпойнты
# ============================================================

@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/api/config")
async def api_config():
    return {
        "model": settings.IMAGE_MODEL,
        "image_models": [
            {
                "key": key,
                "id": m["id"],
                "label": m["label"],
                "engine": m["engine"],
                "supports_quality": m["supports_quality"],
                "max_size_tier": m.get("max_size_tier"),
            }
            for key, m in settings.IMAGE_MODELS.items()
        ],
        "default_image_model": settings.DEFAULT_IMAGE_MODEL,
        "aspects": list(settings.ASPECT_RATIOS.keys()),
        "size_tiers": settings.IMAGE_SIZE_TIERS,
        "default_aspect": settings.DEFAULT_ASPECT,
        "default_size_tier": settings.DEFAULT_SIZE_TIER,
        "qualities": settings.IMAGE_QUALITIES,
        "formats": settings.IMAGE_FORMATS,
        "default_quality": settings.DEFAULT_QUALITY,
        "default_format": settings.DEFAULT_FORMAT,
        "max_n_per_call": settings.MAX_N_PER_CALL,
        "max_references": settings.MAX_REFERENCE_IMAGES,
        "max_prompt_length": settings.MAX_PROMPT_LENGTH,
        "max_image_long_edge": settings.MAX_IMAGE_LONG_EDGE,
        "provider_base": settings.PROVIDER_API_BASE,
        "usd_to_rub": settings.USD_TO_RUB,
        "price_per_image": settings.PRICE_PER_IMAGE_USD,
        "gemini_price_by_size": settings.GEMINI_PRICE_USD_BY_SIZE,
        "gemini_tokens_by_size": settings.GEMINI_TOKENS_BY_SIZE,
    }


@router.post("/api/estimate")
async def api_estimate(
    init_data: str = Form(...),
    model: str = Form(settings.DEFAULT_IMAGE_MODEL),
    aspect: str = Form(settings.DEFAULT_ASPECT),
    size_tier: str = Form(settings.DEFAULT_SIZE_TIER),
    quality: str = Form(settings.DEFAULT_QUALITY),
    n: int = Form(1),
):
    await get_or_create_user(init_data)
    if not settings.is_supported_model(model):
        raise HTTPException(status_code=400, detail=f"Unsupported model: {model}")
    if aspect not in settings.ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail=f"Unsupported aspect: {aspect}")
    ensure_supported(size_tier, settings.IMAGE_SIZE_TIERS, "size_tier")
    # Понижаем размер под лимит модели (банана не тянет 4K → 2K).
    size_tier = settings.effective_size_tier(model, size_tier)
    # quality валидируем только для моделей, которые его поддерживают (gpt)
    if settings.model_supports_quality(model):
        ensure_supported(quality, settings.IMAGE_QUALITIES, "quality")
    n = max(1, min(int(n), settings.MAX_N_PER_CALL))
    size = settings.aspect_to_size(aspect, size_tier)
    return estimate(size, quality, n, model)


@router.post("/api/balance")
async def api_balance(init_data: str = Form(...)):
    user = await get_or_create_user(init_data)
    pool_rows = await db.get_pool_keys(int(user["id"]))
    active = await db.get_active_pool_keys(int(user["id"]))
    pool_info = {
        "count": len(pool_rows),
        "active_count": len(active),
    }
    if not user.get("api_key") and not pool_rows:
        return {"has_key": False, "balance": None, "raw": None, "pool": pool_info}
    # Баланс — по primary-ключу (users.api_key), как раньше.
    primary_key = user.get("api_key") or (pool_rows[0]["api_key"] if pool_rows else "")
    try:
        balance = await AIGateClient(primary_key).get_balance()
        # balance от AIGate: {balance: <usd>, ...}
        usd = float(balance.get("balance", 0) or 0)
        return {
            "has_key": True,
            "balance": format_balance(balance),
            "balance_usd": usd,
            "balance_rub": round(usd * settings.USD_TO_RUB, 2),
            "raw": balance,
            "pool": pool_info,
        }
    except AIGateError as exc:
        return {"has_key": True, "balance": None, "raw": None, "error": str(exc), "pool": pool_info}


@router.get("/api/keys")
async def api_keys(init_data: str = ""):
    """Masked-список пула ключей пользователя (без plaintext)."""
    if not init_data:
        raise HTTPException(status_code=403, detail="init_data required")
    user = await get_or_create_user(init_data)
    rows = await db.get_pool_keys(int(user["id"]))
    now_ts = time.time()
    keys = []
    active_count = 0
    for r in rows:
        status = r.get("status") or "ok"
        on_cooldown = False
        if status == "cooldown":
            try:
                on_cooldown = float(r.get("cooldown_until") or 0) > now_ts
            except (TypeError, ValueError):
                on_cooldown = False
        if status == "ok" or (status == "cooldown" and not on_cooldown):
            active_count += 1
        keys.append({
            "id": r["id"],
            "last_four": r.get("last_four") or (r["api_key"][-4:] if r.get("api_key") else ""),
            "status": "cooldown" if (status == "cooldown" and on_cooldown) else status,
        })
    return {"count": len(rows), "active_count": active_count, "keys": keys}


@router.post("/api/setkey")
async def api_setkey(init_data: str = Form(...), api_keys: str = Form(...)):
    """Принимает несколько ключей (multiline). Валидирует каждый, синхронизирует пул.
    Невалидные возвращает в ответе. users.api_key = первый валидный (primary)."""
    user = await get_or_create_user(init_data)
    keys = _parse_keys_multiline(api_keys)
    if not keys:
        raise HTTPException(status_code=400, detail="Нет ни одного ключа")
    for k in keys:
        if len(k) < 16:
            raise HTTPException(status_code=400, detail=f"Ключ ...{k[-4:]} выглядит слишком коротким")

    valid, invalid = await _validate_keys(keys)
    if not valid:
        detail = "; ".join(f"...{i['key_tail']}: {i['reason']}" for i in invalid) or "ни один ключ не прошёл проверку"
        raise HTTPException(status_code=400, detail=f"Ни один ключ не прошёл проверку: {detail}")

    valid_keys = [k for k, _ in valid]
    pool_rows = await db.sync_pool_keys(int(user["id"]), valid_keys)
    # primary = первый валидный (для /api/balance и бэкофиса).
    await db.set_user_api_key(int(user["telegram_id"]), valid_keys[0])

    primary_balance = valid[0][1]
    return {
        "success": True,
        "count": len(pool_rows),
        "active_count": len([r for r in pool_rows if (r.get("status") or "ok") != "dead"]),
        "balance": format_balance(primary_balance),
        "raw": primary_balance,
        "invalid": invalid,
        "pool": [
            {"id": r["id"], "last_four": r.get("last_four"), "status": r.get("status") or "ok"}
            for r in pool_rows
        ],
    }


@router.post("/api/generate")
async def api_generate(request: Request):
    form = await request.form()
    init_data = form_text(form, "init_data")
    prompt = form_text(form, "prompt").strip()
    model = form_text(form, "model", settings.DEFAULT_IMAGE_MODEL)
    aspect = form_text(form, "aspect", settings.DEFAULT_ASPECT)
    size_tier = form_text(form, "size_tier", settings.DEFAULT_SIZE_TIER)
    quality = form_text(form, "quality", settings.DEFAULT_QUALITY)
    output_format = form_text(form, "output_format", settings.DEFAULT_FORMAT)
    n = form_int(form, "n", 1)
    reference_files = form_files(form, "references")

    user = await get_or_create_user(init_data)

    if not prompt:
        raise HTTPException(status_code=400, detail="Промпт не может быть пустым")
    if len(prompt) > settings.MAX_PROMPT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Промпт длиннее {settings.MAX_PROMPT_LENGTH} символов")
    if not settings.is_supported_model(model):
        raise HTTPException(status_code=400, detail=f"Unsupported model: {model}")
    if aspect not in settings.ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail=f"Unsupported aspect: {aspect}")
    ensure_supported(size_tier, settings.IMAGE_SIZE_TIERS, "size_tier")
    # Понижаем размер под лимит модели (банана не тянет 4K → 2K).
    size_tier = settings.effective_size_tier(model, size_tier)
    # quality валидируем только для моделей, которые его поддерживают (gpt)
    if settings.model_supports_quality(model):
        ensure_supported(quality, settings.IMAGE_QUALITIES, "quality")
    ensure_supported(output_format, settings.IMAGE_FORMATS, "output_format")

    size = settings.aspect_to_size(aspect, size_tier)

    # Пул ключей пользователя (многопоточность + failover). Берём активные.
    pool = await db.get_active_pool_keys(int(user["id"]))
    if not pool:
        raise HTTPException(status_code=400, detail="Сначала подключите API-ключ")

    if n < 1 or n > settings.MAX_N_PER_CALL:
        raise HTTPException(status_code=400, detail=f"За один запуск до {settings.MAX_N_PER_CALL} картинок")

    # Референсы (порядок = @Image1, @Image2, ...). Читаем в память.
    references = await read_reference_files(reference_files)

    est = estimate(size, quality, n, model)
    job_id = await db.create_job(
        user_db_id=int(user["id"]),
        prompt=prompt,
        size=size,
        quality=quality,
        n=n,
        seed=None,
        estimate_total=est["total"],
        used_shared_key=False,
        references_count=len(references),
        model=model,
        aspect=aspect,
        size_tier=size_tier,
        output_format=output_format,
    )

    # Сохраняем референсы в библиотеку + привязываем к job'у (для Reuse из истории).
    references = await persist_references(int(user["id"]), job_id, references)

    telegram_id = int(user["telegram_id"])
    asyncio.create_task(
        run_generation(
            job_id=job_id,
            telegram_id=telegram_id,
            pool=pool,
            prompt=prompt,
            size=size,
            quality=quality,
            n=n,
            output_format=output_format,
            references=references,
            model_key=model,
            aspect_ratio=aspect,
            size_tier=size_tier,
        )
    )

    return {
        "job_id": job_id, "status": "queued", "estimate": est,
        "model": model, "aspect": aspect, "size": size, "size_tier": size_tier,
        "references_count": len(references),
    }


@router.post("/api/jobs/{job_id}")
async def api_job(job_id: str, init_data: str = Form(...)):
    user = await get_or_create_user(init_data)
    job = await db.get_job(job_id)
    if not job or int(job["user_id"]) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Задача не найдена")
    images = await db.get_job_images(job_id)
    return {
        "job": job,
        "images": [
            {"id": img["id"], "url": public_url(f"/media/{img['local_path']}"), "size_bytes": img.get("size_bytes")}
            for img in images
        ],
    }


@router.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str, init_data: str = Form(...)):
    user = await get_or_create_user(init_data)
    job = await db.get_job(job_id)
    if not job or int(job["user_id"]) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if job["status"] in {"done", "failed", "partial", "cancelled"}:
        return {"job_id": job_id, "status": job["status"], "cancelled": False}
    _cancelled_jobs.add(job_id)
    return {"job_id": job_id, "status": "cancelling", "cancelled": True}


@router.post("/api/history")
async def api_history(init_data: str = Form(...), limit: int = Form(60)):
    user = await get_or_create_user(init_data)
    limit = max(1, min(int(limit), 200))
    jobs = await db.get_user_jobs(int(user["telegram_id"]), limit=limit)
    images = await db.get_user_images(int(user["telegram_id"]), limit=limit)

    # Подтягиваем референсы для каждого job'а (для Reuse из истории).
    # Собираем одним проходом по уникальным job_id, чтобы не плодить N запросов.
    job_ids = {img["job_id"] for img in images if img.get("job_id")}
    refs_by_job: Dict[str, List[Dict[str, Any]]] = {}
    for jid in job_ids:
        refs_by_job[jid] = await db.get_job_references(jid)

    return {
        "jobs": jobs,
        "images": [
            {
                "id": img["id"],
                "job_id": img["job_id"],
                "url": public_url(f"/media/{img['local_path']}"),
                "size_bytes": img.get("size_bytes"),
                "created_at": img.get("created_at"),
                "prompt": img.get("prompt"),
                "size": img.get("size"),
                "quality": img.get("quality"),
                "cost_real": img.get("cost_real"),
                "model": img.get("model") or "gpt",
                "aspect": img.get("aspect"),
                "size_tier": img.get("size_tier"),
                "output_format": img.get("output_format"),
                "n": img.get("n_requested"),
                "references": [
                    {
                        "position": r.get("position", i),
                        "asset_id": r["id"],
                        "url": public_url(f"/media/{r['path']}"),
                        "filename": r.get("filename"),
                    }
                    for i, r in enumerate(refs_by_job.get(img["job_id"], []))
                ],
            }
            for img in images
        ],
    }


@router.get("/api/refs")
async def api_refs(init_data: str = ""):
    """Библиотека референсов пользователя (список, без бинарников)."""
    if not init_data:
        raise HTTPException(status_code=403, detail="init_data required")
    user = await get_or_create_user(init_data)
    rows = await db.get_user_references(int(user["id"]))
    return {
        "count": len(rows),
        "refs": [
            {
                "id": r["id"],
                "url": public_url(f"/media/{r['path']}"),
                "filename": r.get("filename"),
                "created_at": r.get("created_at"),
            }
            for r in rows
        ],
    }


@router.get("/api/refs/{asset_id}/file")
async def api_ref_file(asset_id: int, init_data: str = ""):
    """Отдаёт сам файл референса (для подгрузки в <File> через fetch→blob)."""
    if not init_data:
        raise HTTPException(status_code=403, detail="init_data required")
    user = await get_or_create_user(init_data)
    asset = await db.get_reference_asset(int(user["id"]), int(asset_id))
    if not asset:
        raise HTTPException(status_code=404, detail="Референс не найден")
    abs_path = Path(settings.MEDIA_DIR) / asset["path"]
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="Файл референса потерян")
    return FileResponse(str(abs_path), media_type=asset.get("content_type") or "image/png")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, init_data: str = ""):
    try:
        user = validate_telegram_init_data(init_data)
        telegram_id = int(user["id"])
    except HTTPException:
        await websocket.close(code=1008)
        return

    await manager.connect(telegram_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(telegram_id, websocket)


# ============================================================
# Фоновая генерация (чанками, как в Runway)
# ============================================================

def _split_chunks(n: int, chunk_size: int) -> List[tuple]:
    """Возвращает [(start_index, chunk_size), ...]. start_index — глобальный 0-based."""
    chunks = []
    idx = 0
    remaining = n
    while remaining > 0:
        sz = min(chunk_size, remaining)
        chunks.append((idx, sz))
        idx += sz
        remaining -= sz
    return chunks


async def run_generation(
    *,
    job_id: str,
    telegram_id: int,
    pool: List[Dict[str, Any]],
    prompt: str,
    size: str,
    quality: str,
    n: int,
    output_format: str = "png",
    references: Optional[List[tuple]] = None,
    model_key: str = "gpt",
    aspect_ratio: Optional[str] = None,
    size_tier: str = "standard",
):
    # Клиент и размер чанка зависят от движка модели. api_key подставляется per-чанк из пула.
    engine = settings.model_engine(model_key)
    if engine == "gemini":
        chunk_size = 1  # Gemini отдаёт 1 картинку за вызов; N картинок = N вызовов
        image_size = settings.gemini_image_size(size_tier)
        make_client = lambda api_key: GeminiImageClient(api_key)
    else:
        chunk_size = settings.CHUNK_SIZE
        image_size = None
        make_client = lambda api_key: GPTImageClient(api_key)

    ext = output_format if output_format in {"png", "jpeg", "webp"} else "png"
    references = references or []
    keypool = KeyPool(pool)

    # Баланс ДО — для расчёта реальной цены (дельта) в конце. По primary (первому) ключу.
    balance_before: Optional[float] = None
    try:
        primary_key = pool[0]["api_key"]
        bal = await AIGateClient(primary_key).get_balance()
        balance_before = float(bal.get("balance", 0) or 0)
    except Exception:
        pass

    await db.update_job_status(job_id, "generating")
    await manager.send_progress(
        telegram_id, job_id, "generating", "Отправляем запросы…",
        progress=5, done_count=0, total_count=n,
    )

    chunks = _split_chunks(n, chunk_size)
    sem = asyncio.Semaphore(settings.CHUNK_CONCURRENCY)

    # shared mutable state
    state = {"done": 0, "tokens": 0, "failed": 0}
    all_previews: List[str] = []
    chunk_errors: List[str] = []

    async def run_chunk(start_index: int, this_chunk_size: int) -> str:
        async with sem:
            assigned = keypool.assign()
            if assigned is None:
                err_msg = "Все ключи недоступны (rate-limit/баланс). Попробуйте позже или добавьте ключи."
                chunk_errors.append(err_msg)
                state["failed"] += this_chunk_size
                return "failed"
            key_id, api_key = assigned

            for attempt in range(settings.MAX_RETRIES):
                if is_job_cancelled(job_id):
                    return "cancelled"
                try:
                    client = make_client(api_key)
                    logger.info(
                        "Generating chunk: job=%s model=%s chunk_size=%s key=%s refs=%s ref_sizes=%s size=%s quality=%s",
                        job_id, model_key, this_chunk_size, key_id, len(references),
                        [len(r[0]) for r in references] if references else [],
                        size, quality,
                    )
                    result = await client.generate(ImageRequest(
                        prompt=prompt, size=size, quality=quality, n=this_chunk_size,
                        output_format=output_format,
                        reference_images=references,
                        model_key=model_key,
                        aspect_ratio=aspect_ratio,
                        image_size=image_size,
                    ))
                    images_b64 = [b64 for b64 in result.images_b64 if b64]
                    if not images_b64:
                        logger.warning(
                            "AIGate returned empty images: job=%s chunk_size=%s data_keys=%s data_len=%s raw=%s",
                            job_id, this_chunk_size,
                            list(result.raw.keys()) if isinstance(result.raw, dict) else type(result.raw).__name__,
                            len(result.raw.get("data", [])) if isinstance(result.raw, dict) else 0,
                            str(result.raw)[:400],
                        )
                        raise AIGateError("AIGate вернул пустой ответ (нет картинок)", payload=result.raw)

                    job_dir = images_dir(job_id)
                    chunk_previews: List[str] = []
                    for offset, b64 in enumerate(images_b64):
                        global_idx = start_index + offset + 1
                        content = base64.b64decode(b64)
                        fname = f"{global_idx}.{ext}"
                        path = job_dir / fname
                        await asyncio.to_thread(path.write_bytes, content)
                        relative = path.relative_to(Path(settings.MEDIA_DIR)).as_posix()
                        await db.add_image(job_id, relative, len(content))
                        chunk_previews.append(public_url(f"/media/{relative}"))

                    tokens = int((result.usage or {}).get("total_tokens", 0))
                    cost_usd_chunk = float((result.usage or {}).get("cost_usd", 0) or 0)
                    await db.add_job_usage(job_id, add_n=len(images_b64), add_tokens=tokens)
                    state["done"] += len(images_b64)
                    state["tokens"] += tokens
                    state["cost_usd"] = state.get("cost_usd", 0.0) + cost_usd_chunk
                    all_previews.extend(chunk_previews)

                    # Ключ отработал чисто — сбросим cooldown, если был.
                    await db.clear_key_status(key_id)

                    pct = int(state["done"] / n * 100) if n else 100
                    await manager.send_progress(
                        telegram_id, job_id, "generating",
                        f"Готово {state['done']}/{n}…",
                        progress=pct, done_count=state["done"], total_count=n,
                        previews=chunk_previews,
                    )
                    return "ok"
                except AIGateError as exc:
                    if is_job_cancelled(job_id):
                        return "cancelled"
                    err_msg = str(exc) or f"AIGate error {exc.status_code}"

                    # === Failover по пулу ===
                    if is_rate_limited(exc):
                        # 429 — ключ в cooldown, пробуем другой.
                        await db.mark_key_cooldown(key_id, 30)
                        keypool.suspend(key_id)
                        fallback = keypool.fallback(key_id)
                        if fallback is not None and attempt < settings.MAX_RETRIES - 1:
                            key_id, api_key = fallback
                            logger.warning("Key %s rate-limited, switching to key %s (job=%s)", key_id, api_key[-4:], job_id)
                            await asyncio.sleep(min(settings.RETRY_BACKOFF_MAX, settings.RETRY_BACKOFF_BASE * (2 ** attempt)))
                            continue
                    elif is_dead_key(exc):
                        # 401/403 — ключ мёртв, исключаем до след. синхронизации.
                        await db.mark_key_dead(key_id)
                        keypool.kill(key_id)
                        fallback = keypool.fallback(key_id)
                        if fallback is not None and attempt < settings.MAX_RETRIES - 1:
                            key_id, api_key = fallback
                            logger.warning("Key %s dead, switching to key %s (job=%s)", key_id, api_key[-4:], job_id)
                            continue
                    elif is_transient(exc) and attempt < settings.MAX_RETRIES - 1:
                        # Прочие 5xx/502/503 — ретрай на том же ключе с backoff.
                        backoff = min(settings.RETRY_BACKOFF_MAX, settings.RETRY_BACKOFF_BASE * (2 ** attempt))
                        await manager.send_progress(
                            telegram_id, job_id, "generating",
                            f"Временная ошибка, повтор {attempt + 2}/{settings.MAX_RETRIES}…",
                            progress=int(state["done"] / n * 100) if n else 0,
                            done_count=state["done"], total_count=n,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    chunk_errors.append(err_msg)
                    state["failed"] += this_chunk_size
                    logger.warning("Chunk failed (AIGateError): job=%s status=%s msg=%s", job_id, exc.status_code, err_msg)
                    await manager.send_progress(
                        telegram_id, job_id, "generating",
                        f"Чанк ({this_chunk_size} шт) не вышел: {err_msg}",
                        done_count=state["done"], total_count=n,
                    )
                    return "failed"
                except asyncio.TimeoutError:
                    err_msg = f"Таймаут генерации (>{settings.GENERATION_TIMEOUT}с). Попробуй меньший размер или quality."
                    logger.warning("Chunk timeout: job=%s timeout=%ss", job_id, settings.GENERATION_TIMEOUT)
                    chunk_errors.append(err_msg)
                    state["failed"] += this_chunk_size
                    await manager.send_progress(
                        telegram_id, job_id, "generating",
                        f"Чанк ({this_chunk_size} шт) не вышел: {err_msg}",
                        done_count=state["done"], total_count=n,
                    )
                    return "failed"
                except Exception as exc:
                    err_msg = str(exc) or f"{type(exc).__name__} (без сообщения)"
                    logger.exception("Chunk failed unexpectedly for %s: %s", job_id, err_msg)
                    chunk_errors.append(err_msg)
                    state["failed"] += this_chunk_size
                    return "failed"
            return "failed"

    results = await asyncio.gather(*[run_chunk(s, c) for s, c in chunks])

    cancelled = is_job_cancelled(job_id)
    _cancelled_jobs.discard(job_id)
    done_count = state["done"]
    failed_count = state["failed"]
    total_tokens = state["tokens"]

    # Реальная цена: приоритет — sum(cost_usd) из ответов AIGate (точнее),
    # fallback — дельта баланса.
    cost_real_usd: Optional[float] = None
    if state.get("cost_usd"):
        cost_real_usd = round(state["cost_usd"], 4)
    elif balance_before is not None:
        try:
            bal_after = await AIGateClient(api_key).get_balance()
            balance_after = float(bal_after.get("balance", 0) or 0)
            cost_real_usd = round(balance_before - balance_after, 4)
            if cost_real_usd < 0:
                cost_real_usd = 0.0
        except Exception:
            pass
    cost_real_rub = settings.usd_to_rub(cost_real_usd) if cost_real_usd is not None else None

    def cost_str() -> str:
        if cost_real_usd is not None:
            return f"Потрачено {cost_real_usd:.4f}$ ≈ {cost_real_rub:.2f} ₽"
        return f"Потрачено {total_tokens} токенов"

    failed_str = f" Упало: {failed_count} шт." if failed_count > 0 else ""

    if cancelled and done_count < n:
        status = "cancelled"
        msg = f"Отменено: готово {done_count}/{n}.{failed_str}"
    elif cancelled and done_count == n:
        status = "done"
        msg = f"Готово: {done_count}/{n}. {cost_str()}"
    elif done_count == 0:
        status = "failed"
        msg = chunk_errors[0] if chunk_errors else "Генерация не удалась"
    elif done_count < n:
        status = "partial"
        msg = f"Частично: готово {done_count}/{n}.{failed_str} {cost_str()}"
    else:
        status = "done"
        msg = f"Готово: {done_count}/{n}. {cost_str()}"

    await db.update_job_status(
        job_id, status,
        n_failed=failed_count,
        usage_total_tokens=total_tokens,
        cost_real=cost_real_usd,
        error=("; ".join(chunk_errors) if chunk_errors and status != "done" else None),
    )
    await manager.send_progress(
        telegram_id, job_id, status, msg,
        progress=100, done_count=done_count, total_count=n, previews=all_previews,
        tokens=total_tokens,
        cost_rub=cost_real_rub if cost_real_rub is not None else settings.tokens_to_rub(total_tokens),
    )

    if status in {"done", "partial"}:
        await notify_job_done(telegram_id, job_id, prompt, size, quality, done_count, n, all_previews, total_tokens, output_format)
    elif status == "failed":
        await notify_job_failed(telegram_id, job_id, prompt, msg)


# ============================================================
# Уведомления в чат бота (фото + текст через Telegram Bot API)
# ============================================================

async def _telegram_post(endpoint: str, data: aiohttp.FormData, timeout_seconds: int = 60) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{settings.BOT_TOKEN}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=data) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400 or not payload.get("ok"):
                logger.warning("Telegram %s failed: %s", endpoint, payload)
            return payload


async def _download(url: str) -> bytes:
    # Если URL относительный (/media/...) — читаем файл с диска (быстрее и надёжнее).
    if url.startswith("/media/"):
        path = Path(settings.MEDIA_DIR) / url[len("/media/"):]
        return await asyncio.to_thread(path.read_bytes)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


def _ext_for_format(fmt: str) -> str:
    return fmt if fmt in {"png", "jpeg", "webp"} else "png"


def _ctype_for_format(fmt: str) -> str:
    return {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "image/png")


async def notify_job_done(telegram_id, job_id, prompt, size, quality, n_done, n_total, previews, usage_tokens, output_format="png"):
    if not settings.BOT_TOKEN or not previews:
        return

    ext = _ext_for_format(output_format)
    ctype = _ctype_for_format(output_format)
    chat_id = str(telegram_id)

    caption = f"🖼 Готово: {n_done}/{n_total}\n{size} · {quality}\nТокенов: {usage_tokens}\nПромпт: {prompt[:200]}"
    single_caption = f"🖼 Готово: {n_done}/{n_total}\n{size} · {quality}\nПромпт: {prompt[:200]}"

    async def _post_photo(photo_bytes, caption_text):
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        data.add_field("photo", photo_bytes, filename=f"image.{ext}", content_type=ctype)
        if caption_text:
            data.add_field("caption", caption_text[:1024])
        payload = await _telegram_post("sendPhoto", data, timeout_seconds=180)
        if not payload.get("ok"):
            raise Exception(f"Telegram sendPhoto failed: {payload}")

    async def _post_group(photo_bytes_list):
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        media = []
        for i, b in enumerate(photo_bytes_list):
            fname = f"image_{i}.{ext}"
            data.add_field(fname, b, filename=fname, content_type=ctype)
            media.append({"type": "photo", "media": f"attach://{fname}"})
        media[0]["caption"] = caption[:1024]
        data.add_field("media", json.dumps(media))
        payload = await _telegram_post("sendMediaGroup", data, timeout_seconds=180)
        if not payload.get("ok"):
            raise Exception(f"Telegram sendMediaGroup failed: {payload}")

    async def _post_text(text):
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        data.add_field("text", text)
        payload = await _telegram_post("sendMessage", data, timeout_seconds=60)
        if not payload.get("ok"):
            raise Exception(f"Telegram sendMessage failed: {payload}")

    # Preload images from disk (max 10 for Telegram media group limit)
    images = []
    for p in previews[:10]:
        try:
            images.append(await _download(p))
        except Exception as exc:
            logger.warning("Failed to download preview %s for job %s: %s", p, job_id, exc)
            images.append(None)

    last_error = None
    try:
        if len(previews) == 1:
            photo_bytes = images[0]
            for attempt in range(2):
                if photo_bytes is None:
                    break
                try:
                    await _post_photo(photo_bytes, single_caption)
                    return
                except Exception as exc:
                    last_error = exc
                    logger.warning("sendPhoto attempt %s failed for job %s: %s", attempt + 1, job_id, exc)
                    if attempt == 0:
                        await asyncio.sleep(1.5)

        elif all(b is not None for b in images):
            for attempt in range(2):
                try:
                    await _post_group(images)
                    return
                except Exception as exc:
                    last_error = exc
                    logger.warning("sendMediaGroup attempt %s failed for job %s: %s", attempt + 1, job_id, exc)
                    if attempt == 0:
                        await asyncio.sleep(1.5)

        # Fallback: send each image individually
        sent_any = False
        for i, photo_bytes in enumerate(images):
            if photo_bytes is None:
                continue
            try:
                await _post_photo(photo_bytes, single_caption if not sent_any else "")
                sent_any = True
            except Exception as exc:
                last_error = exc
                logger.warning("Individual sendPhoto failed for job %s preview %s: %s", job_id, i, exc)

        if sent_any:
            return

        # Final fallback: text message with a link to the Mini App
        logger.warning("All media send attempts failed for job %s, sending text fallback", job_id)
        await _post_text(
            f"🖼 Готово: {n_done}/{n_total}\n{size} · {quality}\nТокенов: {usage_tokens}\nПромпт: {prompt[:200]}\n\n"
            f"Результаты доступны в Mini App (задача #{job_id[:8]})."
        )
    except Exception as exc:
        logger.exception("notify_job_done failed for job %s", job_id)


async def notify_job_failed(telegram_id, job_id, prompt, error):
    if not settings.BOT_TOKEN:
        return
    data = aiohttp.FormData()
    data.add_field("chat_id", str(telegram_id))
    data.add_field("text", f"❌ Генерация не удалась\nЗадача #{job_id[:8]}\nОшибка: {error}\nПромпт: {prompt[:200]}")
    await _telegram_post("sendMessage", data)
