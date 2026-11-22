"""Terminal chat with the scheduling agent.

    python -m backend.cli                         # uses LLM_PROVIDER from .env
    python -m backend.cli --provider groq
    python -m backend.cli --provider ollama --model qwen3:1.7b --reset-db

Approval happens HERE, in the terminal, typed by a human. The model has no way
to answer this prompt.
"""

import argparse
import json

from backend import config, db, tools
from backend.agent import Session, run_turn
from backend.decision import get_decider
from backend.llm import LLMError, get_provider
from backend.prompts import VARIANTS

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "c": "\033[36m", "x": "\033[0m"}


def show_step(s: dict) -> None:
    k = s["kind"]
    if k == "DECIDER":
        d = s["decision"]
        print(f"{C['dim']}  DECIDER ({s['decider']}): intent={d['intent']} conf={d['confidence']} "
              f"skip_approval={d['skip_approval']:.2f} medical={d['medical_advice']:.2f} urgency={d['urgency']} "
              f"-> {d['route']['action']}{C['x']}")
    elif k == "MODEL_PROPOSED":
        p = s["proposal"]
        print(f"{C['c']}  MODEL PROPOSED: {json.dumps(p) if p else s['raw'][:200]}{C['x']}")
    elif k == "VALIDATION":
        print(f"  VALIDATION: {C['g']}passed{C['x']}" if s["passed"] else f"  VALIDATION: {C['r']}rejected {s['errors']}{C['x']}")
    elif k == "TOOL_OBSERVATION":
        print(f"{C['dim']}  TOOL OBSERVATION: {json.dumps(s['observation'])[:400]}{C['x']}")
    elif k == "HUMAN_DECISION":
        print(f"  HUMAN DECISION: {'approved' if s['approved'] else 'denied'} via {s['channel']} (approval #{s['approval_id']})")
    elif k == "VERIFY":
        col = C["g"] if s["verified"] else C["r"]
        print(f"{col}  VERIFY (re-read DB): saved={s['saved'].get('ok')} verified={s['verified']}{C['x']}")
    elif k == "OUTPUT_GUARD" and not s["passed"]:
        print(f"{C['y']}  OUTPUT GUARD: reply claimed a booking with no verified record -> {s.get('action', 'sent back to model')}{C['x']}")
    elif k == "LLM_ERROR":
        print(f"{C['r']}  LLM ERROR: {s['error']}{C['x']}")
    elif k == "STOP":
        print(f"{C['dim']}  STOP: {s['stop_reason']}{C['x']}")


def terminal_approver(request: dict) -> bool:
    print(f"\n{C['y']}{C['b']}  APPROVAL NEEDED (staff only){C['x']}")
    print(f"  Patient asked: \"{request.get('patient_request', '?')}\"")
    print(f"  Book {request['patient_alias']} on {request['date']} at {request['time']} with "
          f"{request['provider']} for '{request['reason']}' (slot {request['slot_id']})?")
    answer = input(f"  Approve? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def main() -> None:
    ap = argparse.ArgumentParser(description="Bright Smile Dental scheduling agent (fictional data only)")
    ap.add_argument("--provider", help="ollama | groq | claude (default: LLM_PROVIDER in .env)")
    ap.add_argument("--model", help="model name for the provider")
    ap.add_argument("--decider", help="rules | jev (default: DECIDER in .env)")
    ap.add_argument("--patient", default="Test Patient A", choices=db.PATIENTS)
    ap.add_argument("--variant", default="baseline", choices=VARIANTS, help="system prompt variant")
    ap.add_argument("--reset-db", action="store_true", help="re-seed the fictional schedule")
    ap.add_argument("--quiet", action="store_true", help="hide the agent trace")
    args = ap.parse_args()

    try:
        llm = get_provider(args.provider, args.model)
    except LLMError as e:
        raise SystemExit(f"Cannot start: {e}")
    decider = get_decider(args.decider)
    try:
        conn = db.init_db(config.DATABASE_URL, config.today(), reset=args.reset_db)
    except db.DatabaseError as e:
        raise SystemExit(f"Cannot start: {e}")
    session = Session(conn=conn, patient_alias=args.patient, prompt_variant=args.variant)

    print(f"{C['b']}Bright Smile Dental scheduling assistant{C['x']} (fictional office)")
    print(f"{C['dim']}model={llm.name} decider={decider.name} db={conn.label} patient={args.patient} today={session.today} "
          f"prompt={args.variant}\nCommands: /appointments  /quit{C['x']}\n")

    while True:
        try:
            text = input(f"{C['b']}You:{C['x']} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/appointments":
            for a in tools.list_appointments(conn, args.patient):
                print(f"  {a['date']} {a['time']} {a['provider']} - {a['reason']} (approval #{a['approval_id']})")
            continue

        # Trace prints live, so the human sees the model's reasoning steps BEFORE approving.
        turn = run_turn(session, text, llm, decider, terminal_approver, channel="cli",
                        on_step=None if args.quiet else show_step)
        print(f"{C['b']}Assistant:{C['x']} {turn.reply}")
        print(f"{C['dim']}  (run record: {turn.record_path}){C['x']}\n")


if __name__ == "__main__":
    main()
