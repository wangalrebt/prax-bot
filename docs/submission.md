# Prax Bot — submission draft

Prepared for the Nebius × NVIDIA Global AI Hackathon, **Coding and Agentic Engineering Track**.

This file contains copy for the submission form. Source and test-build links are below. The actual-app demo video is available below. The required public YouTube upload, final submission, and judging-access arrangements still need to be completed.

## Project name

Prax Bot

## Tagline

A Nemotron coding agent that tests fixes in isolated sandboxes before you apply them.

## Short description

Prax Bot makes AI-generated code changes reviewable. Nemotron works inside a Token Factory Sandbox; Prax checks the proposed patch independently, shows baseline and patch results together, and lets you compare approaches before explicitly applying a change to your repository.

## Inspiration

A coding agent saying it finished leaves important questions unanswered. Did the proposed patch pass the tests? Did those tests already fail before the change? Did the agent install something that will be missing when the patch reaches your machine?

Prax grew out of wanting those answers alongside the diff. The developer should be able to inspect the evidence, compare alternatives, and choose which changes reach the repository.

## What it does

Give Prax a task in a Git repository. NVIDIA Nemotron 3 Super reads files, edits code, and runs commands inside a Nebius Token Factory Sandbox. Commands and edits advance an immutable image, preserving checkpoints of the work.

Prax independently runs configured checks against a clean baseline and against the proposed patch. The Before / After view presents both results, including commands, exit codes, timing, and logs. This helps distinguish pre-existing failures from regressions and catches changes that depend on the agent's incidental environment.

Compare starts two attempts from the same checkpoint. Each keeps its own patch, trajectory, checks, and usage. Choosing A or B loads that candidate for review. Applying the selected files is a separate action that writes to the developer's repository.

A persistent Main session can delegate to named work sessions and inspect their results. Sessions run independently and retain pending changes and evidence across close, reopen, and server restart. Stop cancels queued instructions and settles the current call before stopping; completed edits remain available.

## How we built it

Prax is a Python application with a localhost web interface and terminal front ends. Nemotron 3 Super supplies the coding decisions through Token Factory's inference API using structured tool calls. The ConTree SDK supplies the sandbox execution and immutable images used for checkpoints, independent attempts, and forked verification.

The verification fork begins with the baseline environment plus the patch that an Apply would write. A second fork checks the unchanged baseline. The runtime records that evidence independently of the model's final summary. Verification statuses distinguish passing checks, failures, unavailable evidence, and stale results.

The interface streams tool events and retains each session's chat, diff, checks, and trajectory. A saved session has a stable identity; reopening creates a new run segment and rejects requests from an old run generation.

## Challenges

The difficult part was making saved results mean the same thing after a workspace changed, a session reopened, or a process stopped. Prax preserves check provenance, marks stale evidence, keeps comparison usage from being counted twice, and separates choosing a candidate from applying it.

Model behavior also shaped the harness. Bounded file reads, structured editing feedback, checkpoint restoration, and context pruning help the model work within a step budget. Small benchmark experiments sometimes contradicted our expectations; unsuccessful changes and corrected accounting remain documented.

## Accomplishments

The earlier [live integration exercise](validation/live-20261029.json) used **29 Nemotron model calls** against real Token Factory Sandboxes. It verified Main delegation, concurrent sessions, a patch passing against a failing baseline, two independently checked alternatives, explicit Apply, queue cancellation, and reopen/restart recovery. The selected patch changed only the intended file and passed the test on the host after Apply.

The separate [browser workflow validation](validation/browser-20261030.json) completed with **19 calls and 73,145 tokens** in its successful attempt. Main delegated without editing its own workspace. The child passed independent checks against a failing baseline, both comparison candidates passed, and choosing A left the host unchanged. Apply changed only `calc.py`, after which the host test passed. Renaming, closing, and reopening the child preserved its patch and check evidence. The key screens were visually reviewed; no layout issues or browser page errors were observed.

