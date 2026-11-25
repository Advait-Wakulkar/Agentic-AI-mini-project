| Decider | Intent acc | Skip-approval acc | Medical acc | Avg latency (s) | Avg change chance | Worst change chance | Unparsed |
|---|---|---|---|---|---|---|---|
| rules | 0.833 | 1.0 | 0.917 | 0.0 | 0.0 | 0.0 | 0 |
| llm:ollama:qwen3:1.7b | 0.75 | 0.167 | 1.0 | 0.48 | 0.0 | 0.0 | 0 |

24 labelled messages; self-consistency on the first 8 with 5 repeats.
Rules are deterministic, so their change chance is 0 by construction; the interesting comparison is Jev vs the LLM.