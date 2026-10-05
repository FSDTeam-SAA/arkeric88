import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import main
from src.service.chat_services import get_ai_response
from src.tools.tools import get_detailed_tourist_places
from intake_fixtures import build_intake


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
    def bind_tools(self, tools):
        self.tools = tools
        return self
    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_cityinfo", "args": {"city_name": "Paris"}, "id": "call-1"}
                ],
            )
        return AIMessage(
            content='{"suggested_cities":[{"city_name":"Paris","country_name":"France","city_image":["https://example.com/paris.jpg"],"latitude":48.8566,"longitude":2.3522,"number_of_days":3,"description":"Romantic"}],"reasoning":"done"}'
        )


class TravelPlannerFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.tour_plan_responses = iter([
            '{"tour_plan":[{"day":1,"activities":[{"activity_name":"Museum","activity_description":"Visit","activity_location":"Louvre","activity_time":"10:00","activity_cost":20}]}],"total_cost_estimate":20,"packing_tips":"light","travel_tips":"walk"}',
            '{"tour_plan":[{"day":1,"activities":[{"activity_name":"Cafe","activity_description":"Coffee","activity_location":"Center","activity_time":"11:00","activity_cost":10}]}],"reasoning":"updated"}',
        ])

    def test_get_ai_response_executes_tool_calls(self) -> None:
        fake_llm = FakeLLM()
        tool_mock = Mock()
        tool_mock.invoke.return_value = {"photos": ["https://example.com/paris.jpg"]}
        with patch("src.service.chat_services.GetOpenAILlm", return_value=fake_llm), patch.dict(
            "src.service.chat_services.TOOL_BY_NAME", {"get_cityinfo": tool_mock}
        ):
            response = get_ai_response("Suggest a city")
        self.assertIn("Paris", response)
        self.assertEqual(fake_llm.calls, 2)
        tool_mock.invoke.assert_called_once_with({"city_name": "Paris"})

    def test_city_and_tour_session_flow(self) -> None:
        """
        Step 1 (POST /get_suggested_city) ranks the destination catalog
        deterministically -- no LLM is involved -- so only the map lookup and
        the tour-plan LLM step (Step 2) need mocking.
        """
        def fake_tour_plan_ai(prompt: str) -> str:
            if "The feeling behind your journey" in prompt:
                return "{}"  # Unusable answer: the block falls back to "not_assessed".
            return next(self.tour_plan_responses)

        def fake_city_lookup(args: dict) -> dict:
            # A real lookup echoes the requested place in its own country.
            return {
                "city_name": args["city_name"],
                "country": args["region_hint"],
                "lat": 10.0,
                "lng": 20.0,
                "photos": ["https://example.com/photo.jpg"],
            }

        with patch("app.router.city_content_route.get_ai_response", side_effect=fake_tour_plan_ai), \
             patch("src.core.destination_places.get_cityinfo") as mock_lookup, \
             patch("app.router.city_content_route.get_detailed_tourist_places") as mock_places, \
             patch("app.router.city_content_route.get_nearby_restaurants") as mock_restaurants, \
             patch("app.router.city_content_route.get_google_hotels_sorted_by_rating") as mock_hotels:
            mock_lookup.invoke.side_effect = fake_city_lookup
            place_batches = iter([
                {"name": "Museum", "street": "1 Museum St", "photos": ["https://example.com/museum.jpg"]},
                {"name": "Cafe Central", "street": "2 Cafe St", "photos": ["https://example.com/cafe.jpg"]},
            ])

            def fake_places(args: dict) -> list:
                # Real results sit inside the destination's country ("City, Country").
                place = next(place_batches)
                country = args["location_name"].rsplit(",", 1)[1].strip()
                return [{"name": place["name"], "address": f"{place['street']}, {country}", "photos": place["photos"]}]

            mock_places.invoke.side_effect = fake_places
            mock_restaurants.invoke.return_value = [{
                "name": "Cafe Meal",
                "address": "3 Meal St",
                "photos": ["https://example.com/meal.jpg"],
                "rating": 4.2,
            }]
            mock_hotels.invoke.return_value = [{
                "name": "Hotel Calm",
                "address": "1 Hotel St",
                "rating": 4.5,
                "price_level": "PRICE_LEVEL_MODERATE",
                "photos": ["https://example.com/hotel.jpg"],
                "coords": {"lat": 48.0, "lng": 2.0},
            }]

            # Step 1: rank the catalog, get real destinations with destination_id + match_score.
            initial = self.client.post("/get_suggested_city", json=build_intake())
            self.assertEqual(initial.status_code, 200)
            suggested_cities = initial.json()["suggested_cities"]
            self.assertTrue(suggested_cities)
            first_city = suggested_cities[0]
            self.assertTrue(first_city["destination_id"])
            self.assertIsInstance(first_city["match_score"], int)
            self.assertEqual(first_city["city_image"], ["https://example.com/photo.jpg"])
            self.assertEqual(first_city["evidence"]["place_lookup"]["outcome"], "MATCHED")
            session_id = initial.json()["session_id"]

            # Regenerate: deterministic, excludes destinations already shown.
            regenerated = self.client.post(
                "/regenerate_suggested_city",
                json={"session_id": session_id, "user_instruction": "something different"},
            )
            self.assertEqual(regenerated.status_code, 200)
            regenerated_ids = {city["destination_id"] for city in regenerated.json()["suggested_cities"]}
            self.assertFalse(regenerated_ids & {first_city["destination_id"]})

            # Step 2: select a destination by destination_id, get activities.
            plan = self.client.post(
                "/get_tour_plan",
                json={"session_id": session_id, "destination_id": first_city["destination_id"]},
            )
            self.assertEqual(plan.status_code, 200)
            self.assertEqual(plan.json()["source"], "generated")
            plan_regenerated = self.client.post(
                "/regenerate_tour_plan",
                json={
                    "activity_session_id": plan.json()["activity_session_id"],
                    "day_to_regenerate": 1,
                    "user_instruction": "more relaxed",
                },
            )
            self.assertEqual(plan_regenerated.status_code, 200)
            self.assertEqual(plan_regenerated.json()["destination_id"], first_city["destination_id"])
            regenerated_activities = plan_regenerated.json()["tour_plan"][0]["activities"]
            first_experience = next(
                activity for activity in regenerated_activities if activity["item_type"] == "experience"
            )
            self.assertEqual(first_experience["activity_name"], "Cafe Central")
            details = self.client.get(f"/session/{session_id}")
            self.assertEqual(details.status_code, 200)
            self.assertEqual(details.json()["intake"]["trip_goals"], ["restoration", "reflection"])
            self.assertEqual(len(details.json()["suggested_cities"]), len(regenerated_ids))

    def test_dynamic_place_query(self) -> None:
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {"places": []}
        with patch("src.tools.tools.requests.post", return_value=mock_response) as post:
            result = get_detailed_tourist_places.invoke({
                "location_name": "Kyoto",
                "search_query": "quiet forest mindfulness restorative experiences",
            })
        self.assertEqual(result, [])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["textQuery"],
            "quiet forest mindfulness restorative experiences in Kyoto",
        )
        self.assertNotIn("includedType", payload)
        field_mask = post.call_args.kwargs["headers"]["X-Goog-FieldMask"]
        self.assertIn("places.id", field_mask)
        self.assertIn("places.businessStatus", field_mask)

if __name__ == "__main__":
    unittest.main()
