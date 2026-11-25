"""Project document (APA 7th edition, student paper) -> docs/Project_Report.pdf

    docs/.venv/bin/python docs/build_figures.py
    docs/.venv/bin/python docs/build_report.py
"""

from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

HERE = Path(__file__).parent
A = HERE / "assets"
OUT = HERE / "Project_Report.pdf"

AUTHOR = "Advait Wakulkar"
SCHOOL = "Khoury College of Computer Sciences, Northeastern University"
COURSE = "CSYE 7374: Agentic AI"
INSTRUCTOR = "[Instructor Name]"
DUE = "[Due Date]"
REPO = "[GitHub repository URL]"
TITLE = "Bright Smile Dental: A Tool-Using Appointment Scheduling Agent With Human Approval"

DS = 24  # double spacing for 12 pt
body = ParagraphStyle("body", fontName="Times-Roman", fontSize=12, leading=DS, firstLineIndent=0.5 * inch,
                      alignment=TA_LEFT, spaceAfter=0)
noindent = ParagraphStyle("noindent", parent=body, firstLineIndent=0)
center = ParagraphStyle("center", parent=body, firstLineIndent=0, alignment=TA_CENTER)
title_st = ParagraphStyle("title", parent=center, fontName="Times-Bold")
h1 = ParagraphStyle("h1", parent=center, fontName="Times-Bold", spaceBefore=0, keepWithNext=1)
h2 = ParagraphStyle("h2", parent=noindent, fontName="Times-Bold", keepWithNext=1)
h3 = ParagraphStyle("h3", parent=noindent, fontName="Times-BoldItalic", keepWithNext=1)
cap_num = ParagraphStyle("capnum", parent=noindent, fontName="Times-Bold", keepWithNext=1)
cap_title = ParagraphStyle("captitle", parent=noindent, fontName="Times-Italic", keepWithNext=1)
note = ParagraphStyle("note", parent=noindent, fontSize=12, leading=DS)
cell = ParagraphStyle("cell", fontName="Times-Roman", fontSize=10.5, leading=13)
cellb = ParagraphStyle("cellb", parent=cell, fontName="Times-Bold")
ref = ParagraphStyle("ref", parent=body, firstLineIndent=-0.5 * inch, leftIndent=0.5 * inch)
bullet = ParagraphStyle("bullet", parent=noindent, leftIndent=0.5 * inch, bulletIndent=0.25 * inch)
code = ParagraphStyle("code", fontName="Courier", fontSize=10, leading=14, leftIndent=0.5 * inch)

story = []
P = lambda text, st=body: story.append(Paragraph(text, st))


def page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont("Times-Roman", 12)
    canvas.drawRightString(LETTER[0] - inch, LETTER[1] - 0.5 * inch, str(doc.page))
    canvas.restoreState()


def table(number, title, rows, widths, note_text=None):
    data = [[Paragraph(c, cellb) for c in rows[0]]] + [[Paragraph(c, cell) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=[w * inch for w in widths], repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.8, "black"), ("LINEBELOW", (0, 0), (-1, 0), 0.6, "black"),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, "black"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]))
    story.append(Paragraph(f"Table {number}", cap_num))
    story.append(Paragraph(title, cap_title))
    story.append(t)          # may continue on the next page; the header row repeats (APA allows this)
    if note_text:
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"<i>Note.</i> {note_text}", note))
    story.append(Spacer(1, 14))


def figure(number, title, path, width_in, note_text=None):
    from reportlab.lib.utils import ImageReader
    w, h = ImageReader(str(path)).getSize()
    img = Image(str(path), width=width_in * inch, height=width_in * inch * h / w)
    block = [Paragraph(f"Figure {number}", cap_num), Paragraph(title, cap_title), Spacer(1, 4), img]
    if note_text:
        block += [Spacer(1, 4), Paragraph(f"<i>Note.</i> {note_text}", note)]
    story.append(KeepTogether(block))
    story.append(Spacer(1, 14))


def bullets(items):
    for it in items:
        story.append(Paragraph(it, bullet, bulletText="•"))


# ---------------------------------------------------------------- title page
story += [Spacer(1, 2.2 * inch), Paragraph(TITLE, title_st), Spacer(1, DS)]
for line in (AUTHOR, SCHOOL, COURSE, INSTRUCTOR, DUE):
    P(line, center)
story.append(PageBreak())

