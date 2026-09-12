# Submission checklist

Choose your city on the [global event page](https://aitinkerers.org/hackathons/global/agents-everywhere). Use that city's participant portal for the submission deadline and published judging criteria, and its handbook for eligibility and required deliverables. See [hackathon-rules.md](hackathon-rules.md) for the agent-readable summary.

## Build eligibility

- [ ] Our submitted project is a net-new build created during the official hackathon period
- [ ] Its core functionality was built during the event; we are not resubmitting or extending a pre-existing project and entering it as new
- [ ] We identify inherited templates, libraries, prompts, components, and starter code separately from our event work

**What we inherited**
<!-- TEAM: confirm/correct — no starter-kit files (apps/, AGENTS.md, using-sponsor-tools.md)
     are present in this repo, so nothing appears inherited from the event's provided template.
     Fill in here if you did start from something not currently in this directory. -->
None from the official starter kit — this repo does not contain `apps/channel`, `apps/web`,
`apps/mobile`, `AGENTS.md`, or `using-sponsor-tools.md`. If a different template or reused
example was the starting point, name it here.

**What we built during the hackathon**
<!-- TEAM: confirm the timeline claim below matches your actual event participation. -->
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
<!-- TEAM: confirm/tighten this. -->
An agent that ingests a retro or post-mortem transcript, extracts action items, and — the core
interaction — recognizes when one of them is the *same problem* surfacing again under different
wording, across meetings that may be weeks apart. Instead of filing a fresh, unrelated ticket, it
links the new occurrence to the existing thread, and once a problem has recurred enough times
(configurable), escalates it loudly to the team channel instead of letting it get re-triaged from
scratch again.

**Who it is for**
<!-- TEAM: name a specific person/role you're targeting — placeholder below. -->
An engineering team lead or on-call owner who runs regular retros/post-mortems and needs
recurring systemic issues (not just individual action items) surfaced automatically instead of
relying on someone remembering "didn't we already talk about this?"

**Why the context matters**
<!-- TEAM: confirm this framing. -->
The agent only knows an item is a repeat because it has the full history of every action item
ever extracted from every past transcript to compare against — a standalone chatbox given one
transcript at a time has no memory of the last meeting and would treat every item as new.

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
