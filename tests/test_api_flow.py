import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import main
from app.schemas.city_body import QuestionAnswers
from src.service.chat_services import get_ai_response
from src.tools.tools import get_detailed_tourist_places


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

    def _v2_payload(self) -> dict:
        return {
            "archetype": "burned_out_achiever",
            "escape_from": "noise_stimulation",
            "arrival_priority": "silence_privacy",
            "structure_preference": "optional_rituals",
            "reset_style": "digital_disconnection",
            "physical_intensity": "gentle",
            "party": {"type": "solo", "adults": 1, "children": 0},
            "spirituality": "none",
            "travel_window": {"mode": "flexible"},
            "planning_service_level": "well_planned",
            "restrictions": {"text": "", "codes": []},
            "settings": [],
            "budget": {"currency": "USD", "per_person_per_night_max": 500, "open_ended": False},
            "duration": {"bucket": "4_7_nights"},
            "transform_focus": ["Burnout Recovery", "Sleep"],
        }

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
        Step 1 (POST /get_suggested_city) now ranks real properties and groups
        them into cities deterministically -- no LLM city-invention call is
        involved, so only the get_cityinfo enrichment tool and the tour-plan
        LLM step (Step 2) need mocking.
        """
        def fake_tour_plan_ai(prompt: str) -> str:
            return next(self.tour_plan_responses)

        with patch("src.service.chat_services.get_ai_response", side_effect=RuntimeError("LLM disabled in tests")), \
             patch("app.router.city_content_route.get_ai_response", side_effect=fake_tour_plan_ai), \
             patch("app.router.city_content_route.get_cityinfo") as mock_tool, \
             patch("app.router.city_content_route.get_detailed_tourist_places") as mock_places, \
             patch("app.router.city_content_route.get_nearby_restaurants") as mock_restaurants, \
             patch("app.router.city_content_route.get_google_hotels_by_facilities") as mock_hotel_facilities, \
             patch("app.router.city_content_route.calculate_distance_routes_api") as mock_distance:
            mock_tool.invoke.return_value = {
                "city_name": "MockCity",
                "country": "Mockland",
                "lat": 46.0,
                "lng": 2.0,
                "photos": ["https://example.com/photo.jpg"],
            }
            mock_places.invoke.side_effect = [
                [{"name": "Museum", "address": "1 Museum St, Paris", "photos": ["https://example.com/museum.jpg"]}],
                [{"name": "Cafe Central", "address": "2 Cafe St, Paris", "photos": ["https://example.com/cafe.jpg"]}],
            ]
            mock_restaurants.invoke.return_value = [{
                "name": "Cafe Meal",
                "address": "3 Meal St, Paris",
                "photos": ["https://example.com/meal.jpg"],
                "rating": 4.2,
            }]
            mock_hotel_facilities.invoke.return_value = [{
                "name": "Hotel Paris",
                "address": "1 Hotel St, Paris",
                "rating": 4.5,
                "price_level": "PRICE_LEVEL_MODERATE",
                "photos": ["https://example.com/hotel.jpg"],
                "coords": {"lat": 48.0, "lng": 2.0},
            }]
            mock_distance.invoke.return_value = {
                "distance_km": 2.5,
                "duration_minutes": 10,
            }

            # Step 1: rank properties, get real cities back with property_id + match_score.
            initial = self.client.post("/get_suggested_city", json=self._v2_payload())
            self.assertEqual(initial.status_code, 200)
            suggested_cities = initial.json()["suggested_cities"]
            self.assertTrue(suggested_cities)
            first_city = suggested_cities[0]
            self.assertTrue(first_city["property_id"].startswith("retreat_"))
            self.assertIsInstance(first_city["match_score"], int)
            session_id = initial.json()["session_id"]

            # Regenerate: deterministic, excludes cities already shown.
            regenerated = self.client.post(
                "/regenerate_suggested_city",
                json={"session_id": session_id, "user_instruction": "more energetic"},
            )
            self.assertEqual(regenerated.status_code, 200)
            regenerated_ids = {city["property_id"] for city in regenerated.json()["suggested_cities"]}
            self.assertFalse(regenerated_ids & {first_city["property_id"]})

            # Step 2: select a city by property_id, get activities (unchanged flow).
            plan = self.client.post(
                "/get_tour_plan",
                json={
                    "session_id": session_id,
                    "selected_city": first_city["city_name"],
                    "property_id": first_city["property_id"],
                },
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
            regenerated_activities = plan_regenerated.json()["tour_plan"][0]["activities"]
            first_non_meal = next(
                activity for activity in regenerated_activities
                if not activity["activity_name"].startswith(("Breakfast", "Lunch", "Dinner"))
            )
            self.assertEqual(first_non_meal["activity_name"], "Cafe Central")
            details = self.client.get(f"/session/{session_id}")
            self.assertEqual(details.status_code, 200)
            self.assertEqual(len(details.json()["suggested_cities"]), len(suggested_cities))

    def test_regenerate_suggested_city_rejects_legacy_sessions(self) -> None:
        """A session with no stored v2 profile (pre-dating this flow) cannot be regenerated."""
        from src.session.city_session_store import CitySessionStore
        legacy_session = CitySessionStore.create(
            questions_answers=QuestionAnswers(
                todays_feeling="curious",
                experience_kind="culture",
                energy_level="medium",
                travel_style="slow",
                trip_organization="loose",
                activity_restrictions=[],
                life_season="exploration",
                preferred_environments=["cities"],
                total_trip_budget=1200.0,
                trip_length_days=3,
            ),
            suggested_cities=[],
            response={"suggested_cities": []},
        )
        response = self.client.post(
            "/regenerate_suggested_city",
            json={"session_id": legacy_session.session_id, "user_instruction": "anything"},
        )
        self.assertEqual(response.status_code, 409)

    def test_profile_budget_region_and_dynamic_place_query(self) -> None:
        profile = QuestionAnswers(
            todays_feeling="overwhelmed",
            experience_kind="nervous system reset",
            energy_level="low",
            travel_style="solo and slow",
            trip_organization="semi-guided",
            activity_restrictions=["steep hikes"],
            life_season="recovery",
            preferred_environments=["forest", "water"],
            budget_per_person_per_night=250,
            trip_length_days=4,
            preferred_region="Asia",
        )
        self.assertEqual(profile.effective_total_budget, 1000)

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

if __name__ == "__main__":
    unittest.main()