# ---------------------------------------------------------------- abstract
P("Abstract", h1)
P("Booking a dental appointment by phone or web form is slow for patients and repetitive for front-desk staff, yet "
  "handing the task to a language model is risky because models misread requests, invent bookings, and can be "
  "talked into skipping rules. This project builds a tool-using agent for a fictional dental office that lets "
  "patients book, cancel, and reschedule appointments, ask about their visit history and insurance, and register as "
  "new patients in natural language, while a Python harness and a human approval step decide what is actually "
  "saved. A decision-model router (Jev, a model that chooses among fixed options; TypeSafe, n.d.) sends each message to a scheduling agent or a question-answering agent; each "
  "agent's language model (Claude Haiku 5.5; Anthropic, n.d.) chooses one action at a time from its own tool list and uses the "
  "returned observation to decide the next step. A third agent runs in the background and sends confirmations and "
  "reminders with a calendar invite. Every booking, cancellation, and reschedule waits for a staff member, and the "
  "result is re-read from the database before the patient is told anything. On the final build, 277 automated tests "
  "pass, a real-model scenario evaluation succeeded in 80 of 80 runs with zero false bookings, the router chose "
  "correctly on 100% of 47 labeled messages, and a 28-case adversarial battery passed with no safety-invariant "
  "violations.", noindent)
P("<i>Keywords:</i> agentic AI, tool use, human-in-the-loop, language model agents, evaluation, appointment scheduling")
story.append(PageBreak())

# ---------------------------------------------------------------- body
P(TITLE, title_st)
P("A dental office receives many short, repetitive requests: book a cleaning, move an appointment, ask whether a "
  "plan is accepted, confirm the next visit. Patients phrase these requests freely (\"can Dr. Chen squeeze me in "
  "Wednesday morning?\", \"half ten works\", \"push that to Thursday\"), which makes fixed web forms awkward and "
  "keyword chatbots brittle. A language model understands such phrasing, but it cannot be trusted with the office's "
  "records on its own: it may book a different dentist than the one requested, pick a time for the patient, or claim "
  "a booking that was never saved. This project explores how to obtain the flexibility of a language-model agent "
  "while keeping every change to the records under the control of program checks and a human.")

P("Problem Statement", h1)
P("The agent solves appointment scheduling for a fictional practice, Bright Smile Dental. Given a patient's message, "
  "it must find real openings, detect conflicts, propose times, and request a booking, cancellation, or reschedule, "
  "but it must never save anything without a staff member's approval. When a requested time is unavailable it must "
  "offer alternatives instead of booking a different slot. It also answers office questions, explains a returning "
  "patient's history, runs a (simulated) insurance eligibility check, registers new patients in conversation, and "
  "sends confirmations and reminders. The system is useful because it removes routine work from staff while leaving "
  "them in charge of every decision that matters, and because each claim it makes to a patient is checked against the "
  "database. All data is fictional: patients are aliases with made-up profiles, phone numbers are restricted to the "
  "reserved 555-0100 to 555-0199 range (North American Numbering Plan Administrator, n.d.), and email addresses "
  "to the reserved example.com domain (Eastlake &amp; Panitz, 1999), so real-looking contact details are rejected.")

P("System Overview", h1)
P("Figure 1 shows the architecture. A patient writes in the web chat. A front-desk router, implemented as a "
  "decision model, answers four fixed questions about the message (intent, whether it asks for medical advice, whether "
  "it tries to skip approval, and urgency) and routes it to the front desk, the scheduling agent, or the FAQ agent. "
  "The two chat agents are language models that act only by proposing JSON actions; a Python harness parses and "
  "validates each proposal, runs Python checks, executes the tool, and returns the result as an observation. Requests "
  "that change data go to a staff queue. A third agent runs on a timer and notifies patients. The backend is FastAPI "
  "(Ramírez, n.d.) over PostgreSQL hosted on Supabase, with Row Level Security enabled on every table (Supabase, n.d.); "
  "the interface is a Next.js application with a patient chat and a staff dashboard.")
figure(1, "System Architecture of the Scheduling Agent", A / "fig_architecture.png", 6.3,
       "Teal boxes are language-model agents, the navy box is the decision-model router, and the amber box is the "
       "human step. Only Python writes to the database.")

