from langchain_openai import ChatOpenAI
from functools import lru_cache
from src.config import config_env

@lru_cache
def GetOpenAILlm(model_name="gpt-5.4-nano-2026-03-17", temp = 0) -> ChatOpenAI:
    llm = ChatOpenAI(
        api_key=config_env.settings.openai_api_key,
        model=model_name,
        temperature=temp
    )

    return llm