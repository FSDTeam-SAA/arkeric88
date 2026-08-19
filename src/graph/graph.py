from langgraph.graph import StateGraph, START, END
from src.graph.nodes import chatbot, tools
from src.graph.state import ChatState
from langgraph.prebuilt import ToolNode, tools_condition

builder = StateGraph(ChatState)


builder.add_node("chatbot", chatbot)
builder.add_node("tools", ToolNode(tools))



builder.add_edge(START, "chatbot")

builder.add_conditional_edges("chatbot", tools_condition)

builder.add_edge("tools", "chatbot")

# builder.add_edge( "chatbot", END)

graph = builder.compile()