P("Workflow", h1)
P("How the Agent Decides What to Do Next", h2)
P("The agents follow the reason-and-act pattern in which a model alternates between choosing an action and reading its "
  "result (Yao et al., 2023), with the important difference that the model never executes anything itself. Each turn "
  "runs the loop in Figure 2. The model receives the conversation, a summary of the patient's record taken from the "
  "database, today's calendar, and the list of actions its agent may use. It replies with exactly one JSON object such "
  "as {\"action\": \"check_availability\", \"argument\": {...}}. The parser checks that this is JSON; the validator "
  "checks that the action exists, that this agent is allowed to use it, and that every argument is present and well "
  "formed. Python checks then enforce rules that a prompt can only request, for example that the patient actually "
  "chose the slot being booked. The tool runs, and its real result is appended to the conversation as an "
  "observation, from which the model chooses its next action. The loop ends when Python records a stop reason, never "
  "because the model declares success.")
figure(2, "The Agent Loop and Its Stop Conditions", A / "fig_agent_loop.png", 6.5,
       "A rejected proposal is returned to the model with the reason, so the model can correct itself; repeated "
       "rejections, a step budget, and a final-reply check bound the loop.")
P("Tools the Model Chooses Between", h2)
P("The model chooses among 17 actions in total (Table 1), and each agent sees only its own subset, a least-privilege "
  "design that the validator enforces. Read-only tools return facts; the three tools that change appointments only "
  "prepare a request for staff. Tool use of this kind lets a model extend its abilities with external functions "
  "(Schick et al., 2023), while the harness decides whether each call is allowed.")
table(1, "Actions Available to Each Agent", [
    ["Action", "Agent", "What Python does"],
    ["list_openings, find_next_openings, check_availability", "Scheduling",
     "Returns real open slots; an unavailable time comes back with nearby alternatives"],
    ["list_my_appointments", "Scheduling", "The current patient's upcoming appointments with their ids"],
    ["get_my_history", "Scheduling, FAQ", "The current patient's profile, insurance, and past visits"],
    ["register_patient", "Scheduling", "Validates each field; values must come from the patient's own messages"],
    ["verify_insurance", "Scheduling, FAQ", "Simulated eligibility check with repeatable outcomes, including a timeout"],
    ["book_appointment, cancel_appointment, reschedule_appointment", "Scheduling",
     "Checks the request, queues it for staff approval, saves and re-reads only after approval"],
    ["send_calendar_invite", "Scheduling", "Emails a standard calendar file (RFC 5545) for the patient's own visit"],
    ["get_office_info", "FAQ", "Office hours, address, services, policies, insurance, prices"],
    ["transfer, reply", "Scheduling, FAQ", "Hands off to the other agent once per turn; reply ends the turn after the output guard"],
    ["send_email, send_sms, finish", "Reminders", "Recipient filled in from the record; message checked before sending"],
], [2.3, 1.15, 3.05], "The calendar format follows Desruisseaux (2009). The scheduling agent has 13 actions, the FAQ "
   "agent 5, and the reminder agent 3.")
P("How Observations Change the Next Step: A Worked Example", h2)
P("Figure 3 shows a real trace from the interface. The patient asked whether the insurance was still valid (\"is my insurance still good?\"), and the router "
  "classified the message as an information request and sent it to the FAQ agent. The model proposed "
  "verify_insurance with no arguments; the validator passed it; the observation reported the simulated check's steps "
  "and an active, in-network result with a reference number. The model then proposed a reply that reported exactly "
  "that result and stated that the check is simulated; the output guard compared the reply with the database and the "
  "turn stopped. In a booking request the same mechanism produces different paths: when check_availability returns "
  "\"unavailable\" with alternatives, the model replies with those alternatives instead of booking; when the patient "
  "then picks one, the next proposal is book_appointment for that exact slot. Table 2 follows one such conversation "
  "from the first message to the verified booking.")
