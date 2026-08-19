# Frontend Developer Handoff

## Objective

Build the new 15-question experience against the versioned retreat-recommendation API. Do not submit the new questionnaire to the existing city-suggestion endpoint.

## Integration Rules

- Send stable enum codes, not the visible question labels.
- Keep the raw questionnaire state until the recommendation request succeeds.
- Use the API's canonical field names and schema version.
- Do not calculate or display a match score locally.
- Submit Q4 retreat structure separately from Q10 planning service.
- Select a recommendation by `property_id`, never by city or property display name.

## Question Requirements

### Q1: Archetype

Single select. Send one of:

- `burned_out_achiever`
- `transformer`
- `seeker`
- `optimizer`
- `escapist`
- `reconnector`

### Q2: Break from

Single select. Use stable codes such as:

- `noise_stimulation`
- `responsibility_decisions`
- `routine_repetition`
- `emotional_heaviness`

### Q3: Arrival priority

Single select:

- `breathtaking_view`
- `silence_privacy`
- `warmth_water_sunshine`
- `beautiful_design_service`

### Q4: Retreat structure

Single select:

- `almost_none`
- `optional_rituals`
- `one_daily_anchor`
- `full_program`

Do not reuse this answer for Q10.

### Q5: Reset style

Single select:

- `digital_disconnection`
- `sensory_indulgence`
- `creative_inspiration`
- `doing_nothing`

### Q6: Physical intensity

Single select:

- `gentle`
- `moderate`
- `challenging`

Do not call this field `energy_level`.

### Q7: Party

Single select for party type, followed by counts where applicable:

- `solo`
- `couple`
- `small_group`
- `family`

For Family collect adults and children. For Small Group collect party size if the product needs booking support.

### Q8: Spirituality

Single select:

- `none`
- `light`
- `moderate`
- `deep`

### Q9: Travel timing

Use either:

- `flexible`, with no months
- `specific`, with one or more month numbers

Prefer a month picker over labels such as summer or winter because seasons differ by hemisphere.

### Q10: Planning service

Single select:

- `loose`
- `well_planned`
- `hour_by_hour`

Explain in the UI that this changes planning support, not which retreat matches.

### Q11: Restrictions

Use an open text field with optional predefined chips. Submit both when available:

```json
{
  "text": "No hiking or water activities",
  "codes": ["no_hiking", "no_water_activities"]
}
```

Show that restrictions will be checked where property data allows it and that unverified restrictions may produce warnings.

### Q12: Setting

Prefer multi-select because retreats may fit multiple settings:

- `mountains`
- `ocean_beach`
- `jungle_rainforest`
- `desert`
- `countryside_farmland`
- `lake`
- `city_urban`

Send an empty array for No strong preference.

### Q13: Budget

Submit:

- Currency: `USD`
- Numeric per-person/per-night maximum
- `open_ended=true` when the user selects `$7,000+`

Because this is a nightly budget, do not silently change its meaning based on duration. Show an estimated trip total separately when enough information is available.

### Q14: Duration

Prefer wording based on nights:

- `1_3_nights`
- `4_7_nights`
- `1_2_weeks`
- `2_plus_weeks`

If the business must retain “days,” keep the bucket but ask for exact nights before calculating accommodation totals or creating an itinerary.

### Q15: Transform Focus

Multi-select with a strict maximum of three. Display friendly labels but submit canonical database values:

| Display label | Submitted value |
|---|---|
| Burnout recovery | `Burnout Recovery` |
| Longevity / healthy aging | `Longevity` |
| Detox | `Detox` |
| Weight loss | `Weight Loss` |
| Spiritual growth | `Spiritual Growth` |
| Emotional healing | `Emotional Healing` |
| Nervous system reset | `Nervous System Reset` |
| Fitness | `Fitness` |
| Creativity | `Creativity` |
| Relationship repair | `Relationship Repair` |
| Community / connection | `Community` |
| Better sleep | `Sleep` |
| Digital detox | `Digital Detox` |
| Cultural immersion | `Cultural Immersion` |

Disable additional choices after three are selected and show a clear validation message.

## Recommendation Results UI

Each property card should support:

- Property name and location
- Match score
- Two or three match reasons
- Warnings or trade-offs
- Restriction verification status
- Nightly price and whether it is a lower-bound estimate
- Budget tier
- Package or À la carte status
- Best season
- Settings

Do not hide warnings below a collapsed area when they relate to accessibility, restrictions or spirituality mismatch.

## Error and Loading States

- Preserve answers after API validation errors.
- Display field-specific validation messages.
- Distinguish “no compatible properties” from a network/server error.
- When no property satisfies every restriction, show the returned trade-offs instead of silently relaxing constraints.
- Do not show a recommendation until the API returns its stable `property_id`.

## Release Dependency

Do not enable the new questionnaire in production until:

- The versioned recommendation endpoint is available.
- Setting data has been added to all properties.
- Backend canonical enums are finalized.
- Family and restriction limitations have approved user-facing wording.
- Recommendation cards support warnings and lower-bound prices.

## Required Frontend Tests

- All visible choices serialize to the intended enum.
- Q4 and Q10 remain separate in the payload.
- Q15 cannot exceed three choices.
- No-preference Setting submits an empty array.
- Flexible timing submits no months.
- `$7,000+` submits `open_ended=true`.
- Open-text restrictions survive navigation and validation errors.
- Recommendation selection passes `property_id` to the next step.

## Definition of Done

- The submitted payload matches the versioned API contract exactly.
- All 15 questions preserve their intended meaning.
- Users see clear reasons, warnings and price limitations for each recommendation.
- The frontend never sends display labels as matching values.
