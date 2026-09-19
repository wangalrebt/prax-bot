# Feedback on Nebius Token Factory, Sandboxes, and Nemotron

Notes kept while building Prax Bot, for the hackathon feedback field and for anyone hitting the same things.

## Sandboxes (ConTree)

- **Beta access is per project and silent.** With a valid key but no access, `whoami` succeeds and every permission is `false`; image calls return HTTP 403 "Insufficient permissions". A message pointing at the access page would save an afternoon.
- **`truncate_output_at` must be under 10 MiB**, which the API reports only as a 400 on the run request. The SDK's own example in `contree_sdk.langchain` uses exactly 10 MiB, which fails.
- **Runs are disposable by default.** New users expect state to persist between `run` calls; it only does with `disposable=False`. Worth a prominent line in the getting-started guide.
- **The SDK layout differs from the README.** The README imports `contree_client.httpx`; the installed 0.3.6 package has no such module. `ContreeSync()` with env vars or `auth.ini` is what works.
- **`NEBIUS_PROJECT_ID` versus `NEBIUS_AI_PROJECT`.** The SDK reads the first, the CLI's `auth` command reads the second. One name would be kinder.
- **Occasional API timeouts** during instance spawn under normal load. A retry fixed every case we saw; the SDK could retry those itself.
- **What worked well:** fork-from-any-image is instant and correct in our isolation tests, uploads and reads are simple, and the per-command immutable image made checkpoints free. Forks also carried a feature we could not have built cheaply elsewhere: every check runs in a fork of a clean image with only the diff applied, while the same check runs without the diff in a parallel fork, from the same client on another thread. The `python:3.12` image resolved from the project's tags in under a second.

## Token Factory

- The model catalog changed between two days of use, from seven `nvidia/` models to four. Documented deprecation notices would help anyone pinning ids.
- Function calling with `tool_choice` forced to a specific function worked reliably with Nemotron 3 Super; we lean on it to end runs.
- The `token_expiration` reported by the sandbox `whoami` did not match when the key actually stopped working; it kept working past that time.

## Nemotron 3 Super in an agent loop

- Reliable tool calling, but it sometimes replies with prose and no tool call; a nudge and a forced tool choice handle it.
- It occasionally sends `edit_file` text with indentation one space off on continuation lines. Exact-match editing fails every time; whitespace-tolerant matching fixed it.
- In a single 3,000-line module it tends to guess function names rather than look them up. A repository map with definitions and line numbers in the first prompt helped; see the benchmark in the README.
- Nemotron 3 Ultra solved that same task on its first attempt at similar step count.