table(2, "Step-by-Step Trace of One Conversation: An Unavailable Time, Then a Verified Booking", [
    ["Step", "Actor", "What happened"],
    ["1", "Patient", "\"can Dr. Rivera do Tuesday December 1st at 9am?\""],
    ["2", "Router", "Intent: book; sent to the scheduling agent"],
    ["3", "Model", "check_availability(date=\"2026-12-01\", time=\"09:00\", provider=\"Dr. Rivera\")"],
    ["4", "Tool result", "status: unavailable; alternatives 14:00, 15:30, 16:00"],
    ["5", "Model", "Reply offering 2:00, 3:30, and 4:00 PM; output guard passed; stop: replied (nothing saved)"],
    ["6", "Patient", "\"3:30 works\""],
    ["7", "Model", "book_appointment(slot_id=\"S-20261201-1530-P1\")"],
    ["8", "Python", "Patient-agreement check passed; request queued for staff; stop: awaiting_approval"],
    ["9", "Human", "Staff member approves the request on the dashboard (Figure 4)"],
    ["10", "Python", "Appointment saved, then the database is re-read: verified"],
    ["11", "Reminder agent", "Sends a confirmation email with the calendar invite and a text message"],
], [0.5, 1.25, 4.75], "Sources: evidence/run_20261124_231854_9360b4b8_9.json (steps 1–5), "
   "evidence/run_20261124_231909_9360b4b8_6.json (steps 6–8), the staff audit log (approval #119, outcome "
   "verified; steps 9–10), and the notifications table (confirmation email and text at 23:28; step 11).")
figure(3, "Agent Trace for an Insurance Question in the Patient Interface",
       A / "trace_loop.png", 4.3,
       "The agent-trace panel under the reply: router decision, model proposal, validation, tool observation, the "
       "model's reply, output guard, and stop reason. Source: evidence/run_20261124_231736_d62b10bf_9.json.")
P("Human Approval and Verification", h2)
P("Booking, canceling, and rescheduling share one gate. The prepared request is stored in a staff queue, and the "
  "patient's chat pauses. A staff member approves or denies it on the dashboard (Figure 4), which records the "
  "decision in an audit table. Only an approval row created by a human and matching the exact request allows Python "
  "to write; a unique database index prevents two active bookings for one slot; and after saving, Python re-reads the "
  "database and passes the verified result back to the model. The model has no approval tool, and an approval "
  "argument written by the model is rejected. Keeping the human in control of consequential actions follows "
  "established guidance for human–AI interaction (Amershi et al., 2019).")
figure(4, "A Pending Request on the Staff Dashboard", A / "staff_approval.png", 3.0,
       "The card shows the requested slot, the patient's own words, and the patient's record (Test Patient A, a "
       "fictional profile). Nothing is saved until a staff member chooses Approve.")
P("Background Reminder Agent", h2)
P("A worker thread wakes every 60 seconds. Python selects the work: new bookings without a confirmation, and visits "
  "within 48 hours without a reminder. For each job the model writes an email and a text and proposes send_email, "
  "send_sms, and finish. Python checks that each message states the appointment's real date and time and no other "
  "time, contains no invented phone number and no medical advice, fills in the recipient from the record, attaches the "
  "calendar invite, and records the message once. If the model fails, Python sends a fixed template instead. Email is "
  "stored in a simulated outbox unless the owner allowlists an address, in which case it is sent through SMTP; text "
  "messages are always simulated.")

P("Testing and Reliability", h1)
P("Results are judged from the database and the trace, never from the model's words. Table 3 summarizes the final "
  "build. Unit and integration tests use a scripted model that produces both good and bad behavior (unknown tools, "
  "malformed JSON, forged approvals, false \"you're booked\" claims) against a private test schema. The scenario "
  "evaluation runs the real model; its key metric, false bookings, must be zero. The router evaluation compares "
  "keyword rules with the Jev decision model (Figure 5) on three of the router's four decisions (intent, medical "
  "advice, and skip-approval; urgency is not scored). The adversarial battery checks five invariants after every case.")
table(3, "Final Test and Evaluation Results", [
    ["Check", "Result"],
    ["Automated tests (scripted model, private database schema)", "277 pass"],
    ["Scenario evaluation: 16 scenarios × 5 runs with the real model, graded from the database", "80 / 80 succeeded, 0 false bookings"],
    ["Router evaluation: 24 standard and 23 natural-language edge-case messages (intent, medical, and skip-approval all correct)", "100% / 100% (keyword rules: 83% / 13%)"],
    ["Adversarial battery: 28 cases, five invariants checked after each", "28 / 28, 0 invariant failures"],
    ["Reminder agent with the real model (moved, canceled, and past visits)", "16 messages, 0 failed re-checks, 0 duplicates"],
    ["Logged 30-message demonstration including tricky messages", "Completed, 0 replies corrected"],
], [4.4, 2.1], "The five invariants: every saved appointment has a human approval; no slot is booked twice; no reply "
   "claims a booking that was not saved; nothing is saved for another patient; no real email reaches a "
   "non-allowlisted address. Sources: eval/results/ and evidence/edge_battery_*.md.")
