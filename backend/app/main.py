"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, auth, courses, documents, health
from app.core.config import get_settings
from app.core.errors import CourseCreatorError
from app.core.logging import configure_logging, get_logger
from app.db.engine import dispose_engine

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.courses_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "%s v%s starting - data_dir=%s ai=%s research_mode=%s",
        settings.app_name,
        settings.app_version,
        settings.data_dir,
        "mock" if settings.use_mock_ai else "openai",
        settings.research_mode,
    )
    if settings.use_mock_ai:
        log.warning(
            "No OPENAI_API_KEY configured (or MOCK_OPENAI=true): generation will "
            "return offline placeholder content."
        )
    yield
    if settings.use_database:
        await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Backend POC for the AI Course Creation Platform.\n\n"
        "Pipeline: input -> planner -> blueprint -> research -> writer -> reviewer "
        "-> course document JSON -> images -> PDF.\n\n"
        "The Course Document JSON is the source of truth; the PDF is only an export."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(CourseCreatorError)
async def handle_domain_error(request: Request, exc: CourseCreatorError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(courses.router)
app.include_router(documents.router)
