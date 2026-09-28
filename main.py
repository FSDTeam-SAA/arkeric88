from app.router.city_content_route import router as city_content_router
from fastapi import FastAPI

app = FastAPI(title="Travel Planner API", version="2.0.0")
app.include_router(city_content_router)