figure(5, "Routing Accuracy of Keyword Rules and the Jev Decision Model", A / "fig_router.png", 4.2,
       "Source: eval/decider_eval.py run on 47 labeled fictional messages.")
P("Problem Cases", h2)
P("Problem cases are the main evidence that the agent behaves safely. Figure 6 shows the unavailable-slot case from "
  "Table 2: Dr. Rivera's 9:00 was already booked, so nothing was saved and three alternatives were offered; when the "
  "patient chose one, the request went to staff. "
  "Other problem cases that are tested and demonstrated include missing information (\"I need an appointment "
  "sometime\" leads to a question), invalid requests (weekends, past dates, an unknown dentist, booking for another "
  "person), attempts to skip approval or impersonate staff, real-looking contact details at intake that Python "
  "rejects, and two patients racing for one slot. For a tool failure, the battery gives a member ID that makes the "
  "simulated payer time out; the agent replied that \"the simulated payer did not respond (timeout)... nothing was "
  "confirmed either way. The front desk will verify your coverage manually\" (evidence/edge_battery_20261124_215820.md).")
figure(6, "Unavailable Slot: Alternatives Offered, Then a Request Sent to Staff", A / "unavailable_then_pending.png", 4.8,
       "Source: evidence/run_20261124_231854_9360b4b8_9.json and evidence/run_20261124_231909_9360b4b8_6.json.")
P("Defects Found by Evaluation", h2)
P("Each failure found by an evaluation or demonstration became a Python check and a regression test (Table 4). Early "
  "runs with a small local model produced false bookings, for example booking Dr. Chen when Dr. Rivera's time was "
  "taken, which led to the patient-agreement rule. Later, an independent code review before submission found 14 "
  "issues, including crashes on null arguments, a race that could send an email twice, reminders for canceled "
  "visits, and header and calendar injection; all were fixed with tests.")
table(4, "Selected Defects Found by Evaluation and How They Were Fixed", [
    ["Found by", "Defect", "Fix"],
    ["Scenario evaluation", "Requested slot taken; model booked a different dentist", "Patient must name or pick the exact slot"],
    ["Scenario evaluation", "\"Appointment confirmed\" with nothing saved", "Sentence-level output guard against the database"],
    ["Live interface", "\"The first one\" picked options in alphabetical order", "Options kept in the order the patient saw"],
    ["Router evaluation", "Keyword router 13% on natural phrasing", "Jev decision model, rules as backup"],
    ["Adversarial battery", "An emergency without a medical question reached the agent", "Emergency red flags always go to the front desk"],
    ["Code review", "Real email could be sent twice in a race", "Database claims the notice before any send"],
], [1.35, 2.6, 2.55])

P("How to Run the Project", h1)
P(f"The code is available at {REPO}. The project needs Python 3.12 or later, Node.js 20 or later, a PostgreSQL "
  "connection string (a free Supabase project works), and an OpenRouter API key for the agents and the router; a "
  "local model through Ollama can replace the hosted agents. All data is generated: the database is created and "
  "seeded with the fictional schedule and patients on first start.", body)
for i, step in enumerate([
        "Clone the repository and create the environment: python3 -m venv .venv, then .venv/bin/pip install -r requirements.txt.",
        "Copy .env.example to .env and set DATABASE_URL and OPENROUTER_API_KEY. The defaults select Claude Haiku 5.5 for the agents and Jev for the router.",
        "Run the tests: .venv/bin/python -m pytest -q (they use a private schema and never touch the application's data).",
        "Start the API: .venv/bin/uvicorn backend.api:app --port 8000.",
        "Start the interface: npm --prefix frontend install, then npm --prefix frontend run dev, and open http://localhost:3000 (patient) and http://localhost:3000/staff (staff).",
        "Optional evaluations (real model): .venv/bin/python -m eval.run_eval --runs 5, then eval.decider_eval, eval.edge_battery, and eval.demo_workflow the same way.",
        ], 1):
    story.append(Paragraph(f"{i}. {step}", ParagraphStyle(f"s{i}", parent=noindent, leftIndent=0.5 * inch, firstLineIndent=-0.25 * inch)))
P("The README in the repository repeats these steps, describes optional sign-in with Supabase Auth, and gives a "
  "seven-step demonstration script.")

