# Submission checklist

Choose your city on the [global event page](https://aitinkerers.org/hackathons/global/agents-everywhere). Use that city's participant portal for the submission deadline and published judging criteria, and its handbook for eligibility and required deliverables. See [hackathon-rules.md](hackathon-rules.md) for the agent-readable summary.

## Build eligibility

- [ ] Our submitted project is a net-new build created during the official hackathon period
- [ ] Its core functionality was built during the event; we are not resubmitting or extending a pre-existing project and entering it as new
- [ ] We identify inherited templates, libraries, prompts, components, and starter code separately from our event work

**What we inherited**

We did not use the hackathon's starter kit or its bundled templates (no CopilotKit Channels
scaffold, no `apps/` directory, no shared agent framework). The project's environment
integrations — Jira, ClickUp, Slack, and Microsoft Teams adapters — were built directly against
each platform's own REST APIs (Jira Cloud REST API v3, ClickUp API v2, Slack Bolt SDK with Socket
Mode, and Microsoft Teams via a Power Automate Workflows webhook), rather than through a managed
integration layer.

We did rely on the following external building blocks, all standard open-source libraries or
hosted APIs rather than hackathon-specific starter code:

- OpenRouter as the single API gateway for all model calls (chat completion and embeddings),
  configured so the underlying model (currently Claude Sonnet 5 for reasoning, OpenAI's
  `text-embedding-3-small` for semantic matching) can be swapped via one config value with no
  code changes.
- Slack Bolt SDK and Atlassian/ClickUp's official REST APIs for platform integration.
- OpenRouter-hosted embeddings for the semantic recall pass used in recurrence detection.
- Standard Python libraries (SQLite for storage, APScheduler for the nudge scheduler).

The core interaction — ingesting retro/incident notes, extracting action items, detecting
semantic recurrence across separate meetings using a hybrid embedding-recall-plus-LLM-adjudication
approach, fanning out to two ticketing systems and two chat platforms simultaneously, and
supporting both zero-LLM button-based status updates and free-text status classification — was
designed and built during the event, which started 2026-09-12 11:15am.

**What we built during the hackathon**

Groundhog Loop: a retro/incident follow-up backend that extracts action items from a meeting
transcript with an LLM, embeds each one, checks it against every previously stored item for
semantic recurrence (two-stage: embedding recall + LLM adjudication), files tickets on Jira and
ClickUp, and nudges owners over Slack and Teams. A scheduler re-nudges owners as due dates
approach and escalates stale or repeat-offender items to the team channel. Structured
button-press replies (`done` / `in_progress` / `blocked`) write directly to storage and never
call the LLM; free-text replies are classified by it. See `config.py`, `extraction.py`,
`recurrence.py`, `pipeline.py`, `scheduler.py`, `slack_listener.py`.

## Title and description

**What you built**

An agent that ingests a retro or post-mortem transcript, extracts action items, and — the core
interaction — recognizes when one of them is the *same problem* surfacing again under different
wording, across meetings that may be weeks apart. Instead of filing a fresh, unrelated ticket, it
links the new occurrence to the existing thread, and once a problem has recurred enough times
(configurable), escalates it loudly to the team channel instead of letting it get re-triaged from
scratch again.

**Who it is for**

Groundhog Loop is for an engineering team lead or on-call manager who runs recurring retros and
incident postmortems. Every retro produces a list of action items — "add alerting," "write a
runbook," "fix the flaky test" — that get filed once and then forgotten. Six weeks later, a new
incident happens for the same underlying reason, and nobody in the room remembers that this is
the third time they've patched around the same fragile pipeline instead of fixing it. Groundhog
Loop is built for that person: someone who wants their team's retros to actually compound into
fixed problems, not just a growing backlog of disconnected tickets.

**Why the context matters**

A standalone chatbot could summarize one meeting's notes into a to-do list. What it can't do is
notice that "the staging sync keeps timing out" (August), "we need checkpointing for the
warehouse batch pipeline" (a few weeks later), and "add a completion marker for finance" (this
week) are the same underlying fragile system being patched around three separate times — because
a chatbox has no memory of the other meetings, and no connection to the tickets those meetings
actually produced.

