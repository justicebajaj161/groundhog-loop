# CLAUDE.md — project state

> **This file is the session handoff.** Claude Code loads it automatically at the start of
> every session, so it is the first thing any new session knows about this project.
> **Keep it true.** See "Maintaining this file" at the bottom — updating it is not optional.

**Last verified:** 2026-09-12 12:20 (paid models, live Jira + live Slack, **Socket Mode connected**) · **Status:** core complete and verified live end-to-end. Now on **paid** models throughout — `anthropic/claude-sonnet-5` for chat, `openai/text-embedding-3-small` (1536-d) for embeddings — after the free pool 429d mid-run and silently cost a recurrence link. Two real bugs were found and fixed while getting the grouping stable: **groups were never merged** (a linked item was orphaned out of its own thread) and **extraction truncated the recurrence evidence** to one sentence. Latest live run: **KAN-44 … KAN-52, group {3,4,7,8}, both escalations fired, zero 429s, 11/11 Slack deliveries.**

---

## What this is

A Python backend for **Groundhog Loop**, a retro/incident follow-up agent with recurrence detection.
It ingests a meeting transcript (retro / post-mortem), extracts action items with an LLM,
embeds each one, compares it against every item ever stored to catch **semantic recurrence**,
files tickets on Jira + ClickUp simultaneously, and nudges owners over Slack + Teams.

The point of the product: when the same problem is agreed on for the third time in different
words, say so loudly instead of filing a third unrelated ticket.

```
transcript ─▶ extract ─▶ embed ─▶ recall ─▶ LLM adjudicate ─▶ store
                                        │
                                        ├─▶ tickets  → Jira + ClickUp   (fan-out)
                                        └─▶ notify   → Slack + Teams    (fan-out)
                                                 │
                         button press ───────────┤ direct DB write, NO LLM
                         free text    ───────────┘ classified by the LLM
```

## Run it

```bash
.venv/bin/python demo.py --sample all --reset-db --fake-llm   # fully offline, no keys
.venv/bin/python demo.py --sample all --reset-db               # needs OPENROUTER_API_KEY
.venv/bin/python demo.py --list | --run-scheduler | --payloads
.venv/bin/python demo.py --button 3 done                       # no LLM
.venv/bin/python demo.py --reply 3 "blocked on vendor"         # LLM
.venv/bin/python slack_listener.py                             # Socket Mode receiver
```

**Use `.venv/bin/python`, not `python3`.** This machine's Python is PEP 668
externally-managed, so `pip install` into it is blocked. `.venv` was created with
`--system-site-packages` (inherits openai / numpy / requests / dotenv) plus
`slack-bolt` and `apscheduler` installed into it.

## File map

| File | Role |
|---|---|
| `config.py` | Every key, model name, threshold. **Nothing else reads `os.environ`.** |
| `llm_client.py` | The only module that calls a provider. Chat + embeddings + JSON parsing. |
| `extraction.py` | `extract_action_items`, `classify_reply`, `adjudicate_recurrence` |
| `recurrence.py` | `cosine_similarity`, `shortlist_candidates` (recall), `check_recurrence` (orchestrates both stages) |
| `storage.py` | SQLite `items` table, `Status` enum |
| `pipeline.py` | Orchestration shared by demo / scheduler / listener |
| `scheduler.py` | `run_n_cycle`, `handle_config_command` (`/groundhogloop`) |
| `slack_listener.py` | Socket Mode receiver: buttons, replies, slash command |
| `demo.py` | Traceable CLI + the `--fake-llm` offline stubs |
| `adapters/_http.py` | `AdapterBase`: dry-run gate, HTTP, `CALL_LOG` |
| `adapters/ticket_base.py`, `ticket_manager.py`, `jira_adapter.py`, `clickup_adapter.py` | `TicketAdapter` ABC, Jira, ClickUp, `TicketManager` |
| `adapters/notifier_base.py`, `notifier_manager.py`, `slack_adapter.py`, `teams_adapter.py` | `NotifierAdapter` ABC, Slack, Teams, `NotifierManager` |
| `env.example` | Credential/tuning template. **No leading dot** — copy it to `.env`. |
| `sample_transcripts/` | 3 transcripts; one issue recurs across all three |
| `.claude/hooks/claude_md_guard.py` | Keeps this file honest (see Maintaining this file) |
| `.claude/settings.json` | Wires that script to the SessionStart and Stop hooks |

Adding a platform = one new file + one line in that manager's `REGISTRY`. Nothing in
`pipeline.py`, `demo.py` or `scheduler.py` names a platform — keep it that way.

