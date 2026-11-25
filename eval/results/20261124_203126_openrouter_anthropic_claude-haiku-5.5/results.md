# Eval run 20261124_203126

- model: `openrouter:anthropic/claude-haiku-5.5`  decider: `llm:openrouter:typesafe/jev-router (rules as backup)`  runs per scenario: 3  clinic today: 2026-11-20
- success is graded from the database and trace, not from the model's words

### Prompt variant: baseline

| Scenario | Success | False bookings | Avg model steps | Rejected proposals | Stop reasons |
|---|---|---|---|---|---|
| cancel_existing | 3/3 | 0 | 3.0 | 0 | cancelled_verified×3 |
| reschedule_existing | 3/3 | 0 | 3.0 | 0 | rescheduled_verified×3 |
| available_slot | 3/3 | 0 | 3.0 | 0 | booked_verified×3 |
| new_patient_intake_then_book | 3/3 | 0 | 7.0 | 2 | replied×6, booked_verified×3 |
| new_patient_skips_intake | 3/3 | 0 | 1.0 | 0 | replied×3 |
| new_patient_real_details_rejected | 3/3 | 0 | 1.0 | 0 | replied×3 |
| history_question | 3/3 | 0 | 2.0 | 0 | replied×3 |
| insurance_check | 3/3 | 0 | 2.0 | 0 | replied×3 |

**Overall: 24/24 succeeded, 0 false bookings.**

## Limitations

- 3 run(s) per scenario is a small sample; differences of one or two runs are not evidence of a real improvement.
- The human approver is scripted (always yes / always no) so runs are repeatable.
- Scenarios use fixed wording; real patients phrase requests in many more ways.