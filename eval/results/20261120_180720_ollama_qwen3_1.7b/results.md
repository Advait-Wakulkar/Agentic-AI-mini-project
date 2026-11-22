# Eval run 20261120_180720

- model: `ollama:qwen3:1.7b`  decider: `rules`  runs per scenario: 1  clinic today: 2026-11-20
- success is graded from the database and trace, not from the model's words

### Prompt variant: baseline

| Scenario | Success | False bookings | Avg model steps | Rejected proposals | Stop reasons |
|---|---|---|---|---|---|
| unavailable_slot | 0/1 | 1 | 4.0 | 0 | booked_verified×1 |

**Overall: 0/1 succeeded, 1 false bookings.**

## Limitations

- 1 run(s) per scenario is a small sample; differences of one or two runs are not evidence of a real improvement.
- The human approver is scripted (always yes / always no) so runs are repeatable.
- Scenarios use fixed wording; real patients phrase requests in many more ways.