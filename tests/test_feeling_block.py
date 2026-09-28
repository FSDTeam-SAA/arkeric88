"""'The feeling behind your journey' block: wording rules and validation."""

import json

import pytest

from app.schemas.intake_schema import TravelIntakeRequest
from src.core.feeling_block import assess_feeling_block, fallback_intention, headline, revision_note
from src.core.trip_profile import build_trip_profile
from intake_fixtures import build_intake

EXPERIENCES = [
    {"day": 1, "activity_name": "Moss garden walk", "activity_description": "Unhurried morning walk"},
    {"day": 2, "activity_name": "Tea ceremony", "activity_description": "Small-group tea ritual"},
    {"day": 3, "activity_name": "Philosopher's Path", "activity_description": "Quiet canal-side path"},
]


def _profile(**overrides):
    return build_trip_profile(TravelIntakeRequest(**build_intake(**overrides)))


def _answer(**overrides) -> str:
    payload = {
        "intention": "You want time to slow down and hear yourself think.",
        "narrative": (
            "Kyoto's moss garden walk and a small tea ceremony give you unhurried space to take in "
            "a new place while reconnecting with your own thoughts."
        ),
        "supporting_experience_ids": ["E1", "E2"],
        "supports_feeling": True,
        "mismatch_reason": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _assess(answers, profile=None):
    replies = iter(answers)
    prompts = []

    def fake_ai(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    block = assess_feeling_block(profile or _profile(trip_goals=["reflection"]), "Kyoto", "Japan", EXPERIENCES, fake_ai)
    return block, prompts


def test_headline_uses_the_selected_feelings_not_destination_tags():
    assert headline(_profile(trip_goals=["reflection"])) == "THE FEELING: REFLECTIVE"
    assert headline(_profile(trip_goals=["connection", "discovery"])) == "THE FEELING: CONNECTED & CURIOUS"


def test_aligned_block_has_bold_headline_and_real_supporting_experiences():
    block, prompts = _assess([_answer()])
    assert block["title"] == "The feeling behind your journey"
    assert block["alignment"]["status"] == "aligned"
    assert block["markdown"].startswith("**THE FEELING: REFLECTIVE**\nYou want time to slow down")
    assert block["supporting_experiences"] == [
        {"day": 1, "activity_name": "Moss garden walk"},
        {"day": 2, "activity_name": "Tea ceremony"},
    ]
    assert "Reflective" in prompts[0]
    assert "Philosopher's Path" in prompts[0]


@pytest.mark.parametrize("bad_answer, reason", [
    (_answer(narrative="In Kyoto you will feel calm after the moss garden walk and tea ceremony."), "promised"),
    (_answer(narrative="Kyoto's moss garden walk guarantees peace. The tea ceremony helps too."), "promised"),
    (_answer(narrative="Kyoto's garden walk and tea ceremony add stillness, so your week feels centered."), "promised"),
    (_answer(narrative="Kyoto's garden walk and tea ceremony, leaving you feeling rested."), "promised"),
    (_answer(supporting_experience_ids=["E1", "E9"]), "at least 2"),
    (_answer(narrative="Gardens and tea rituals give you room to think."), "must name Kyoto"),
    (_answer(intention="You want quiet. You want perspective."), "exactly one sentence"),
    (_answer(narrative="Kyoto is calm. The garden helps. The tea ceremony too."), "one or two sentences"),
    (_answer(narrative="Kyoto offers " + "a long unhurried garden morning and " * 12 + "tea."), "at most 45 words"),
    (_answer(intention="You want " + "a slow and quiet " * 6 + "break."), "at most 15 words"),
    ("not json", "JSON"),
    (_answer(narrative="Kyoto's garden walk (E1) and tea ceremony (E2) leave room to think."), "IDs"),
    (_answer(narrative="You want time to think in Kyoto. The garden walk and tea ceremony help."), "restate"),
    (_answer(narrative="You're looking for quiet in Kyoto's gardens and tea rooms."), "restate"),
])
def test_invalid_answers_are_retried_with_the_reason(bad_answer, reason):
    block, prompts = _assess([bad_answer, _answer()])
    assert block["alignment"]["status"] == "aligned"
    assert len(prompts) == 2
    assert reason in prompts[1]


def test_ids_are_read_even_when_the_model_copies_the_whole_line():
    block, _ = _assess([_answer(supporting_experience_ids=["e1", "E2 (day 2): Tea ceremony -- Small-group tea ritual"])])
    assert [item["activity_name"] for item in block["supporting_experiences"]] == ["Moss garden walk", "Tea ceremony"]
    assert block["alignment"]["status"] == "aligned"


def test_prompt_lists_experiences_with_ids():
    _, prompts = _assess([_answer()])
    assert "- E1 (day 1): Moss garden walk" in prompts[0]
    assert "- E3 (day 3): Philosopher's Path" in prompts[0]


def test_repeated_failures_fall_back_to_headline_and_intention_only():
    block, _ = _assess([_answer(supporting_experience_ids=[]), "still not json", "{}"])
    assert block["alignment"]["status"] == "not_assessed"
    assert block["narrative"] is None
    assert block["supporting_experiences"] == []
    assert block["intention"] == "You want quiet and perspective to think about what matters."
    assert "only the headline" in block["alignment"]["display_guidance"]


def test_model_can_flag_a_mismatch_instead_of_stretching():
    block, _ = _assess([_answer(supports_feeling=False, mismatch_reason="Every day is packed with nightlife.")])
    assert block["alignment"]["status"] == "mismatch"
    assert block["alignment"]["detail"] == "Every day is packed with nightlife."
    assert block["narrative"] is None
    assert "Do not present" in block["alignment"]["display_guidance"]
    note = revision_note(block)
    assert "Reflective" in note and "nightlife" in note


def test_fallback_intention_reflects_both_selected_feelings():
    profile = _profile(trip_goals=["restoration", "connection"])
    assert fallback_intention(profile) == (
        "You want space to slow down and feel less pulled in every direction, "
        "and meaningful time with people who matter to you."
    )


def test_prompt_uses_assessment_answers_but_no_private_text():
    profile = _profile(
        trip_goals=["reflection"],
        travel_party="couple",
        recent_feelings=["something_else"],
        recent_feelings_other="PRIVATE-FEELING",
    )
    _, prompts = _assess([_answer()], profile=profile)
    assert "My partner and me" in prompts[0]
    assert "Quiet and privacy" in prompts[0]
    assert "One highlight each day" in prompts[0]
    assert "PRIVATE" not in prompts[0]
