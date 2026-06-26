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

from app.api_client import (
    AIGateClient,
    AIGateError,
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


def estimate(size: str, quality: str, n: int) -> Dict[str, Any]:
    """Просчёт цены до запуска: фикс. $ за картинку × множитель площади × n.
    По факту кабинета AIGate (не по токенам — токены только справочно)."""
    usd = settings.estimate_price_usd(size, quality, n)
    tokens = settings.estimate_tokens(size, quality, n)
    return {
        "n": n,
        "total": usd,
        "total_rub": settings.usd_to_rub(usd),
        "currency": "USD",
        "tokens_estimated": tokens,
    }


def is_transient(exc: AIGateError) -> bool:
    return exc.status_code in settings.transient_statuses


def is_dead_key(exc: AIGateError) -> bool:
    return exc.status_code in settings.dead_statuses


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
    }


@router.post("/api/estimate")
async def api_estimate(
    init_data: str = Form(...),
    aspect: str = Form(settings.DEFAULT_ASPECT),
    size_tier: str = Form(settings.DEFAULT_SIZE_TIER),
    quality: str = Form(settings.DEFAULT_QUALITY),
    n: int = Form(1),
):
    await get_or_create_user(init_data)
    if aspect not in settings.ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail=f"Unsupported aspect: {aspect}")
    ensure_supported(size_tier, settings.IMAGE_SIZE_TIERS, "size_tier")
    ensure_supported(quality, settings.IMAGE_QUALITIES, "quality")
    n = max(1, min(int(n), settings.MAX_N_PER_CALL))
    size = settings.aspect_to_size(aspect, size_tier)
    return estimate(size, quality, n)


@router.post("/api/balance")
async def api_balance(init_data: str = Form(...)):
    user = await get_or_create_user(init_data)
    if not user.get("api_key"):
        return {"has_key": False, "balance": None, "raw": None}
    try:
        balance = await AIGateClient(user["api_key"]).get_balance()
        # balance от AIGate: {balance: <usd>, ...}
        usd = float(balance.get("balance", 0) or 0)
        return {
            "has_key": True,
            "balance": format_balance(balance),
            "balance_usd": usd,
            "balance_rub": round(usd * settings.USD_TO_RUB, 2),
            "raw": balance,
        }
    except AIGateError as exc:
        return {"has_key": True, "balance": None, "raw": None, "error": str(exc)}


@router.post("/api/setkey")
async def api_setkey(init_data: str = Form(...), api_key: str = Form(...)):
    user = await get_or_create_user(init_data)
    api_key = api_key.strip()
    if len(api_key) < 16:
        raise HTTPException(status_code=400, detail="Ключ выглядит слишком коротким")

    try:
        balance = await AIGateClient(api_key).get_balance()
        await db.set_user_api_key(int(user["telegram_id"]), api_key)
        return {"success": True, "balance": format_balance(balance), "raw": balance}
    except AIGateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/generate")
