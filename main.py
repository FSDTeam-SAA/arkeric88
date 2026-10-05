import os

from app.router.city_content_route import router as city_content_router
from app.router.dates_route import router as dates_router
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Travel Planner API", version="2.0.0")

# Browsers block cross-origin calls (e.g. the account pages fetching a saved
# search) unless the API allows the frontend's origin. Set CORS_ALLOW_ORIGINS
# to a comma-separated list of frontend origins to allow them with
# credentials (cookies / auth headers). Unset, any origin may call without
# credentials.
_allowed_origins = [origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["*"],
    allow_credentials=bool(_allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(city_content_router)
app.include_router(dates_router)
