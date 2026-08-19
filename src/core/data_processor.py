import json
from uuid import uuid4
from typing import List, Dict

import re
import ast



class DataProcessor:
    @staticmethod
    def _get_value(item, key):
        if isinstance(item, dict):
            return item.get(key, "")
        return getattr(item, key, "")

    @staticmethod
    def process_previous_history(previous_data):
        if not previous_data:
            return "No previous conversation."

        history_lines = []
        for index, item in enumerate(previous_data[-10:], start=1):
            user_query = DataProcessor._get_value(item, "user_query")
            ai_response = DataProcessor._get_value(item, "ai_response")
            history_lines.append(f"{index}. User: {user_query}\nAI: {ai_response}")

        return "\n\n".join(history_lines)

    @staticmethod
    def file_reader_route(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return {
                    "is_read": True,
                    "data": file.read()
                }
        except UnicodeDecodeError:
            return {
                "is_read": False,
                "data": "Uploaded file could not be read as text."
            }



class ProcessData:
    
    @staticmethod
    def CleanData(text):
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        # print(f"Cleaned text: {cleaned}")  # For debugging, see how it's being cleaned.

        # Try parsing it as JSON first
        try:
            # Attempt to load it as JSON
            cleaned = json.loads(cleaned)
        except json.JSONDecodeError:
            # If JSON parsing fails, try using literal_eval (assuming it's a Python literal expression)
            try:
                cleaned = ast.literal_eval(cleaned)
            except (ValueError, SyntaxError) as e:
                # print(f"Error parsing content with ast.literal_eval: {e}")
                cleaned = {None}  # Or handle the error as needed (e.g., return a default value or an empty dict/list)

        return cleaned

    @staticmethod
    def EnsureDict(text):
        data = ProcessData.CleanData(text)
        if not isinstance(data, dict):
            raise ValueError("AI response was not a valid JSON object.")
        return data
            
            

