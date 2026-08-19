from langchain_core.messages import SystemMessage
from src.config.config_openai import GetOpenAILlm
from src.core.system_prompt import SYSTEM_PROMPT
from src.tools.tools import (
    calculate_distance_routes_api,
    get_cityinfo,
    get_detailed_tourist_places,
    get_google_hotels_sorted_by_rating,
    get_google_hotels_by_facilities,
    get_nearby_restaurants,
)
tools = [
    get_cityinfo,
    get_detailed_tourist_places,
    get_google_hotels_sorted_by_rating,
    get_google_hotels_by_facilities,
    get_nearby_restaurants,
    calculate_distance_routes_api,
]
llm = GetOpenAILlm().bind_tools(tools)


def chatbot(state):
    messages = list(state.get("messages", []))
    if not any(getattr(message, "type", None) == "system" for message in messages):
        messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
    response = llm.invoke(messages)
    return {"messages": [response]}
