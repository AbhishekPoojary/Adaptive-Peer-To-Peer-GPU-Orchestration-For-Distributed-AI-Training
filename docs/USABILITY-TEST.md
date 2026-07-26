# Unassisted usability test (M7)

This is a script for the project owner to run with a real person who did
**not** build this system — a classmate, a friend, anyone willing to sit
through ~20 minutes. It has not been run yet as of this milestone; the build
work in M7 was done to make it self-explanatory, but only a real human trial
can confirm that. Do not skip this step and do not fill in the table below
from imagination.

## Setup (you do this before they arrive)

1. Have the dashboard running and reachable (e.g. `http://localhost:5173` in
   dev, or wherever it's deployed) and the orchestrator healthy.
2. Have a spare machine (or a VM, or WSL2) available and reachable from the
   dashboard host, with Python 3.11+ and Docker installed, for Task 1 — you
   are not testing whether *they* can install Python/Docker, you're testing
   whether the dashboard's instructions get them to a working command.
3. Sit next to them with this document, a stopwatch, and nothing else open.
   Do not explain the product. Do not answer "what does this button do?" —
   write down the question instead and let them keep trying.
4. Record their screen if you can (with permission) — it's the fastest way to
   review hesitations afterward without relying on memory.

## Ground rules to tell them

> "I'm going to give you three things to do. I can't help or explain — just
> think out loud as you go, and if you get stuck, tell me what you'd try next
> rather than asking me. There are no wrong answers; if something is
> confusing, that's useful information, not a mistake on your part."

## The three tasks

Give these one at a time, in order. Don't reveal the next task until the
current one is done or they give up.

### Task 1 — Add a node

> "Get this spare machine added to the cluster as a worker."

Watch for:
- Do they find the "Add a node" entry point without prompting (Overview page
  vs. Nodes page — we put it in both; note which one they try first)?
- Do they understand the copy button / notice the command changed once
  copied?
- Do they actually run the command on the spare machine unprompted, or do
  they wait for the dashboard to "do something"?
- Do they notice the "connected" confirmation, or do they need to go looking
  for it?
- If Docker/Python is missing on the spare machine and the script exits with
  an error, do they read the error message and understand what to do next?

### Task 2 — Train a model

> "Start training a model on CIFAR-10 or MNIST."

Watch for:
- Do they find the Submit page without being told?
- Do they notice/understand the "N eligible nodes available" hint, and does
  it change their mind about world size?
- Do they notice the scheduler selector, or do they submit without ever
  looking at it? (If they never touch it, that's a finding either way —
  either it's appropriately out of the way for a first-time user, or it's
  invisible when it shouldn't be.)
- Do they know the job actually started (the toast, the redirect to job
  detail)?

### Task 3 — Find the final accuracy

> "Once that's done — or if it's still running — tell me the final test
> accuracy, or what you'd expect it to be if it hasn't finished."

Watch for:
- Do they find the metrics chart, the training-result stat tile, or the
  metrics table — and which do they reach for first?
- If the job is still running, do they understand it's still running (vs.
  thinking it's broken/stuck), and do they know where to check back?
- Do they ever open "Technical details," and if so, was it because they
  needed something in it or out of idle curiosity?

## What to record

For each task, note:

- **Time to completion** (or "gave up" + at what point).
- **Every hesitation**: a pause, a wrong click, scrolling back and forth, a
  "wait, what does this do" muttered out loud. Write down *where* on the
  screen, not just that it happened.
- **Every question they asked you** (even ones you didn't answer) — each one
  is a place the UI didn't explain itself.
- **Anything they said out loud** that reveals a wrong mental model (e.g.
  "so this button submits the job?" when it doesn't).

## Results table (fill in during/after the session)

| Task | Completed? | Time | Hesitations (where + what) | Questions asked | Notes |
|---|---|---|---|---|---|
| 1. Add a node | | | | | |
| 2. Train a model | | | | | |
| 3. Find final accuracy | | | | | |

## After the session

Ask three open questions, in this order, and write down the answers verbatim
before you editorialize:

1. "What was the most confusing part?"
2. "Was there anything you expected to happen that didn't?"
3. "If a friend asked you to explain this product in one sentence, what would
   you say?"

## Turning findings into work

Any hesitation longer than ~10 seconds, any question asked, and any wrong
mental model is a candidate fix. Don't fix everything reflexively — group
findings by the screen/flow they happened on, and prioritize the ones that
happened on Task 1 or 2 (a stuck first-time user never gets to see the rest
of the product) over polish items on screens they only glanced at.