The browser record also retains two unsuccessful attempts. First, the no-edit finish guard prompted Main to edit its own workspace against the instruction; v0.1.1 corrects that guard for Main. After that fix, non-waiting polling exhausted the step budget, interrupted the child, and produced an unsupported passing-test summary. The successful prompt explicitly requested `wait_seconds=30` and prohibited duplicate follow-ups and stopping the child. All three attempts used **49 calls and 231,692 tokens**. These small functional fixtures do not establish general coding quality.

The application also passed **383 automated tests**, lint, formatting checks, type checking, and JavaScript syntax validation. The [reviewed demo video](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.mp4) is 162.5 seconds long; [English captions](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.en.srt) and [video checks](validation/video-20261030.json) accompany it. YouTube upload remains pending.

## What we learned

Immutable sandbox images are useful beyond isolating commands: they make it practical to test the proposed change independently and compare alternatives from the same starting point. Clear evidence and recovery behavior are product features, because users need to understand what has changed and what remains pending.

Evaluation needs the same care. Our benchmark reports preserve failures, account for incomplete attempts, and distinguish development samples from broader evidence. We do not claim a full SWE-bench Verified score or a general advantage over another agent.

## What's next

Broader task evaluation and more language/toolchain coverage are planned. Improving session navigation and making check results easier to inspect will follow direct user feedback. These are future work, not demonstrated capabilities of this entry.

## Built with

Python 3.12, NVIDIA Nemotron 3 Super, Nebius Token Factory inference, Token Factory Sandboxes, ConTree SDK, OpenAI-compatible client, Pydantic, Typer, Rich, Textual, JavaScript, HTML/CSS, pytest, Ruff, Pyright, uv.

## Nebius and NVIDIA feedback

Immutable sandbox images fit the workflow well: they provide checkpoints and let a proposed change be tested independently of the agent's working environment. During development, unclear per-project sandbox access, API limits reported only at request time, and differing project-ID environment variable names made setup harder. Clearer diagnostics would help. The earlier 29-call integration exercise encountered one transport error; a retry succeeded. Nemotron's tool calling supported the full workflow, with structured editing feedback and explicit verification evidence remaining important.

More detailed observations, including the SDK versions involved, are in [FEEDBACK.md](../FEEDBACK.md).

## Development timeline

The first repository commit is dated September 5, 2026. Work recorded during the hackathon includes the coding runtime, forked checks, context experiments, comparison, persistent work sessions, and recovery. The owner should confirm whether an earlier prototype existed before describing the project as entirely new; the Git history alone cannot establish that.

## Links and final handoff

| Item | Status |
|---|---|
| Public source repository | [wangalrebt/prax-bot](https://github.com/wangalrebt/prax-bot), with its MIT license |
| Working demo or test-build URL | [v0.1.1 release](https://github.com/wangalrebt/prax-bot/releases/tag/v0.1.1), with [testing instructions](judging.md); usable free judge access must still be arranged |
| Demo recording | [2:42 MP4](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.mp4) and [English captions](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.en.srt), with actual app footage and disclosed edits |
| Public YouTube video | Awaiting account sign-in and upload; the MP4 download does not satisfy the YouTube requirement |
| Project description and technology feedback | Ready in this file |
| Verification evidence | [Browser validation, including failed attempts](validation/browser-20261030.json), [earlier integration validation](validation/live-20261029.json), and [testing instructions](judging.md) |
| Team and project-origin fields | Owner to complete or confirm |

The [official rules](https://nebiusglobalaihackathon.devpost.com/rules) list the deadline as **October 30, 2026, 10:00 a.m. Pacific** and require a public code repository with an open-source license, a demo/test-build URL, and a public YouTube demonstration under three minutes. Testing access must remain free for the judging period. Check the live form and rules when completing the entry. Final submission has not been made.
