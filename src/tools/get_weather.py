from langchain_core.tools import tool
import random
@tool
def get_current_weather():
    """Get Current weather / or temperature"""
    return random.randint(18, 60)