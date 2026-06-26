"""Application configuration."""

import os
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BOT_TOKEN: str = ""
    ADMIN_IDS: Any = Field(default_factory=list)

    DATABASE_PATH: str = "data/bot.db"
    MEDIA_DIR: str = "media"
    MAX_PROMPT_LENGTH: int = 3500

    WEBAPP_HOST: str = "0.0.0.0"
    WEBAPP_PORT: int = Field(default_factory=lambda: int(os.getenv("PORT", "8080")))
    WEBAPP_URL: str = "http://localhost:5173"
    # База для media-ссылок (картинки). На проде = тот же домен, что WEBAPP_URL.
    # В локальном деве фронт и бэк на разных портах, поэтому media-URL должны
    # указывать на бэкенд, иначе браузер ищет картинки на порту фронта.
    MEDIA_BASE_URL: str = ""
    CORS_ORIGINS: Any = Field(default_factory=list)

    # === Провайдер (AIGate-шлюз, OpenAI-совместимый) ===
    PROVIDER_API_BASE: str = "https://api.aigate.shop/v1"
    IMAGE_MODEL: str = "openai/gpt-image-2"   # legacy: модель gpt-движка (для совместимости)

    # === Реестр моделей изображения ===
    # Ключ = внутренний id модели в боте. engine определяет клиента:
    #   openai → /images/generations|/images/edits (gpt-image-2)
    #   gemini → /chat/completions (gemini-3.x-image)
    IMAGE_MODELS: Dict[str, Dict[str, Any]] = {
        "gpt": {
            "id": "openai/gpt-image-2",
            "label": "GPT IMAGE 2",
            "engine": "openai",
            "supports_quality": True,   # есть low/medium/high
        },
        "banana": {
            "id": "google/gemini-3.1-flash-image-preview",
            "label": "BANANA 2",
            "engine": "gemini",
            "supports_quality": False,  # качество не выбирается; только размер (1K/2K/4K)
        },
    }
    DEFAULT_IMAGE_MODEL: str = "gpt"

    # size_tier (standard|2k|max) → image_size для Gemini (image_config.image_size).
    GEMINI_SIZE_MAP: Dict[str, str] = {
        "standard": "1K",
        "2k": "2K",
        "max": "4K",
    }
    # Реальные токены за картинку по размеру (замерено на AIGate, 2026-06).
    # Картинка = фикс. image_tokens, не зависит от промпта.
    GEMINI_TOKENS_BY_SIZE: Dict[str, int] = {
        "1K": 1120,
        "2K": 1680,
        "4K": 2520,
    }
    # Реальная цена за картинку по размеру (из cost_usd ответа AIGate, $).
    # Промпт почти не влияет (41→113 tok = +$0.00001), ценой диктует размер.
    GEMINI_PRICE_USD_BY_SIZE: Dict[str, float] = {
        "1K": 0.00902,
        "2K": 0.01353,
        "4K": 0.02029,
    }

    IMAGE_QUALITIES: Any = Field(default_factory=lambda: ["low", "medium", "high"])
    IMAGE_FORMATS: Any = Field(default_factory=lambda: ["png", "jpeg", "webp"])
    IMAGE_ASPECTS: Any = Field(default_factory=lambda: ["1:1", "9:16", "16:9", "4:3", "3:4", "3:2", "2:3", "4:5"])
    IMAGE_SIZE_TIERS: Any = Field(default_factory=lambda: ["standard", "2k", "max"])
    DEFAULT_QUALITY: str = "medium"
    DEFAULT_FORMAT: str = "png"
    DEFAULT_ASPECT: str = "1:1"
    DEFAULT_SIZE_TIER: str = "standard"        # standard | 2k | max — уровень размера
    MAX_PROMPT_LENGTH: int = 16000             # gpt-image-2 принимает длинные промпты (было 3500)
    MAX_UPLOAD_MB: int = 16
    MAX_N_PER_CALL: int = 16                   # лимит n за один вызов
    MAX_REFERENCE_IMAGES: int = 16             # дока: "one or more" без верха — проверить
    MAX_IMAGE_LONG_EDGE: int = 3840            # лимит AIGate: длинная сторона ≤ 3840px
    MIN_IMAGE_SHORT_EDGE: int = 768            # лимит AIGate: короткая сторона ≥ ~720px
    MAX_PIXEL_BUDGET: int = 8_300_000          # лимит AIGate: общее число пикселей ≤ ~8.3M
                                               # (3840x2160 OK, 3072x3072 — нет)
    SIZE_STEP: int = 16                        # AIGate: W и H должны делиться на 16

    # === Чанкование батча (механика «как в Runway») ===
    CHUNK_SIZE: int = 4                       # картинок в одном чанке (запросе к AIGate)
    CHUNK_CONCURRENCY: int = 4                # сколько чанков идёт параллельно

    # === Валюта: курс USD→RUB для отображения цен в рублях ===
    USD_TO_RUB: float = 92.0                  # актуальный курс; меняй в .env

    # === Реальная цена (по факту кабинета AIGate) ===
    # AIGate берёт ≈ фикс. цену за картинку в зависимости от quality. Размер почти
    # не влияет на $ (2k ≈ 1:1 по цене за штуку). Измерено в кабинете (1:1 standard):
    #   low ≈ $0.0015, medium ≈ $0.0090, high ≈ $0.0120 (иногда до $0.03 — плавает).
    # Берём консервативное $0.012 для high (большинство записей кабинета).
    PRICE_PER_IMAGE_USD: Dict[str, float] = {
        "low": 0.0015,
        "medium": 0.0090,
        "high": 0.0120,
    }
    # Токены для отображения реального расхода после генерации (справочно).
    # Измерено: low≈208, medium≈1768, high≈7036 (1:1 standard).
    TOKENS_BY_QUALITY: Dict[str, int] = {
        "low": 208,
        "medium": 1768,
        "high": 7036,
    }
    # Множитель цены по площади относительно 1024x1024 (= 1.0).
    # Эмпирически: 2048x2048 → ×4 площади, 3840x3840 → ×14.

    # === Общий ключ (free-юзеры гоняют на нём) ===
    SHARED_AIGATE_KEY: str = ""

    # === Доступ / тарифы ===
    DEFAULT_TIER: str = "free"               # free|pro|full
    TIER_FREE_MAX_BATCH: int = 4
    TIER_PRO_MAX_BATCH: int = 10
    TIER_FULL_MAX_BATCH: int = 20
    TIER_FREE_KEY_SLOTS: int = 0
    TIER_PRO_KEY_SLOTS: int = 3
    TIER_FULL_KEY_SLOTS: int = 5
    FREE_DAILY_LIMIT: int = 5                # картинок в день на free-юзера на общем ключе

    # === Таймауты / ретраи ===
    GENERATION_TIMEOUT: int = 300            # 2k+high+референс идёт ~200с; ставим с запасом
    MAX_RETRIES: int = 3
    RETRY_BACKOFF_BASE: float = 1.5
    RETRY_BACKOFF_MAX: int = 30
    TRANSIENT_STATUSES: Any = Field(default_factory=lambda: [429, 500, 502, 503, 504])
    DEAD_STATUSES: Any = Field(default_factory=lambda: [401, 403])

    TELEGRAM_AUTH_MAX_AGE_SECONDS: int = 86400

    # === Референсы (переиспользуется на Этапе 4) ===
    MAX_UPLOAD_MB: int = 16
    ENABLE_IMAGE_OCR: bool = True
    OCR_LANG: str = "eng+rus"
    OCR_MAX_CHARS_PER_IMAGE: int = 10000
    OCR_MIN_TEXT_CHARS: int = 20
    PREPARE_IMAGE_REFERENCES: bool = True
    REFERENCE_CANVAS_LONG_EDGE: int = 1536
    REFERENCE_PAD_MODE: str = "blur"
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # === Local UI preview only. Keep false on a real server. ===
    ALLOW_DEV_AUTH: bool = False
    DEV_TELEGRAM_ID: int = 100000001

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_int_list(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "CORS_ORIGINS",
        "IMAGE_QUALITIES",
        "IMAGE_FORMATS",
        "IMAGE_ASPECTS",
        "IMAGE_SIZE_TIERS",
        "TRANSIENT_STATUSES",
        "DEAD_STATUSES",
        mode="before",
    )
    @classmethod
    def parse_list(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def cors_origins(self) -> List[str]:
        origins = {
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        }
        if self.WEBAPP_URL:
            origins.add(self.WEBAPP_URL.rstrip("/"))
        origins.update(origin.rstrip("/") for origin in self.CORS_ORIGINS if origin)
        return sorted(origins)

    @property
    def transient_statuses(self) -> set:
        return {int(s) for s in self.TRANSIENT_STATUSES}

    @property
    def dead_statuses(self) -> set:
        return {int(s) for s in self.DEAD_STATUSES}

    def tier_max_batch(self, tier: str) -> int:
        return {"free": self.TIER_FREE_MAX_BATCH, "pro": self.TIER_PRO_MAX_BATCH,
                "full": self.TIER_FULL_MAX_BATCH}.get(tier, self.TIER_FREE_MAX_BATCH)

    def tier_key_slots(self, tier: str) -> int:
        return {"free": self.TIER_FREE_KEY_SLOTS, "pro": self.TIER_PRO_KEY_SLOTS,
                "full": self.TIER_FULL_KEY_SLOTS}.get(tier, self.TIER_FREE_KEY_SLOTS)

    # ---------------- Модели изображения ----------------

    def get_image_model(self, key: str) -> Optional[Dict[str, Any]]:
        return self.IMAGE_MODELS.get(key)

    def is_supported_model(self, key: str) -> bool:
        return key in self.IMAGE_MODELS

    def model_engine(self, key: str) -> str:
        model = self.get_image_model(key)
        return model["engine"] if model else "openai"

    def gemini_image_size(self, size_tier: str) -> str:
        """size_tier → image_size для image_config Gemini (fallback на 1K)."""
        return self.GEMINI_SIZE_MAP.get(size_tier, "1K")

    def gemini_tokens_per_image(self, size_tier: str) -> int:
        return self.GEMINI_TOKENS_BY_SIZE.get(self.gemini_image_size(size_tier), 1250)

    def model_supports_quality(self, key: str) -> bool:
        model = self.get_image_model(key)
        return bool(model and model.get("supports_quality"))

    def estimate_price_usd(self, size: str, quality: str, n: int, model_key: str = "gpt") -> float:
        """Оценка цены до запуска.
        gpt: фикс. $ за картинку по quality × n (размер почти не влияет).
        banana: реальная цена по размеру (из замеров cost_usd AIGate) × n.
        ВАЖНО: это ОРИЕНТИР до запуска — финальный расход AIGate возвращает в cost_usd ответа."""
        if self.model_engine(model_key) == "gemini":
            tier = self._size_tier_from_size(size)
            base = self.GEMINI_PRICE_USD_BY_SIZE.get(self.gemini_image_size(tier), 0.00902)
            return round(base * n, 4)
        base = self.PRICE_PER_IMAGE_USD.get(quality, self.PRICE_PER_IMAGE_USD["medium"])
        return round(base * n, 4)

    def estimate_tokens(self, size: str, quality: str, n: int, model_key: str = "gpt") -> int:
        """Оценка токенов (справочно). gpt — по quality × множитель площади; banana — по размеру × n."""
        if self.model_engine(model_key) == "gemini":
            return self.gemini_tokens_per_image(self._size_tier_from_size(size)) * n
        base = self.TOKENS_BY_QUALITY.get(quality, self.TOKENS_BY_QUALITY["medium"])
        return int(base * self._size_area_factor(size) * n)

    def _size_tier_from_size(self, size: str) -> str:
        """Обратный маппинг WxH → size_tier (для оценки токенов бананы по размеру)."""
        try:
            long_edge = max(int(x) for x in size.lower().split("x"))
        except Exception:
            return "standard"
        if long_edge >= 3000:
            return "max"
        if long_edge >= 1800:
            return "2k"
        return "standard"

    def _size_area_factor(self, size: str) -> float:
        try:
            w, h = (int(x) for x in size.lower().split("x"))
            return (w * h) / (1024 * 1024)
        except Exception:
            return 1.0

    def usd_to_rub(self, usd: float) -> float:
        return round(usd * self.USD_TO_RUB, 2)

    def tokens_to_usd(self, tokens: int) -> float:
        """Справочно: оценка $ по токенам (грубо, реальная цена — по балансу/кабинету)."""
        # Усреднённая ставка: ~$0.004/1k токенов (из кабинета: 7024 ток = $0.012-0.03).
        return round(tokens / 1000.0 * 0.004, 4)

    def tokens_to_rub(self, tokens: int) -> float:
        return round(self.tokens_to_usd(tokens) * self.USD_TO_RUB, 2)

    # Соотношения сторон → числовое отношение W/H. AIGate принимает любой WxH
    # (до 3840px по длинной стороне). Размер считаем под выбранный уровень (tier).
    ASPECT_RATIOS: Dict[str, float] = {
        "1:1": 1.0,
        "9:16": 9.0 / 16.0,    # вертикаль / сторис
        "16:9": 16.0 / 9.0,    # альбом / экран
        "4:3": 4.0 / 3.0,
        "3:4": 3.0 / 4.0,
        "3:2": 3.0 / 2.0,
        "2:3": 2.0 / 3.0,
        "4:5": 4.0 / 5.0,
    }

    # База — ДЛИННАЯ сторона для каждого уровня размера.
    # AIGate: длинная сторона ≤ 3840px. standard даёт ~1024px по длинной (как у оригинала).
    TIER_BASE_EDGE: Dict[str, int] = {
        "standard": 1024,   # базовый размер (длинная сторона)
        "2k": 2048,         # больше детализации
        "max": 3840,        # максимум, что принимает AIGate
    }

    def compute_size(self, aspect: str, tier: str = "standard") -> str:
        """Считает WxH под аспект и уровень размера, соблюдая лимиты AIGate:
        - длинная сторона ≤ 3840px
        - короткая сторона ≥ 768px (иначе 'too small')
        - общее число пикселей ≤ ~8.3M
        - W и H кратны 16."""
        ratio = self.ASPECT_RATIOS.get(aspect, 1.0)
        max_long = self.MAX_IMAGE_LONG_EDGE      # 3840
        min_short = self.MIN_IMAGE_SHORT_EDGE    # 768
        step = self.SIZE_STEP                    # 16

        def round16(x):
            return max(step, (int(x) // step) * step)

        long_edge = min(self.TIER_BASE_EDGE.get(tier, 1024), max_long)
        if aspect == "1:1":
            w = h = long_edge
        elif ratio >= 1:
            w = long_edge
            h = round(long_edge / ratio)
        else:
            h = long_edge
            w = round(long_edge * ratio)

        # Короткая сторона ≥ 768: если не выполняется, поднимаем короткую,
        # а длинную пересчитываем по пропорции (но не выше 3840).
        if ratio >= 1:
            short = h
            if short < min_short:
                h = min_short
                w = round(h * ratio)
        else:
            short = w
            if short < min_short:
                w = min_short
                h = round(w / ratio)

        w = min(round16(w), max_long)
        h = min(round16(h), max_long)

        # Пиксельный бюджет: если W*H > budget, масштабируем вниз по пропорции.
        budget = self.MAX_PIXEL_BUDGET
        if w * h > budget:
            scale = (budget / (w * h)) ** 0.5
            w = round16(w * scale)
            h = round16(h * scale)
        return f"{w}x{h}"

    def aspect_to_size(self, aspect: str, tier: str = "standard") -> str:
        return self.compute_size(aspect, tier)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
