from typing import List, Optional, Literal, Dict
from pydantic import BaseModel
from app.schemas.city_body import QuestionAnswers


class CitySuggestion(BaseModel):
    city_name: str
    country_name: str
    number_of_days: int

class TourPlanActivity(BaseModel):
    activity_name: str
    activity_description: str
    activity_location: str
    activity_time: str
    activity_cost: float


class TourPlanDay(BaseModel):
    day: int
    activities: List[TourPlanActivity]


class CitySuggestionResponse(BaseModel):
    questions_answers: QuestionAnswers
    suggested_cities: List[CitySuggestion]


class TourPlanResponse(BaseModel):
    questions_answers: QuestionAnswers
    suggested_citie: CitySuggestion
    suggested_tour_plan: List[TourPlanDay]