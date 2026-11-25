# Eval run 20261124_150736

- model: `openrouter:anthropic/claude-haiku-5.5`  decider: `llm:openrouter:typesafe/jev-router (rules as backup)`  runs per scenario: 5  clinic today: 2026-11-20
- success is graded from the database and trace, not from the model's words

### Prompt variant: baseline

| Scenario | Success | False bookings | Avg model steps | Rejected proposals | Stop reasons |
|---|---|---|---|---|---|
| history_question | 5/5 | 0 | 2.2 | 0 | replied×5 |

**Overall: 5/5 succeeded, 0 false bookings.**

## Limitations

- 5 run(s) per scenario is a small sample; differences of one or two runs are not evidence of a real improvement.
- The human approver is scripted (always yes / always no) so runs are repeatable.
- Scenarios use fixed wording; real patients phrase requests in many more ways.