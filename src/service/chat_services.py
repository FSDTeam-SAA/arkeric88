from langchain_core.messages import SystemMessage, ToolMessage
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
TOOLS = [
    get_cityinfo,
    get_detailed_tourist_places,
    get_google_hotels_sorted_by_rating,
    get_google_hotels_by_facilities,
    get_nearby_restaurants,
    calculate_distance_routes_api,
]
TOOL_BY_NAME = {tool.name: tool for tool in TOOLS}


def _message_content_to_text(content) -> str:
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


def get_ai_response(prompt: str) -> str:
    llm = GetOpenAILlm().bind_tools(TOOLS)
    messages = [SystemMessage(content=SYSTEM_PROMPT), ("user", prompt)]
    for _ in range(5):
        response = llm.invoke(messages)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return _message_content_to_text(getattr(response, "content", None))
        messages.append(response)
        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            tool = TOOL_BY_NAME.get(tool_name)
            if tool is None:
                tool_result = {"error": f"Unknown tool: {tool_name}"}
            else:
                tool_result = tool.invoke(tool_call.get("args", {}))
            messages.append(
                ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call.get("id", tool_name or "tool_call"),
                )
            )
    return _message_content_to_text(getattr(response, "content", None))