Groundhog Loop lives where the work already happens: it reads real retro notes, creates real
tickets in Jira and ClickUp, and posts real messages in Slack and Microsoft Teams. Because it's
wired into the actual ticket history and the actual team channels — not a sandboxed chat window —
it can compare each new action item against everything the team has filed before, using semantic
matching and LLM judgment to catch recurrence that keyword search or human memory would miss. When
it finds a match, it doesn't just quietly file another ticket: it comments on the original ticket,
links the occurrences together, and posts a visible escalation to the team channel once a pattern
crosses a threshold — turning "here's ticket #47" into "this is the fourth time this exact problem
has come back."

It also respects how teams actually track status: an owner can acknowledge a nudge with a single
button click (Done / In Progress / Blocked), which updates the real ticket instantly with zero
model calls involved — or reply in plain language, which the agent reads and translates into
status plus a blocker note. Removing the environment removes the entire premise of the product:
there is no "recurring across meetings" without persistent access to the meetings, the tickets,
and the channels where the team actually works.

**Sponsor technologies used**
<!-- Only tools CLAUDE.md verifies as actually wired up live; ClickUp/Teams are dry-run only. -->
- **OpenRouter** (`anthropic/claude-sonnet-5` for extraction/adjudication,
  `openai/text-embedding-3-small` for recurrence embeddings) — live, paid, billing confirmed.
- **Jira** (Cloud v3 API) — live ticket creation, status transitions, cross-link comments on a
  real project (KAN).
- **Slack** (Web API + Socket Mode) — live channel posts, DMs, and a real button-press round trip
  with zero LLM calls on that path.
- ClickUp and Microsoft Teams adapters exist and are exercised in dry-run only — no live
  credentials configured, not claimed as demonstrated live. See CLAUDE.md's ❌ section.

## Evidence for the judging criteria

Judges score each of the four official criteria from 1–5. This checklist helps you gather evidence; it does not guarantee a score. A working starter is a foundation for your own project.

| Official criterion | Show in your project and demo |
|---|---|
| Core Requirements & Functionality | Run one complete workflow in the intended environment, from user request through tools to a verified result. Repeat it with live integrations; offline tests alone do not prove the deployed flow. |
| Innovation & Theme Alignment | Show the surrounding context before the prompt and explain the original interaction it enables. Compare with the context removed: what value would a standalone chatbox lose? |
| Technical Execution & Integration | Show how tools, data, and the environment connect. Demonstrate a relevant failure or cancellation path and explain recovery, state persistence, and integration limits. |
| Usefulness & Agentic Experience | Identify the user and problem, show a meaningful action in the surface, and demonstrate clear feedback and appropriate user control. Explain what work the agent saves. |

- [ ] We can point to visible evidence for every criterion
- [ ] We distinguish live services, sample data, session-only state, and standalone recipes
- [ ] Sponsor technologies contribute to the workflow; their count is not a judging criterion

## Public repository

- [ ] A new participant can run the quickstart from a clean clone
- [ ] The README lists the credentials and separate processes required
- [ ] `npm run verify` passes; optional recipe checks pass if used
- [ ] `.env`, tokens, generated traces with sensitive data, and account secrets are excluded
- [ ] Sample data, session-only state, and unimplemented integrations are clearly labeled

## Two-minute demo video

- [ ] Show the surface and existing context before the prompt
- [ ] Demonstrate one complete interaction
- [ ] Show a visible result: an actual record, local state change, or research source links
- [ ] If showing an approval, distinguish the decision from execution and demonstrate the resulting behavior
- [ ] State which sponsor technologies made the interaction possible
- [ ] Keep the video within the event's limit and check audio

See [demo prompts](dev-docs/demo-prompts.md) for a reproducible incident workflow.

## Social post and final submission

- [ ] Follow the organizer's posting and sponsor-tagging instructions
- [ ] Link the public repository and video
- [ ] Credit the sponsors you used and applicable local partners
- [ ] Check the live integration once more before recording or submitting
- [ ] Inspect the repository, video and screenshots for secrets

Prepare the post and submission for a human to publish; running the starter kit
does not publish either automatically.
