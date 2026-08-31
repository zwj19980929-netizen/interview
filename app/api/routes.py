from fastapi import APIRouter

from app.api.routers import admin, catalog, interviews, plans, realtime, system, talent


router = APIRouter()
router.include_router(system.router)
router.include_router(admin.router)
router.include_router(catalog.router)
router.include_router(talent.router)
router.include_router(plans.router)
router.include_router(interviews.router)
router.include_router(realtime.router)
