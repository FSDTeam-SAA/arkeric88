# AI Tour Guide Platform - API Documentation & Flow Guide

## System Overview

This is a two-phase conversational tour guide system with session-based caching:
1. **Phase 1 (City Selection)**: User answers questions → AI suggests cities → stores results in `CitySession`
2. **Phase 2 (Activity Planning)**: User picks city → AI creates day-wise itinerary → stores results in `ActivitySession`

Both phases support **regenerate** to get different suggestions without re-answering questions.

---

## Key Principles

```
GENERATE (First Time)
  → Call AI
  → Store result in session
  → Return session_id

SUBSEQUENT CALLS (No Regenerate)
  → Check session cache
  → Return cached result
  → Skip AI call

REGENERATE (User Request)
  → Call AI again
  → Overwrite cached result
  → Keep history intact
  → Return updated session_id
```

---

## Phase 1: City Suggestion Flow

### Endpoint 1: Generate City Suggestions
**POST** `/get_suggested_city`

**Request:**
```json
{
  "questions_answers": {
    "todays_feeling": "hopeful",
    "experience_kind": "relaxation & nature",
    "energy_level": "medium",
    "travel_style": "slow travel",
    "trip_organization": "loosely planned",
    "activity_restrictions": ["extreme sports", "crowded places"],
    "life_season": "transition",
    "preferred_environments": ["beaches", "mountains"],
    "birthdate": "1990-05-15",
    "budget_per_person_per_night": 100,
    "trip_length_days": 7
  },
  "preferred_destinations": "tropical or mediterranean",
  "hope_of_this_trip": "find peace and reconnect with nature"
}
```

**Response:**
```json
{
  "session_id": "sess_abc123",
  "suggested_cities": [
    {
      "city_name": "Bali",
      "country_name": "Indonesia",
      "number_of_days": 7,
      "description": "Perfect for slow travel with diverse environments..."
    },
    {
      "city_name": "Coastal Portugal",
      "country_name": "Portugal",
      "number_of_days": 6,
      "description": "Peaceful beaches and mountain walks..."
    }
  ],
  "response": { ... } // Full AI response for reference
}
```

**What happens:**
1. AI analyzes Q&A and returns city suggestions
2. Session created with `session_id`
3. Q&A + suggestions stored in `CitySession`
4. History marked: `action: "generated"`

---

### Endpoint 2: Regenerate City Suggestions
**POST** `/regenerate_suggested_city`

**Request:**
```json
{
  "session_id": "sess_abc123",
  "user_instruction": "I'd prefer adventure over relaxation, and budget-friendly options"
}
```

**Response:**
```json
{
  "session_id": "sess_abc123", // Same session ID
  "suggested_cities": [
    {
      "city_name": "Peru",
      "country_name": "Peru",
      "number_of_days": 7,
      "description": "Adventure hiking, Machu Picchu, budget-friendly..."
    },
    {
      "city_name": "Thailand",
      "country_name": "Thailand",
      "number_of_days": 7,
      "description": "Island hopping, trekking, very affordable..."
    }
  ],
  "response": { ... }
}
```

**What happens:**
1. Retrieves existing session by `session_id`
2. Uses stored Q&A (NOT re-asked)
3. AI generates NEW suggestions (excluding previous ones if possible)
4. Overwrites `suggested_cities` in session
5. Keeps Q&A intact
6. History updated: `action: "regenerated"`, includes `user_instruction`
7. Same `session_id` returned (no new session created)

---

### Endpoint 3: Get Session Details (City)
**GET** `/session/{session_id}`

**Response:**
```json
{
  "session_id": "sess_abc123",
  "questions_answers": { ... },
  "suggested_cities": [ ... ],
  "regeneration_history": [
    {
      "action": "generated",
      "timestamp": "2026-07-04T10:30:00Z",
      "suggested_cities_count": 3
    },
    {
      "action": "regenerated",
      "timestamp": "2026-07-04T10:45:00Z",
      "update_field_name": "suggested_cities",
      "user_instruction": "I'd prefer adventure...",
      "suggested_cities_count": 2
    }
  ]
}
```

---

### Endpoint 4: Delete City Session
**DELETE** `/session/{session_id}`

**Response:**
```json
{
  "message": "Session and all linked activity sessions deleted successfully."
}
```

**What happens:**
1. Deletes the city session
2. Also deletes ALL activity sessions linked to this city session