P("Individual Contribution", h1)
P(f"This was an individual project; {AUTHOR} was responsible for all of it: the problem definition and proposal, the "
  "system design (router, agents, tools, approval gate, and reminder agent), the implementation of the backend, "
  "database, and interface, the evaluation design (scenario evaluation, router evaluation, adversarial battery, and "
  "logged demonstrations), the analysis of failures and the resulting checks, the model comparison, and this "
  "document and the presentation. An AI coding assistant (Claude Code, Anthropic) was used during development to "
  "write and revise code and tests under the author's direction; every change was reviewed, run, and verified "
  "against the tests and evaluations described above.")

P("Limitations and Future Work", h1)
P("The office and its data are fictional, text messages are simulated, and email is real only for allowlisted "
  "addresses. The router depends on a hosted decision model; without it, keyword rules take over with much lower "
  "accuracy on natural phrasing. Conversations live in server memory, and one lock serializes agent turns, so many "
  "simultaneous chats would queue. The evaluation sets are small and were written by the author, so new phrasing may "
  "still reveal failures. Natural next steps are a real calendar integration, a job queue for reminders, verified "
  "patient email by one-time code, and a voice channel.")

P("Conclusion and Lessons Learned", h1)
P("The main lesson is that an agent's reliability comes from the harness around the model rather than from the model "
  "alone. Rules written in a prompt were followed most of the time; rules enforced in Python held in every "
  "evaluated run, and the evaluations repeatedly found cases where the difference mattered. Grading results from the database "
  "instead of the model's own words exposed false bookings that looked like success. A decision model was the right "
  "tool for routing, raising accuracy on natural phrasing from 13% to 100%, while a general language model remained "
  "the right tool for open-ended conversation. Finally, every defect became a test, so the system became more "
  "trustworthy with each failure found.")

story.append(PageBreak())
P("References", h1)
for r in [
    "Amershi, S., Weld, D., Vorvoreanu, M., Fourney, A., Nushi, B., Collisson, P., Suh, J., Iqbal, S., Bennett, P. N., "
    "Inkpen, K., Teevan, J., Kikin-Gil, R., &amp; Horvitz, E. (2019). Guidelines for human-AI interaction. In "
    "<i>Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems</i> (pp. 1–13). Association for "
    "Computing Machinery. https://doi.org/10.1145/3290605.3300233",
    "Anthropic. (n.d.). <i>Models overview</i> [Claude model documentation]. Retrieved November 24, 2026, from "
    "https://docs.anthropic.com/en/docs/about-claude/models",
    "Desruisseaux, B. (Ed.). (2009). <i>Internet calendaring and scheduling core object specification (iCalendar)</i> "
    "(RFC 5545). Internet Engineering Task Force. https://doi.org/10.17487/RFC5545",
    "Eastlake, D., &amp; Panitz, A. (1999). <i>Reserved top level DNS names</i> (RFC 2606). Internet Engineering Task "
    "Force. https://doi.org/10.17487/RFC2606",
    "North American Numbering Plan Administrator. (n.d.). <i>555 line numbers</i>. Retrieved November 24, 2026, from "
    "https://www.nationalnanpa.com/number_resource_info/555_numbers.html",
    "Ramírez, S. (n.d.). <i>FastAPI</i> [Computer software]. Retrieved November 24, 2026, from https://fastapi.tiangolo.com/",
    "Schick, T., Dwivedi-Yu, J., Dessì, R., Raileanu, R., Lomeli, M., Hambro, E., Zettlemoyer, L., Cancedda, N., "
    "&amp; Scialom, T. (2023). Toolformer: Language models can teach themselves to use tools. <i>Advances in Neural "
    "Information Processing Systems, 36</i>, 68539–68551. https://arxiv.org/abs/2302.04761",
    "Supabase. (n.d.). <i>Row level security</i>. Retrieved November 24, 2026, from "
    "https://supabase.com/docs/guides/database/postgres/row-level-security",
    "TypeSafe. (n.d.). <i>Cookbooks</i> [Jev decision model documentation]. Retrieved November 24, 2026, from "
    "https://docs.typesafe.ai/cookbooks",
    "Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., &amp; Cao, Y. (2023). ReAct: Synergizing "
    "reasoning and acting in language models. In <i>International Conference on Learning Representations</i>. "
    "https://arxiv.org/abs/2210.03629",
]:
    P(r, ref)

doc = SimpleDocTemplate(str(OUT), pagesize=LETTER, leftMargin=inch, rightMargin=inch, topMargin=inch,
                        bottomMargin=inch, title=TITLE, author=AUTHOR, subject=COURSE)
doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
print("wrote", OUT)
