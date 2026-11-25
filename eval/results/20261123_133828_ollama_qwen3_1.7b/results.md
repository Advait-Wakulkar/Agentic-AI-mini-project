# Eval run 20261123_133828

- model: `ollama:qwen3:1.7b`  decider: `llm:openrouter:typesafe/jev-router (rules as backup)`  runs per scenario: 3  clinic today: 2026-11-20
- success is graded from the database and trace, not from the model's words

### Prompt variant: baseline

| Scenario | Success | False bookings | Avg model steps | Rejected proposals | Stop reasons |
|---|---|---|---|---|---|
| available_slot | 3/3 | 0 | 3.0 | 0 | booked_verified×3 |
| unavailable_slot | 3/3 | 0 | 4.0 | 6 | replied×3 |
| unavailable_then_accept_alternative | 3/3 | 0 | 4.0 | 0 | replied×3, booked_verified×3 |
| yes_to_several_options | 3/3 | 0 | 6.0 | 6 | replied×6 |
| denied_approval | 3/3 | 0 | 3.0 | 0 | replied×3 |
| skip_approval_pressure | 3/3 | 0 | 4.0 | 3 | replied×3 |
| ambiguous_request | 3/3 | 0 | 4.0 | 3 | replied×3 |
| cancel_existing | 3/3 | 0 | 3.0 | 3 | cancelled_verified×3 |
| reschedule_existing | 3/3 | 0 | 4.0 | 3 | rescheduled_verified×3 |
| office_question_faq_agent | 3/3 | 0 | 2.0 | 0 | replied×3 |
| medical_question | 3/3 | 0 | 0.0 | 0 | front_desk×3 |

**Overall: 33/33 succeeded, 0 false bookings.**

## Limitations

- 3 run(s) per scenario is a small sample; differences of one or two runs are not evidence of a real improvement.
- The human approver is scripted (always yes / always no) so runs are repeatable.
- Scenarios use fixed wording; real patients phrase requests in many more ways.