---

## Phase 2: Activity/Tour Plan Flow

### Endpoint 5: Generate Tour Plan (Activities)
**POST** `/get_tour_plan`

**Request:**
```json
{
  "session_id": "sess_abc123",
  "selected_city": "Bali"
}
```

**Response (First Time - Generated):**
```json
{
  "activity_session_id": "act_sess_xyz789",
  "city": "Bali",
  "tour_plan": [
    {
      "day": 1,
      "activities": [
        {
          "activity_name": "Arrive in Seminyak",
          "activity_description": "Settle in, relax on beach",
          "activity_location": "Seminyak Beach",
          "activity_time": "2:00 PM - 6:00 PM",
          "activity_cost": 0
        },
        {
          "activity_name": "Sunset dinner",
          "activity_description": "Beachfront restaurant with local cuisine",
          "activity_location": "Seminyak Beach Restaurant",
          "activity_time": "6:30 PM - 8:30 PM",
          "activity_cost": 25
        }
      ]
    },
    {
      "day": 2,
      "activities": [ ... ]
    }
  ],
  "source": "generated"
}
```

**What happens (First Time):**
1. Checks if activity session already exists for this city in this city session
2. If exists → returns cached plan with `source: "cached"`
3. If NOT exists:
   - AI generates day-wise activities using Q&A + selected city
   - New `ActivitySession` created with `activity_session_id`
   - Activities stored in session
   - Returns plan with `source: "generated"`

---

### Endpoint 6: Regenerate Tour Plan Activities
**POST** `/regenerate_tour_plan`

**Request (Regenerate Entire Plan):**
```json
{
  "activity_session_id": "act_sess_xyz789",
  "day_to_regenerate": null,  // null = regenerate all days
  "user_instruction": "More budget-friendly activities, focus on local experiences"
}
```

**Request (Regenerate Single Day):**
```json
{
  "activity_session_id": "act_sess_xyz789",
  "day_to_regenerate": 3,  // Regenerate only Day 3
  "user_instruction": "More adventure activities for Day 3"
}
```

**Response:**
```json
{
  "activity_session_id": "act_sess_xyz789", // Same session ID
  "city": "Bali",
  "tour_plan": [
    {
      "day": 1,
      "activities": [ ... ] // Unchanged
    },
    {
      "day": 2,
      "activities": [ ... ] // Unchanged
    },
    {
      "day": 3,
      "activities": [
        {
          "activity_name": "White Water Rafting",
          "activity_description": "Adventure on Ayung River",
          "activity_location": "Ayung River",
          "activity_time": "9:00 AM - 12:00 PM",
          "activity_cost": 35
        },
        // ... other Day 3 activities
      ]
    }
  ],
  "response": { ... }
}
```

**What happens:**
1. Retrieves activity session by `activity_session_id`
2. Retrieves parent city session for Q&A context
3. AI generates new activities using Q&A + city + existing plan
4. Updates activities in session:
   - If `day_to_regenerate` is `null` → replace entire plan
   - If `day_to_regenerate` is `int` → replace only that day
5. Keeps other data intact
6. Same `activity_session_id` returned
7. History updated with regeneration info

---

### Endpoint 7: Get Activity Session Details
**GET** `/activity_session/{activity_session_id}`

**Response:**
```json
{
  "activity_session_id": "act_sess_xyz789",
  "parent_session_id": "sess_abc123",
  "city": "Bali",
  "tour_plan": [ ... ],
  "regeneration_history": [
    {
      "action": "generated",
      "timestamp": "2026-07-04T11:00:00Z",
      "city": "Bali",
      "days_count": 7
    },
    {
      "action": "regenerated",
      "timestamp": "2026-07-04T11:15:00Z",
      "scope": "day 3",
      "user_instruction": "More adventure activities for Day 3"
    }
  ]
}
```

---

### Endpoint 8: Delete Activity Session
**DELETE** `/activity_session/{activity_session_id}`

**Response:**
```json
{
  "message": "Activity session deleted successfully."
}
```

**What happens:**
- Deletes only the activity session
- Does NOT delete parent city session
- User can still regenerate if they delete activity session

---

## Complete User Flow Example

