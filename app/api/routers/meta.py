"""Instance metadata: what the footer and the legal pages render."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import get_settings_dep
from app.config import Settings
from app.contact import imprint_payload, legal_payload
from app.schemas import MetaOut

router = APIRouter(tags=["meta"])


@router.get("/meta", response_model=MetaOut)
async def meta(settings: Settings = Depends(get_settings_dep)) -> MetaOut:
    """Also the client's CSRF bootstrap: the response sets both cookies."""
    legal = legal_payload(settings)
    return MetaOut(
        app_name=settings.app_name,
        version=__version__,
        seo_enabled=settings.seo_enabled,
        seo_site_name=settings.seo_site_name,
        seo_description=settings.seo_description,
        imprint_enabled=legal["imprint_enabled"],
        data_location=settings.data_location,
        contact_links=legal["contact_links"],
        contact_email=legal["contact_email"],
        license_email=legal["license_email"],
    )


@router.get("/imprint")
async def imprint(settings: Settings = Depends(get_settings_dep)) -> dict:
    return imprint_payload(settings)


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "version": __version__}