async def api_generate(request: Request):
    form = await request.form()
    init_data = form_text(form, "init_data")
    prompt = form_text(form, "prompt").strip()
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
    if aspect not in settings.ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail=f"Unsupported aspect: {aspect}")
    ensure_supported(size_tier, settings.IMAGE_SIZE_TIERS, "size_tier")
    ensure_supported(quality, settings.IMAGE_QUALITIES, "quality")
    ensure_supported(output_format, settings.IMAGE_FORMATS, "output_format")

    size = settings.aspect_to_size(aspect, size_tier)

    # Ключ пользователя (как в оригинале). Общий ключ/пул/тарифы — позже.
    api_key = user.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Сначала подключите API-ключ")

    if n < 1 or n > settings.MAX_N_PER_CALL:
        raise HTTPException(status_code=400, detail=f"За один запуск до {settings.MAX_N_PER_CALL} картинок")

    # Референсы (порядок = @Image1, @Image2, ...). Читаем в память.
    references = await read_reference_files(reference_files)

    est = estimate(size, quality, n)
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
    )

    telegram_id = int(user["telegram_id"])
    asyncio.create_task(
        run_generation(
            job_id=job_id,
            telegram_id=telegram_id,
            api_key=api_key,
            prompt=prompt,
            size=size,
            quality=quality,
            output_format=output_format,
            n=n,
            references=references,
        )
    )

    return {
        "job_id": job_id, "status": "queued", "estimate": est,
        "aspect": aspect, "size": size, "size_tier": size_tier,
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
            }
            for img in images
        ],
    }


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
    api_key: str,
    prompt: str,
    size: str,
    quality: str,
    n: int,
    output_format: str = "png",
    references: Optional[List[tuple]] = None,
):
    client = GPTImageClient(api_key)
    ext = output_format if output_format in {"png", "jpeg", "webp"} else "png"
    references = references or []

    # Баланс ДО — для расчёта реальной цены (дельта) в конце.
    balance_before: Optional[float] = None
    try:
        bal = await AIGateClient(api_key).get_balance()
        balance_before = float(bal.get("balance", 0) or 0)
    except Exception:
        pass

    await db.update_job_status(job_id, "generating")
    await manager.send_progress(
        telegram_id, job_id, "generating", "Отправляем запросы…",
        progress=5, done_count=0, total_count=n,
    )

    chunks = _split_chunks(n, settings.CHUNK_SIZE)
    sem = asyncio.Semaphore(settings.CHUNK_CONCURRENCY)

    # shared mutable state
    state = {"done": 0, "tokens": 0, "failed": 0}
    all_previews: List[str] = []
    chunk_errors: List[str] = []

    async def run_chunk(start_index: int, chunk_size: int) -> str:
        async with sem:
            for attempt in range(settings.MAX_RETRIES):
                if is_job_cancelled(job_id):
                    return "cancelled"
                try:
                    logger.info(
                        "Generating chunk: job=%s chunk_size=%s refs=%s ref_sizes=%s size=%s quality=%s",
                        job_id, chunk_size, len(references),
                        [len(r[0]) for r in references] if references else [],
                        size, quality,
                    )
                    result = await client.generate(ImageRequest(
                        prompt=prompt, size=size, quality=quality, n=chunk_size,
                        output_format=output_format,
                        reference_images=references,
                    ))
                    images_b64 = [b64 for b64 in result.images_b64 if b64]
                    if not images_b64:
                        logger.warning(
                            "AIGate returned empty images: job=%s chunk_size=%s data_keys=%s data_len=%s raw=%s",
                            job_id, chunk_size,
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
                    if is_transient(exc) and attempt < settings.MAX_RETRIES - 1:
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
                    state["failed"] += chunk_size
                    logger.warning("Chunk failed (AIGateError): job=%s status=%s msg=%s", job_id, exc.status_code, err_msg)
                    await manager.send_progress(
                        telegram_id, job_id, "generating",
                        f"Чанк ({chunk_size} шт) не вышел: {err_msg}",
                        done_count=state["done"], total_count=n,
                    )
                    return "failed"
                except asyncio.TimeoutError:
                    err_msg = f"Таймаут генерации (>{settings.GENERATION_TIMEOUT}с). Попробуй меньший размер или quality."
                    logger.warning("Chunk timeout: job=%s timeout=%ss", job_id, settings.GENERATION_TIMEOUT)
                    chunk_errors.append(err_msg)
                    state["failed"] += chunk_size
                    await manager.send_progress(
                        telegram_id, job_id, "generating",
                        f"Чанк ({chunk_size} шт) не вышел: {err_msg}",
                        done_count=state["done"], total_count=n,
                    )
                    return "failed"
                except Exception as exc:
                    err_msg = str(exc) or f"{type(exc).__name__} (без сообщения)"
                    logger.exception("Chunk failed unexpectedly for %s: %s", job_id, err_msg)
                    chunk_errors.append(err_msg)
                    state["failed"] += chunk_size
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
