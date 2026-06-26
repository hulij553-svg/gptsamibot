"""Run the aiogram bot and FastAPI server in one process."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api_routes import router as api_router
from app.config import settings
from app.database import db
from app.handlers import router as bot_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database")
    await db.init()
    Path(settings.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    yield
    logger.info("Shutdown complete")


app = FastAPI(title="GPT Image Bot API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
Path(settings.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.MEDIA_DIR), name="media")
app.include_router(api_router)

frontend_dist = Path("frontend_dist")
if frontend_dist.exists():
    assets_dir = frontend_dist / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend_assets")

    @app.get("/", include_in_schema=False)
    async def frontend_root():
        return FileResponse(frontend_dist / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend_fallback(full_path: str):
        if full_path.startswith(("api/", "media/", "ws")):
            raise HTTPException(status_code=404, detail="Not found")

        requested = frontend_dist / full_path
        if requested.is_file():
            return FileResponse(requested)
        return FileResponse(frontend_dist / "index.html")

bot = Bot(token=settings.BOT_TOKEN or "000000:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_router(bot_router)


async def start_bot():
    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def start_api():
    config = uvicorn.Config(
        app,
        host=settings.WEBAPP_HOST,
        port=settings.WEBAPP_PORT,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    logger.info("Starting bot and API")
    await asyncio.gather(start_bot(), start_api())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped")
