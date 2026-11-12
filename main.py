import os

from app.router.city_content_route import router as city_content_router
from app.router.dates_route import router as dates_router
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

API_VERSION_PREFIX = "/v1"

app = FastAPI(title="Travel Planner API v1", version="2.0.0")

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
for router in (city_content_router, dates_router):
    # Every endpoint lives under /v1 (e.g. POST /v1/get_suggested_city); Swagger shows only these.
    app.include_router(router, prefix=API_VERSION_PREFIX)
    # The unversioned paths keep working, hidden from Swagger, until the website has moved to /v1.
    app.include_router(router, include_in_schema=False)