---

## ✅ Verified working (tested in-session, with evidence)

- **Full pipeline offline** — `demo.py --sample all --reset-db --fake-llm`: 9 items across
  3 transcripts, all four adapters fanned out.
- **Recurrence detection** — the ETL → staging-sync → warehouse-batch thread groups across
  all three transcripts, reaches occurrence #3, fires the channel escalation. The other
  7 items correctly stay distinct.
- **Button path makes ZERO LLM calls** (requirement 7). Proved by booby-trapping every
  `llm_client` entry point to raise, then running all three statuses successfully.
  `pipeline.apply_structured_reply` imports nothing from `extraction` — keep it that way.
- **Scheduler idempotence** — first run nudges 5 / escalates 3; second run skips all 8 via
  `last_nudged_at` / `last_escalated_at`.
- **Per-adapter failure isolation** — an exploding adapter errors alone; peers still deliver.
- **Provider swap with zero code changes** — `LLM_MODEL` / `EMBEDDING_MODEL` /
  `ENABLED_*_ADAPTERS` env overrides verified.
- **Embedding dimension guard** — vectors of a different length are skipped with a warning,
  not silently compared.
- **JSON extraction** — survives ```json fences, prose wrappers, braces inside strings,
  escaped quotes.
- **Jira/ClickUp/Slack/Teams dry-run payloads** — ADF structure, transition-id lookup,
  ClickUp epoch-ms dates, Slack Block Kit action_ids, Teams Adaptive Card all inspected.
- **OpenRouter endpoint shapes** — real requests with a dummy key returned **401, not 404**,
  on both `/chat/completions` and `/embeddings`. URLs and payloads are correct.
- **LIVE LLM CALLS WORK** (2026-09-12, 11:34). `.env` created with a real `OPENROUTER_API_KEY`.
  `demo.py --sample all --reset-db` completed end-to-end against `deepseek/deepseek-chat`
  and `liquid/lfm-2.5-embedding-350m:free`: 9 items extracted across the 3 transcripts
  (3/3/3 — same count the `--fake-llm` path produces), all four adapters fanned out dry-run.
- **Real extraction quality is good.** Items, owners and ISO due dates were all correct;
  `source_context` quotes are accurate. No `--fake-llm` canning involved.
- **Real embedding dimension is 1024** (not the fake path's 64). `--reset-db` is mandatory
  when switching between fake and real, or the dimension guard skips every stored vector.
- **JSON mode works live.** Extraction and `classify_reply` both ran through
  `chat_json` with `response_format={"type":"json_object"}` with no fallback warning.
  `--reply 4 "still waiting on the vendor..."` → `blocked` + a correct note.
- **JSON-mode fallback verified live in BOTH directions** (see the fix below):
  - real **401** (bogus key, deepseek): 3 attempts, `response_format` present in all 3,
    no fallback warning → structured output was NOT disabled by an auth failure.
  - real **400** (`inclusionai/ling-3.0-flash-vl:free`): `response_format` present on
    attempt 1, absent on attempt 2, warning fired, call recovered → `{'ok': True}`.
  - normal path (deepseek): exactly one call, JSON mode on, no fallback.
- **Button path still makes ZERO LLM calls** against real data — all four `llm_client`
  entry points booby-trapped to raise; `done` / `in_progress` / `blocked` all succeeded.
- **Scheduler idempotence holds on real data** — cycle 1 nudged 5 / escalated 3;
  cycle 2 nudged 0 / escalated 0 with all 8 skipped as recent.
- **HYBRID RECURRENCE WORKS END-TO-END (2026-09-12, 11:41).** `demo.py --sample all --reset-db`
  against the real model groups the intended thread and rejects everything else:

  | item | meeting | verdict |
  |---|---|---|
  | #3 make the nightly ETL failure alert someone | 01 Incident Retro | first occurrence |
  | #4 investigate why the staging sync keeps timing out | 02 Sprint Retro | **SAME PROBLEM (high)** → #2 |
  | #7 design doc for checkpointing the warehouse batch | 03 Postmortem | **SAME PROBLEM (high)** → #3, **escalation fired** |
  | #8 completion marker for the warehouse batch | 03 Postmortem | **SAME PROBLEM (high)** → #4 |
  | #1 regression test, #2 runbook/Grafana, #5 test-data seeding, #6 on-call handover doc, #9 warehouse on the on-call dashboard | — | all correctly **not the same** |

  5 of 5 unrelated items rejected, 0 false positives. Escalation fires at occurrence #3 as
  designed. Verdicts are quoted, not inferred — e.g. #4 linked with *"'Again? Didn't we talk
  about this before?', per 01 Incident Retro"*, #8 with *"It keeps coming back because we keep
  fixing the symptom. Last time it was the alerting, before that it was the timeout window."*
- **Recall cutoff must sit BELOW the weakest true pair.** At the first-guess 0.30 the run
  silently lost #3↔#4 (cosine **0.2885**) before the LLM ever saw it and produced the wrong
  group. 0.15 fixed it. The adjudicator cannot recover what recall discarded — when tuning,
  loosen recall and let precision be the LLM's job.
- **LIVE JIRA WORKS (2026-09-12, 11:47).** Real credentials for
  `https://adityabajaj20020214.atlassian.net`, project **KAN** ("TestAgentjira", team-managed).
  `demo.py --sample all --reset-db` created **9 real issues, KAN-2 … KAN-10**, one per extracted
  item, with the recurrence group cross-linked by comment. `/rest/api/3/myself` → 200.
- **`JIRA_STATUS_MAP` verified against the real workflow and CORRECTED.** Probed by creating a
  throwaway issue, reading `/transitions`, then deleting it. KAN offers exactly
  **To Do (11) / In Progress (21) / In Review (31) / Done (41)** and has **no "Blocked"** —
  the shipped default mapped `blocked -> "Blocked"`, which would have failed every time.
  Now `blocked -> "In Progress"`; the blocker text is still preserved as a Jira comment.
- **Button path mirrors to real Jira.** `apply_structured_reply` on KAN-2:
  `done -> Done`, `blocked -> In Progress`, `pending -> To Do`, each confirmed by re-reading
  the issue. Reverted to To Do afterwards.
- **LIVE SLACK WORKS — channel and DM.** Workspace TestingAgent, bot `followup`.
  Channel post to `#eng-retro` (`C0C15S17L11`) ok; DM via `SLACK_USER_MAP` ok. The full run
  delivered **9 DMs + 2 channel escalations, every one `ok=True`**.
- **`_sdk_call` dropped the entire live Slack response — fixed.** `slack_sdk`'s `SlackResponse`
  has no `.keys()`, so `dict(result) if hasattr(result, "keys")` fell through to
  `{"result": str(result)}` and lost `ts`, `channel`, everything. Dry-run returned a proper
  dict, live did not — breaking the one invariant this module exists to hold. Now unwraps
  `.data` first. Verified: `ts=1789178152.136429`, `channel=C0C15S17L11`.
- **SOCKET MODE CONNECTS (2026-09-12, 11:53) — first time ever.** `slack_listener.py` dialled out
  and held the websocket: `⚡️ Bolt app is running!`, `A new session has been established
  (session id: 0faaf12d-f40c-4f53-b134-d3d32d63f982)`, `Starting to receive messages`.
  Probed first with `apps.connections.open`, which issued a `wss://wss-primary.slack.com/...`
  ticket and reported app-token scope `connections:write`.
- **THE FULL BUTTON LOOP WORKS LIVE (2026-09-12, 11:58), AND REQUIREMENT 7 HOLDS IN IT.**
  A real `Done` click in Slack on the KAN-11 DM, received over Socket Mode by a listener whose
  `chat_json` / `embed` / `embed_batch` / `get_client` were all booby-trapped to raise:

  ```
  11:59:51 BUTTON RECEIVED: item #1 -> done | jira=KAN-11 | status before=pending
  11:59:53 [DRY-RUN] clickup PUT .../task/86a0001k  {"status": "complete"}
  11:59:53 BUTTON APPLIED : item #1 status after=done | llm calls so far=0
  11:59:53 slack_listener: item 1 -> done via button
  ```

  **Zero booby-trap violations.** Jira re-read from the API afterwards: `KAN-11 = Done`
  (was `To Do`), `updated 2026-09-12T11:59:53+0530` — the same instant as the log line, in
  the Jira account's timezone. Local DB status `done`. ClickUp correctly stayed dry-run.
  This is requirement 7 proven on the real path, not in a unit test: a button press changed
  a real ticket without the model being reachable at all.
- **Bolt builds without `SLACK_SIGNING_SECRET`.** Socket Mode does not need it;
  `slack_listener.build_app()` returns a configured app with the secret unset.
- **Second full live run**: `demo.py --sample all --reset-db` created **KAN-11 … KAN-19**
  (9 issues) and delivered 9 DMs + 1 channel escalation, every Slack call `ok=True`.
- **Retry backoff added to `llm_client` and verified.** `_backoff()` waits
  `LLM_RETRY_429_DELAY * 2**attempt` on a 429 and `LLM_RETRY_BASE_DELAY * 2**attempt`
  otherwise, and waits **zero** on the final attempt. Measured: 429/attempt-0 slept 5.0s,
  last attempt slept 0.0s. `embed_batch` — which previously had **no retry at all** — now
  retries on the same schedule; `llm_client.embed()` still returns 1024 dims afterwards.
- **Placeholder credentials no longer flip adapters live.** Caught in the act: a `.env` copied
  wholesale from `.env.example` left `SLACK_BOT_TOKEN=xoxb-...`, which `is_configured()` read as
  present, so Slack ran **live** and attempted 10 real `chat.postMessage` calls (all failed on
  auth, nothing delivered). `config._opt()` now treats `...`-suffixed, `<bracketed>`,
  `your-domain` and `example.com` values as absent and logs a warning. Re-verified: all four
  adapters back to dry-run.

- **PAID MODELS ARE LIVE AND BILLING (2026-09-12, 12:05).** `openai/text-embedding-3-small`
  returned **1536 dims** with `cost=1.4e-07`; `anthropic/claude-sonnet-5` returned clean JSON via
  provider **CoreWeave** with `cost=2.145e-05`. Both HTTP 200 with real cost fields, so credits are
  genuinely spendable — **do not trust `GET /api/v1/credits`**, which still reports
  `total_credits: 0`, `is_free_tier: true` and a `total_usage` that does not move after a paid run.
- **THE FULL TARGET RUN, LIVE (2026-09-12, 12:20).** `demo.py --sample all --reset-db` against real
  Jira + real Slack: **KAN-44 … KAN-52** created, group `rg-75a92b527f` = **{#3, #4, #7, #8}** at
  occurrence #4, **2 recurrence escalations** both `ok=True` to `#eng-retro`, **11/11 Slack
  deliveries ok**, and **zero** 429s / retries / adjudication failures. Read back from the Jira API:
  KAN-46 carries two cross-link comments, KAN-50 is titled `[Recurring x3]`.
  #5 (test-data seeding) and #9 (warehouse on-call dashboard) were both correctly rejected.
- **GROUP MERGING WAS BROKEN — fixed in `recurrence.py`.** `group_id` took only
  `best_record`'s group, so when a new item confirmed candidates belonging to *different* groups the
  others were silently discarded. Caught live: #4 was judged unrelated to #3, #7 then linked to #4
  and opened group A over {4,7}, then #8 linked to both #3 and #4 — its best match was the ungrouped
  #3, so a fresh group B was opened and {3,4} re-stamped into it, **leaving #7 alone in a dead group
  it had been correctly linked into** and the count reading 3 instead of 4. Now every group present
  among the confirmed candidates is merged. Proved by a deterministic offline test (no LLM):
  stored {#4,#7} in `rg-A` + ungrouped #3, new item confirms #3 and #4 →
  `group_id=rg-A`, `linked_ids=[3,4,7]`, `count=4`.
- **EXTRACTION WAS TRUNCATING THE EVIDENCE — fixed in `extraction.py`.** The prompt said copy
  "that sentence" (singular, max 200 chars), so the model kept one sentence and dropped its
  neighbours. The dropped clauses were the link: *"August retro, sprint 42, now this"* names the
  earlier meetings, and *"Last time it was the alerting, before that it was the timeout window"*
  names the earlier fixes. A live run that lost them produced **group {3,8} and zero escalations** —
  the adjudicator was right to refuse, it had no bridging text. Now: every such sentence, joined,
  and the code cap is **400** not 200. Two runs after the fix both grouped {3,4,7,8}.
- **`require_parameters` could hard-fail a model swap — fixed.** The provider preference added this
  session made OpenRouter 404 with `"No endpoints found that can handle the requested parameters"`
  (`failed_routing_step: Filter by Parameters`) for `openai/gpt-5-mini` and
  `anthropic/claude-sonnet-5`. A 404 is not a 400/422, so the JSON-mode fallback never saw it and
  the run died — breaking the verified "provider swap with zero code changes" property.
  `_is_routing_rejection` now drops the preference and retries. Verified: sonnet-5 went from
  hard failure to `{'ok': True}` with one warning.
- **Model choice measurably changes the answer — measured, not assumed.** Same transcripts, same
  prompts, only `LLM_MODEL` varied, grouping compared against {#3,#4,#7,#8}:
  `anthropic/claude-sonnet-5` **2/2** runs correct (~$0.10-0.15/run);
  `deepseek/deepseek-chat-v3.1` **1/2** (one run lost #3 entirely, ~$0.01/run);
  `openai/gpt-5-mini` over-grouped to 6 items and invented a 10th action item.
- **Recall bands RE-MEASURED for the new embedder, and they still overlap.** On
  `openai/text-embedding-3-small`: weakest TRUE cross-meeting pair **0.2425** (#4↔#8), strongest
  UNRELATED **0.4121** (#6↔#9). Second independent confirmation that **no single cutoff separates**
  the populations. 0.15 keeps ~0.09 of headroom under the weakest true pair.
  `RECURRENCE_MAX_CANDIDATES` raised 5 → 8 so recall can never truncate a true pair out of a
  shortlist (it caps candidates *per adjudication call*, not calls per run).
- **The recall stage was NOT the cause of the false positives.** Checked against the old stored
  1024-d vectors: #5↔#3 was **0.1811** and #9↔#3 **0.3263** under the old embedder — both already
  above the 0.15 cutoff, both already shortlisted in the "verified" run. Identical shortlists,
  different verdicts: the change was purely the chat model.

## ❌ NOT verified / known gaps

- **✅ FIXED / DIAGNOSIS CORRECTED — the 429s were never about a "free pool".**
  `deepseek/deepseek-chat` is a **PAID** slug ($0.257/M prompt) and it routes to exactly **two**
  provider endpoints, StreamLake and DeepInfra — the same two named in the 429s. Buying credits
  therefore does nothing for it; **provider breadth** is the lever, not paid-vs-free. Verified via
  `GET /api/v1/models/<slug>/endpoints`: deepseek-chat **2** endpoints, deepseek-chat-v3.1 **8**,
  deepseek-v3.2 **15**. Now on `anthropic/claude-sonnet-5` and zero 429s across 7 runs.
  The original (partly wrong) note is kept below because the *lesson* still holds.
- **🚨 (historical) The free `deepseek/deepseek-chat` pool rate-limited mid-run and cost a
  real recurrence link.** Observed live 2026-09-12, 12:03: three consecutive
  `429 ... temporarily rate-limited upstream` (providers StreamLake **and** DeepInfra, shared
  free pool) exhausted every retry for one adjudication. `adjudicate_recurrence` failed closed,
  item #4 (staging sync) was treated as new, the #3↔#4 link never formed and only **1 of 2**
  escalations fired. Nothing crashed — the run looked fine unless you read the warnings.
  Root cause was that retries were **immediate**, so all three landed inside one 429 window.
  Backoff now fixes the retry behaviour, but **it does not remove the rate limit**. For a
  demo that must not degrade, add a personal provider key at
  openrouter.ai/settings/integrations to get off the shared pool.
  **Lesson: a fail-closed adjudicator turns a provider hiccup into a silently weaker demo.**
- **Whether #9 joins the group is still a coin-flip, on every model tried.** Across 7 runs
  `{#3,#4,#7,#8}` is stable but #9 ("add the warehouse batch to the on-call dashboard") drifts in
  and out. When it joins, the justification is usually a **transferred quote** — sonnet-5 linked it
  by citing *"the nightly ETL job failed silently again on Tuesday"*, which is evidence that **#3**
  recurred, not that #9 is what recurred. The `WHOSE repeat flag it is` rule suppresses most of
  these; it does not eliminate them, and one accepted link in the final live run still leaned on a
  candidate's context at `medium` confidence.
- **Grouping is not reproducible run-to-run, and the demo story depends on it.** Before the two
  fixes, three consecutive sonnet-5 runs gave `{3,4,7,8,9}`, `{3,4,7,8}` and `{3,8}` — the last
  with **zero escalations**, which would have silently killed the demo. After the fixes two runs in
  a row were correct, but nothing *pins* this: there is still no regression test, and the failure
  mode is quiet.
- **`anthropic/claude-sonnet-5` reliably refuses #3↔#4 at the moment #4 is filed.** In every
  sonnet run it answered `not the same (low)` for staging-sync vs ETL-alerting, under both the
  original and the tightened prompt. The thread still forms, but only later via #8 — which is why
  the escalation *count* moved between runs (1 vs 2) even when the final membership was identical.
  The group-merge fix is what makes the outcome survive this.
- **`env.example` has no leading dot**, so it is not the `.env.example` that `llm_client`'s error
  message and this file's own README reference. Anyone following that message looks for the wrong
  filename.
- **Free-text replies over Socket Mode are still unproven.** Only the button path was
  exercised live. The listener used for that test ran under a booby-trap wrapper that makes
  any model call raise, so free text could not be tested in the same process — and note the
  scopes: `message.im` event subscription is required for free-text DMs to arrive at all, and
  has never been confirmed on this app.
- **Recurrence is only as good as the LLM's judgement.** The cosine stage no longer decides
  anything; a wrong adjudication is now a wrong answer with no numeric backstop. There is no
  regression test pinning the verdicts, so a model swap can change grouping silently.
- **`#8` joining the group is a judgement call, not a certainty.** "Add a completion marker
  for the warehouse batch pipeline" was adjudicated into the thread (occurrence #4). It is the
  stopgap for the same root cause, so this is defensible — but a stricter reading would call
  it a separate task. `#9` ("add the warehouse batch to the on-call dashboard") was rejected,
  which is arguably inconsistent with accepting `#8`.
- **Extraction is not bit-reproducible even at `LLM_TEMPERATURE=0`.** Two consecutive runs with
  identical config gave 8/9 items byte-identical and **identical grouping**, but item #3's
  wording moved between "Make the nightly ETL failure actually alert someone" and "Make the
  nightly ETL job alert someone when it fails". Providers batch and route non-deterministically;
  temperature 0 narrows variance, it does not remove it. Do not script a demo around exact strings.
- **Adjudication costs one LLM call per item that shortlists anything.** The real run made 6
  adjudication calls on top of 3 extraction calls. `RECURRENCE_MAX_CANDIDATES` caps candidates
  per call, not calls per transcript. A large DB with a loose recall cutoff will get expensive.
- **The `recurrence_signal` field carries the whole system.** If a transcript never says
  "again" / "third time" / "same pipeline", grouping falls back to the adjudicator reasoning
  from two bare action items, which is measurably weaker — that is exactly the case that
  failed before this field existed. Untested against transcripts with no such language.
- **ClickUp and Teams have still never been called for real.** Both dry-run for want of
  credentials. `CLICKUP_STATUS_MAP` is still an unverified guess at a default workflow —
  expect it to need the same correction `JIRA_STATUS_MAP` needed.
- **Jira is verified against ONE team-managed project only.** A company-managed project has a
  different workflow and different transition names; `JIRA_STATUS_MAP` is per-workspace.
- **Slack DMs all go to one person.** `SLACK_USER_MAP` maps dana/sam/priya/jules/marcus/rachel
  to `U0C26ANN4KS` deliberately, so a demo can exercise the button path. Not a real routing test.
- **No test suite.** All verification was ad-hoc inline scripts, none committed.
- **Not a git repo.** No version control has been initialised.
- **Teams DM is a degraded path** — a Workflows webhook is one-way, so Teams gets a
  free-text prompt, never buttons (`supports_buttons = False`).

## External API facts (verified live 2026-09-12 — do not "correct" these)

- OpenRouter **does** have `/api/v1/embeddings`. Embedding models are catalogued at
  `/api/v1/embeddings/models` and **deliberately do not appear in `/api/v1/models`**.
- `liquid/lfm-2.5-embedding-350m:free` is real and $0, but has a **512-token context** —
  hence `EMBEDDING_MAX_CHARS` truncation.
- **ClickUp v2 is current**; `api.clickup.com/api/v3` returns 404.
- **Jira Cloud v3 requires ADF**, not plain strings, for descriptions and comments, and has
  no writable status field (read transitions, POST a transition id).
- **ClickUp auth header takes the bare token** — `Authorization: pk_...`. A `Bearer` prefix 401s.
- **Teams classic O365 connector webhooks were disabled 18–22 May 2026** and no longer
  deliver. `TeamsAdapter` targets a Power Automate **Workflows** webhook instead.
- **`liquid/lfm-2.5-embedding-350m:free` returns 1024-d vectors**, and its similarities run
  *low*: two genuinely related sentences score ~0.38, unrelated ~0.14. Any threshold copied
  from OpenAI-style embeddings (0.78+) will never fire against it. Verified 2026-09-12.
- **A provider can reject `response_format` without ever naming it.** Novita (via OpenRouter)
  400s with `"model: ... does not support feature: structured-outputs"`. Confirmed causal:
  the identical call without `response_format` succeeds. This is why the marker list in
  `_is_json_mode_rejection` includes the "structured outputs" spellings — see below.
- **`GET /api/v1/models` exposes `supported_parameters`**, so models lacking
  `response_format` can be found programmatically (12 of 443 free models, 2026-09-12).
- **Jira team-managed ("next-gen") projects expose transitions by NAME as well as id**, and a
  fresh board has no Blocked state. Always probe `/rest/api/3/issue/<key>/transitions` against
  a real issue before trusting a status map. `DELETE /rest/api/3/issue/<key>` returns 204 and
  is the clean way to remove a probe issue.
- **Slack scope notes.** `chat:write` alone cannot resolve a channel by name — posting to a
  channel the bot is not in returns `channel_not_found`, not `not_in_channel`, which reads like
  the channel is missing. `conversations.list` needs `channels:read`, and asking for
  `types=private_channel` additionally demands `groups:read`. `users.info` needs `users:read`.
  Re-installing an app with added scopes can return the **same token string** — verify scopes
  via the `x-oauth-scopes` response header, not by comparing token strings.
- **`provider: {"require_parameters": true}` is stricter than the catalogue suggests.** Models
  whose `supported_parameters` list `response_format` can still have **no endpoint** that satisfies
  `temperature` + `response_format` together, and OpenRouter then returns **404**
  `"No endpoints found..."` with a `routing_funnel` showing `failed_routing_step`. It is a
  preference, never a requirement — always be able to drop it.
- **`GET /api/v1/credits` lies about paid access.** It reported `total_credits: 0`,
  `is_free_tier: true` and a frozen `total_usage` while paid calls were succeeding with real
  per-call `cost` fields. Trust a probe call's `cost`, not the balance endpoint.
- **`openai/text-embedding-3-small` on OpenRouter**: 1536-d, 8192-token context, $0.02/M, two
  endpoints (OpenAI and Azure). Azure's does not advertise `dimensions`, so leave
  `EMBEDDING_DIMENSIONS` at 0. Its similarities run **higher** than the old free model's — see the
  re-measured bands above; any threshold carried over from the free model is meaningless.
- **`slack_sdk.SlackResponse` is not a mapping.** The JSON body is on `.data`; `dict(response)`
  does not work and `hasattr(response, "keys")` is False.

## Design decisions worth not undoing

- Extraction asks for `{"action_items": [...]}`, **never a bare top-level array** — provider
  JSON mode rejects a top-level array on several backends, and that only breaks after a
  model swap.
- The JSON-mode fallback in `llm_client.chat_json` fires **only** on a 400/422 whose message
  names the parameter *or the feature*. It previously fired on any error, which silently
  disabled structured output on a 401. Do not loosen the **status** check back.
  **Fixed 2026-09-12:** the marker list was too *narrow* — it matched only `response_format`
  / `json_object` / `json mode` / `json_schema`, so a real Novita 400 saying
  `"does not support feature: structured-outputs"` was not recognised, the fallback never
  fired, and the model swap hard-failed — the exact case the fallback exists to prevent.
  Added the `structured-outputs` / `structured outputs` / `structured_outputs` /
  `structured output` spellings. The 401 guard is unaffected: a 401 is rejected by the
  status check before markers are ever consulted (re-verified live, both directions).
- **Recurrence is two-stage and the stages have different jobs.** Embeddings do RECALL at a
  deliberately loose cutoff; the LLM does PRECISION. Do not re-introduce a single similarity
  threshold — it was measured on real vectors and no cutoff separates the populations
  (intended thread 0.18–0.34, unrelated pairs up to 0.59). The two-stage split is the only
  reason this works.
- **The recurrence evidence is in the dialogue, not the action item.** `recurrence_signal` is
  captured during extraction precisely because the sentence that proves a repeat ("This is the
  third time we've had an action item about this pipeline") sits several lines away from the
  action item and is otherwise discarded. Removing that field breaks grouping — verified: the
  same run without it rejected #4↔#3.
- **A recurrence means it came back in a LATER meeting.** `RECURRENCE_CROSS_MEETING_ONLY`
  excludes same-meeting candidates. Two action items from one retro about one system are two
  tasks, not a recurrence — and same-meeting pairs are the strongest cosine matches in the
  whole sample (#7↔#8 = 0.594), so without this they dominate the shortlist.
- **Adjudication failure fails CLOSED (item treated as new).** Falling back to the recall
  cutoff would mean trusting a threshold known not to separate.
- **A recurrence link merges groups; it never picks one and drops the rest.** A new occurrence is
  often the first evidence that two separately tracked threads are one problem. Taking only the
  best-scoring candidate's group orphans items and under-counts the thread — see the fix above.
- **A repeat flag belongs to the item it was captured with.** The NEW item's own "flagged this as a
  repeat" line is the strongest evidence there is; a CANDIDATE's flag proves only that the candidate
  came back. Carrying a candidate's flag across to a new item on a different system is the exact
  shape of every false positive seen this session (#5, #9). The rule is in `_ADJUDICATE_SYSTEM` —
  keep it two-sided: an earlier, one-sided version of it also suppressed the genuine #3↔#4 link.
- **The recurrence evidence must be captured WHOLE.** `recurrence_signal` keeps every sentence that
  evidences the repeat, not the most quotable one, at a 400-char cap. The clauses naming *where* it
  came up before and *what* was tried before are the only text tying differently-worded items
  together, and losing them collapses the group with no error anywhere.
- Dry-run gates the socket write only. Adapters synthesise a response in the *real* API's
  shape, so response-parsing code runs identically with or without credentials.
- `DRY_RUN` is tri-state: `true` / `false` / unset = per-adapter automatic.

## Next steps (suggested order)

1. **Pin the grouping in a test — this is now the top priority by a distance.** Every recurrence
   decision is the model's, with no numeric backstop, and this session watched the grouping swing
   between `{3,4,7,8,9}`, `{3,4,7,8}` and `{3,8}` on the *same* config. Two bugs that each silently
   destroyed the group were found by reading logs, not by any test. Assert: the thread is
   `{#3,#4,#7,#8}`, #5 is rejected, at least one escalation fires. `DRY_RUN=true DB_PATH=...` runs
   the whole pipeline with real models and zero external side effects — that is the harness.
2. **Decide the #9 policy and encode it.** It drifts in and out on every model tried, and when it
   joins the justification is usually a quote transferred from another item. Either accept it
   deliberately (and stop calling it a false positive) or tighten the rule further.
3. **Try a transcript with no "again"/"third time" language** and see whether grouping survives
   without `recurrence_signal`. That is the known weak spot.
4. Bring up ClickUp and Teams the same way Jira was: credentials in, then probe the real
   workflow before trusting `CLICKUP_STATUS_MAP`.
5. Decide whether `#8`-style stopgaps belong in the group — it is accepted while `#9` is
   rejected on similar evidence.
6. `git init`. Still no version control, and this session rewrote the recurrence core.

### Demo-day notes (2026-09-12)

- `demo.py --sample all --reset-db` is **live** against Jira KAN and Slack #eng-retro. Each
  run creates 9 new Jira issues and sends 11 Slack messages, and costs roughly **$0.10-0.15**
  on `anthropic/claude-sonnet-5`. Delete KAN-* between rehearsals, or set `DRY_RUN=true`
  (optionally with `DB_PATH=`) to rehearse with real models and zero side effects.
- **The demo block is KAN-44 … KAN-52.** KAN-20 … KAN-43 are earlier runs from this session,
  including an over-grouped one and a partially-completed interrupted one — delete them so the
  board is not confusing.
- **If a run shows fewer than 2 escalations, read the extracted `recurrence_signal` first**, not
  the adjudicator. A signal that lost its "August retro, sprint 42" / "last time it was the
  alerting" clause takes the whole group down with it and logs nothing.
- Extraction is not bit-reproducible even at temperature 0 — grouping is stable, exact wording
  is not. Do not script narration around exact item strings.
- **Biggest live risk is the free OpenRouter pool 429ing**, which degrades the recurrence
  story quietly rather than loudly. Backoff helps; a personal provider key removes it.
  If a run shows fewer than 2 escalations, check the log for `adjudication failed`.
- If Slack or Jira breaks mid-demo, the run still completes: per-adapter failure isolation is
  verified, a failing adapter errors alone and its peers still deliver.

---

## Maintaining this file

**Update this file in the same session you change the code — before you finish your turn.**
This is enforced, not merely requested:

- **SessionStart hook** — if any `.py`, `requirements.txt` or `.env.example` is newer than this
  file, the session opens with a warning that the claims below may be stale.
- **Stop hook** — when you finish a turn, the same check runs. If the code is newer than this
  file, it blocks once and tells you to update it. Updating this file makes it the newest file,
  so the check then passes on its own.

Both run `.claude/hooks/claude_md_guard.py`, which fails open — if it errors it exits silently
rather than breaking the session. Do not defer the update and do not wait to be asked.

What to change:
- Moved something from unverified to verified? Move the bullet from ❌ to ✅ **and say what
  evidence proved it** — a command that ran, its output. Never mark something verified
  because it looks correct.
- Broke or discovered something? Add it to ❌ with enough detail to act on.
- Added or removed a file? Update the file map.
- Always refresh **Last verified** at the top.

Be accurate over flattering. A wrong ✅ is worse than a missing one: the next session will
trust it and build on sand.
