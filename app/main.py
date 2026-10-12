from fastapi import FastAPI

from app.api.routes import router as api_router
from app.config import get_settings, validate_runtime_secrets
from app.logging_config import configure_logging

settings = get_settings()
validate_runtime_secrets(settings)
configure_logging(settings.log_level)

app = FastAPI(title="Text-to-SQL Platform", version="0.1.0")
app.include_router(api_router)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
