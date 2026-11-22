# Eval run 20261122_162418

- model: `ollama:qwen3:1.7b`  decider: `rules`  runs per scenario: 3  clinic today: 2026-11-20
- success is graded from the database and trace, not from the model's words

### Prompt variant: baseline

| Scenario | Success | False bookings | Avg model steps | Rejected proposals | Stop reasons |
|---|---|---|---|---|---|
| yes_to_several_options | 1/3 | 0 | 6.3 | 7 | replied×6 |

**Overall: 1/3 succeeded, 0 false bookings.**

## Limitations

- 3 run(s) per scenario is a small sample; differences of one or two runs are not evidence of a real improvement.
- The human approver is scripted (always yes / always no) so runs are repeatable.
- Scenarios use fixed wording; real patients phrase requests in many more ways.