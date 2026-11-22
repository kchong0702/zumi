import uvicorn

from src.core.config import get_settings


def main() -> None:
    """Starts the uvicorn server"""
    settings = get_settings()
    uvicorn.run(
        "src.app.app:create_app",
        factory=True,
        host=settings.app.host,
        port=settings.app.port,
        workers=settings.app.workers,
        log_level=settings.app.log_level,
        access_log=settings.app.access_log,
        log_config=None,
        loop="asyncio",
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