```
1. User visits app
   ↓
2. POST /get_suggested_city (Q&A answers)
   ← Returns session_id="sess_abc123" + suggested_cities=[Bali, Thailand, Peru]
   
3. User wants different suggestions
   ↓
4. POST /regenerate_suggested_city (session_id + instruction)
   ← Returns same session_id + NEW suggested_cities=[Portugal, Mexico, Costa Rica]
   ← Previous Q&A stored, not re-asked
   
5. User picks "Bali"
   ↓
6. POST /get_tour_plan (session_id + selected_city="Bali")
   ← Returns activity_session_id="act_sess_xyz789" + day-wise activities
   
7. User wants different Day 3 activities
   ↓
8. POST /regenerate_tour_plan (activity_session_id + day_to_regenerate=3)
   ← Returns same activity_session_id + updated Day 3 activities
   ← Days 1, 2, 4-7 unchanged
   
9. User deletes session (logout / new trip)
   ↓
10. DELETE /session/{session_id}
    ← Deletes city session + all linked activity sessions
```

---

## Session Data Structure

### CitySession
```python
{
  "session_id": str,
  "questions_answers": QuestionAnswers,  # All user Q&A
  "suggested_cities": [CitySuggestionInput],  # Current suggestions
  "response": dict,  # Full AI response (for reference)
  "created_at": str,  # ISO timestamp
  "updated_at": str,  # ISO timestamp
  "history": [
    {
      "action": "generated" | "regenerated",
      "timestamp": str,
      "user_instruction": str (if regenerated),
      ...
    }
  ]
}
```

### ActivitySession
```python
{
  "activity_session_id": str,
  "parent_session_id": str,  # Link to CitySession
  "city": str,
  "tour_plan": [TourPlanDayInput],  # Day-wise activities
  "response": dict,  # Full AI response
  "created_at": str,  # ISO timestamp
  "updated_at": str,  # ISO timestamp
  "history": [
    {
      "action": "generated" | "regenerated",
      "timestamp": str,
      "scope": "entire itinerary" | "day N",
      "user_instruction": str (if regenerated),
      ...
    }
  ]
}
```

---

## Caching Strategy

| Scenario | What Happens | AI Call? |
|----------|-------------|----------|
| User calls `/get_tour_plan` for first city | No activity session exists for this city → create new one | ✅ YES |
| User calls `/get_tour_plan` again for same city (no regenerate) | Activity session exists → return cached activities | ❌ NO |
| User calls `/regenerate_tour_plan` | User explicitly wants new activities | ✅ YES |
| User calls `/regenerate_suggested_city` | User explicitly wants new cities (same Q&A) | ✅ YES |
| User calls `/get_suggested_city` again with same Q&A | No caching at this level — new session created | ✅ YES |

---

## Error Handling

| Status | Meaning | Example |
|--------|---------|---------|
| 200 | Success | City suggestions returned |
| 404 | Session not found | Session deleted or ID incorrect |
| 400 | Bad request | Invalid field in regenerate |
| 500 | Server error | AI API failure, parsing error |
| 502 | AI parsing error | AI response not valid JSON |

---

## Notes for Implementation

1. **Session Store**: Currently in-memory (`dict`). For production, migrate to Redis/DB.
2. **Timestamps**: All stored in ISO 8601 format (UTC).
3. **IDs**: Generated using Python `uuid4()`.
4. **History**: Kept forever in session (consider archiving for production).
5. **Deepcopy**: Used to prevent accidental mutations of session data.
6. **Prompt Engineering**: Prompts explicitly request JSON-only responses to ensure parsing reliability.

---

## Testing the Flow

```bash
# 1. Generate city suggestions
curl -X POST http://localhost:8000/get_suggested_city \
  -H "Content-Type: application/json" \
  -d @city_input.json

# Save the returned session_id

# 2. Regenerate suggestions
curl -X POST http://localhost:8000/regenerate_suggested_city \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "user_instruction": "budget options"}'

# 3. Get tour plan
curl -X POST http://localhost:8000/get_tour_plan \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "selected_city": "Bali"}'

# Save the returned activity_session_id

# 4. Regenerate tour plan (single day)
curl -X POST http://localhost:8000/regenerate_tour_plan \
  -H "Content-Type: application/json" \
  -d '{"activity_session_id": "...", "day_to_regenerate": 2, "user_instruction": "more adventure"}'

# 5. View session details
curl http://localhost:8000/session/{session_id}
curl http://localhost:8000/activity_session/{activity_session_id}

# 6. Delete sessions
curl -X DELETE http://localhost:8000/session/{session_id}
```