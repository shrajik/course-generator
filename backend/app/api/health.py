from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.services.openai_service import get_ai_client

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    client = get_ai_client()
    return {
        "status": "ok",
        "version": settings.app_version,
        "ai_client": "mock" if client.is_mock else "openai",
        "research_mode": settings.research_mode,
        "image_generation": settings.enable_image_generation,
        "data_dir": str(settings.data_dir),
    }


@router.get("/")
async def root() -> dict[str, Any]:
    settings = get_settings()
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "endpoints": [
            "POST /api/courses",
            "POST /api/courses/improve-toc",
            "POST /api/courses/{course_id}/generate",
            "GET  /api/courses/{course_id}",
            "GET  /api/courses/{course_id}/document",
            "POST /api/documents/{document_id}/ai-edit",
            "POST /api/documents/{document_id}/export/pdf",
        ],
    }
