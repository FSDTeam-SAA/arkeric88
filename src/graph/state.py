from langgraph.graph import MessagesState

class ChatState(MessagesState):
    user_id: str
    language: str
    documents: list