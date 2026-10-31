# AI usage disclosure

This project was built with Claude Code (Claude Opus 5.5). The complete prompt history is in
[prompts/claude_prompts.md](../prompts/claude_prompts.md), exported verbatim from the session transcript by
`scripts/export_prompt_history.py`.

## What the AI did

- **Wrote almost all of the code, tests, contracts, and documentation** in this repository, working from a detailed
  brief written by the candidate.
- **Profiled the real data** and derived the keys, nullability, and rules from the results.
- **Ran everything reported here:** tests, benchmarks, real-data loads, and Docker builds.
- **Made design choices the brief left open**, recorded in [design_decisions.md](design_decisions.md).

## Where AI output was wrong, and how it was caught

These are recorded because they show why the results needed checking rather than trusting.

| What went wrong | How it was caught |
|---|---|
| Default host `localhost` stalled connections on IPv6 | Running the commands and timing them |
| `LoggerAdapter(merge_extra=...)` only exists on Python 3.13, but the project targets 3.12 | First real run crashed |
| A rejected row containing NUL crashed the run, because JSONB cannot store it | Automated test |
| An all-rejected file deleted its bookings and reported success | Independent code review by a second AI agent |
| Reconciliation's "explained" status could excuse a difference of any size | Independent code review |
| A stale quarantined row could overwrite a later correction | Independent code review |
| `--full-refresh --input` emptied a dataset | Independent code review |
| Benchmarks ran without checking free space, filled drive C, and stopped Docker Desktop and the candidate's other projects | Skipped tests in the next run led to the cause. Reruns then used a disk-space watchdog, and benchmarks now drop their database afterwards |
| With the database down, tests skipped and the mutation check reported bugs as uncaught | Reading the skip count. It now requires a passing baseline, and makes an unreachable database a failure |
| A single-run benchmark seemed to show a speedup, then a slowdown, both really noise | Comparing repeated runs; benchmarks now report medians |

The review findings came after 225 tests passed and 15 of 15 deliberate bugs were caught, so those measures alone
were not enough. Each defect now has a test in `tests/edge_cases/test_recovery_and_accounting.py`.

## How the results were checked

- **Against the data, independently.** `scripts/verify_real_data.py` recomputes results from the raw files using none
  of the pipeline's validation, transformation, or loading code.
- **Against an answer key.** The generator's expected results are computed with plain pandas, separately from the pipeline.
- **The tests themselves.** `scripts/mutation_check.py` breaks the code on purpose and confirms the tests fail.

These checks were also AI-written, and they share one limitation: they test what was thought of. See Known limitations
in the [README](../README.md).

## What the candidate personally reviewed, changed, and validated

> **To be completed by the candidate before submission, in their own words.** The guidelines ask for this directly,
> and only the candidate can answer it truthfully. Describe which parts you read and understood, what you ran yourself,
> what you changed or would change, and where you relied heavily on the AI.

- Reviewed:
- Ran myself:
- Changed or directed:
- Relied on heavily:
