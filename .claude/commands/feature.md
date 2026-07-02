---
description: Run the full agent pipeline (research → plan → spec → implement → test → review) for a feature request
argument-hint: <feature description>
---

Run the ZENITH development pipeline for this request: $ARGUMENTS

Assign a short kebab-case slug to the request and use it in every stage header.

Execute the stages IN ORDER, invoking each subagent explicitly. Do not skip stages. Do not do a stage's work yourself in the main session — delegate it.

1. **Research** — Use the researcher subagent on the request. If it returns `Frozen-zone check: BLOCKED`, stop the pipeline immediately and report why to the user.

2. **Plan** — Use the planner subagent with the request + research brief. If it returns `NEEDS HUMAN DECISION`, stop and present the question to the user. Resume only after the user answers.

3. **Human gate** — Present the plan to the user and ask for explicit approval before writing the spec. Never proceed past this point without a yes.

4. **Spec** — Use the spec-writer subagent with the approved plan + research brief. Save the spec to `docs/specs/<slug>.md` so it is versioned with the code.

5. **Implement** — Use the implementer subagent with the spec. If it returns `SPEC DEFECT` or `NEW DEPENDENCY REQUESTED`, surface it to the user and loop back to the appropriate stage.

6. **Test** — Use the tester subagent with the acceptance criteria + implementation summary. If verdict is FAIL, send the specific defects back to the implementer subagent (max 2 fix cycles, then escalate to the user).

7. **Review** — Use the code-reviewer subagent on the diff. If REQUEST CHANGES, send CRITICAL + HIGH findings back to the implementer (max 2 cycles, then escalate).

8. **Wrap-up** — When the review verdict is APPROVE: summarize the whole run in ≤10 lines (slug, files changed, criteria passed, review verdict), remind the user this is NOT committed/pushed yet, and stop. The user commits and pushes manually — the pipeline never touches git history or deployment.

Context discipline: pass each subagent ONLY what it needs (the request, the prior stage's output, the slug). Do not paste full file contents between stages — subagents read files themselves.
