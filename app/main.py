from fastapi import FastAPI

from app.config import get_settings
from app.logging_config import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="Text-to-SQL Platform", version="0.1.0")

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
