from app.router.city_content_route import router as city_content_router
from app.router.retreat_recommendation_route import router as retreat_recommendation_router
from fastapi import FastAPI

app = FastAPI(title="Travel Planner API", version="1.0.0")
app.include_router(city_content_router)
app.include_router(retreat_recommendation_router)