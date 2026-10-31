# Client test request bodies (October 2026 customer tests)

The client's five questionnaire tests as `POST /v1/get_suggested_city` bodies, for Swagger
(`http://localhost:8000/docs` → **POST /v1/get_suggested_city** → *Try it out* → paste a file).
The old path `/get_suggested_city` still works and gives the same result.
Each file differs from Test 1 only where the client's test changed it.

| File | Client test | What changes from Test 1 |
| --- | --- | --- |
| `test_1_restoration_reflection.json` | Test 1 | — |
| `test_2_discovery_inspiration.json` | Test 2 | Curious; Discovery + Inspiration (main: Discovery); "I'm curious to explore" |
| `test_3_nearby.json` | Test 3 | `travel_distance: nearby` |
| `test_4_unrecognized_departure.json` | Test 4 | `departure_location: "Testville, TX"` |
| `test_5_accessibility.json` | Test 5 | Mobility or accessibility, **must avoid** (replaces No intense activity) |
| `test_5b_accessibility_with_details.json` | Test 5 + new Q8 details | Adds `accessibility_needs: step_free_access, limited_walking` |
| `test_6_flexible_20_day_window.json` | Flexible-dates finding | 20-day window 1–21 Dec 2026, 5 nights, still `flexible` |
| `test_7_decide_later.json` | "Decide later" | No `departure_location` / `travel_distance` |

## Previous build vs. current build

Full responses are saved in `results/`:

- `results/previous_build_9913196/`: commit `9913196` (17 Oct 2026, "8. ITINERARY_VALIDATION_CHANGES").
  It reproduces every result the client reported, so it is the build the website was using.
  Fields that build does not accept (`primary_trip_goal`, `accessibility_needs`,
  `earliest_check_in`, `latest_check_out`) were left out of the request and are listed in
  `fields_not_supported_by_this_build`; the old website could not send them either.
- `results/current_build/`: `main` after the 30 Oct 2026 fixes.

Both were run with Austin located by Google Maps. On a server, check that `response.scoring_version`
is `velari-destinations-2026-10-30.1` and `response.total_candidate_count` is `1000`; otherwise it runs an older build.

| Test | Client reported | Previous build (9913196) | Current build |
| --- | --- | --- | --- |
| 1 | Ubud 100, Rishikesh 92, Baa Atoll 85 | Ubud 100, Rishikesh 92, Baa Atoll 85; label "Restored" | Ubud 87 (Excellent), Rishikesh 78 (Strong), Sedona 72 (Strong); label "Restored and reflective". Ubud and Rishikesh carry a "long journey for a 5-night trip" trade-off. |
| 2 | Ubud 80, Coffee Cultural Landscape 72, Valle de Guadalupe 65; every card "Curious" | Ubud 80, Coffee Cultural Landscape 72, Valle de Guadalupe 65; label "Curious" | Coffee Cultural Landscape 76, Ubud 71, Valle de Guadalupe 69; label "Curious and inspired" |
| 3 | Error: "AI matched response contains duplicate destination countries" | Sedona 72, Tucson 72 (both United States, both Arizona desert), Oaxaca 67. The website's duplicate-country check rejected this. | Tucson 72, Denver 68 (both United States, about 1,000 km apart, different settings), San Miguel de Allende 61. **The website's duplicate-country check must be removed or this still errors.** |
| 4 | Ubud 100, Rishikesh 92, Baa Atoll 85; label "Reflective" | Ubud 100, Rishikesh 92, Baa Atoll 85; label "Restored" with this body. The label was the first goal clicked, so "Reflective" means Reflection was clicked first. | Same three; label "Restored and reflective" in any click order; `origin.status` `UNVERIFIED`, `origin.input_text` "Testville, TX". Travel effort can't be measured without a located departure. |
| 5 | "Let's refine your journey" (no results) | `no_valid_result`: 67 destinations held back as unverified, 5 excluded; asks for details in "Other planning needs" or to mark it "I'd prefer to avoid this" | Ubud 79, Rishikesh 70, Tucson 64; each says accessibility will be checked before booking, plus a non-blocking question for details |
| 5b | — | Same `no_valid_result` (`accessibility_needs` not supported) | Same ranking as 5; "To confirm" names "step-free access, limited walking"; no clarification question |
| 6 | Website switched to "I have exact dates" | Same as Test 1; the window can't be sent | Same as Test 1; `guest_context.travel_window` keeps 2026-12-01 → 2026-12-21 and the timing stays flexible |
| 7 | Option not shown on the website | HTTP 422: `departure_location` "Field required" | Ubud 100, Rishikesh 92, Baa Atoll 85; `origin.status` `NOT_PROVIDED`; a non-blocking question asks for the departure city |

Budget does not change any result yet: no price-estimate file is in `data/`, so every response
includes the "No destination has a nightly price yet" note.
