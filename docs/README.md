# Project documents

| File | What it is |
|---|---|
| `Project_Report.pdf` | Project document, APA 7th edition (student paper): problem statement, workflow with diagrams, tools, testing, how to run, individual contribution, references |
| `Presentation.pptx` | Class presentation (9 slides, ~8–10 min), following the brief: 1 problem · 2 how the agent works (architecture, decisions, tools) · 3 demonstration (a success and problem cases, each as a real step-by-step trace: decision → tool call → tool result → outcome) and testing · 4 what I learned. Consulting-style action titles, sources and speaker notes on every slide |
| `assets/` | Figures (generated) and screenshots from live runs of the app |

Before submitting, fill in the placeholders at the top of `build_report.py` (`INSTRUCTOR`, `DUE`, `REPO`) and rebuild.

## Rebuild

```bash
python3 -m venv docs/.venv
docs/.venv/bin/pip install reportlab matplotlib pymupdf
docs/.venv/bin/python docs/build_figures.py     # diagrams and chart -> docs/assets/
docs/.venv/bin/python docs/build_report.py      # -> docs/Project_Report.pdf
cd docs && npm install && node build_deck.js    # -> docs/Presentation.pptx
```

`docs/.venv/` and `docs/node_modules/` are not committed.
