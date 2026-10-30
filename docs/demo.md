# Prax Bot — demo video and recording plan

Actual application footage and host command output have been captured, and the key screens have been visually reviewed. The [browser validation record](validation/browser-20261030.json) includes the successful workflow and both earlier unsuccessful attempts. The reviewed [2:42 demo is public on YouTube](https://youtu.be/Ci8Vz0iyZGk) with English captions. The [MP4](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.mp4) and [English captions](https://github.com/wangalrebt/prax-bot/releases/download/v0.1.1/prax-bot-demo.en.srt) remain available as release downloads. [Video checks](validation/video-20261030.json) record the duration, codecs, hashes, captions, and editing disclosures. The outline below was the recording plan; the published edit lasts 162.5 seconds. Use the fresh fixture and prompts in [testing instructions](judging.md); inspect the complete results before recording. Keep the final public YouTube cut under three minutes, and label any omitted waiting time. Do not replace live results with mock UI or simulated tool output.

| Time | Show | Suggested narration |
|---|---|---|
| 0:00–0:15 | Title, failing host test, and the `return a - b` line | “Prax Bot makes coding-agent changes reviewable before they reach your repository. This small bug lets us see each decision clearly.” |
| 0:15–0:40 | Main receiving the task, then actual `session_start` and `session_read` calls | “Nemotron 3 Super runs through Token Factory. Main delegates to an independent work session and inspects its result. The coding work happens inside a Token Factory Sandbox.” |
| 0:40–1:10 | Child's one-line patch and Before / After check logs | “Prax checks the original baseline and the proposed patch independently. Here the same test fails before the fix and passes after it. The check runs with the patch in a clean fork, separate from the agent's working environment.” |
| 1:10–1:45 | Compare fixes: A uses addition, B uses `sum`; both results, diffs, and checks | “We can try two approaches from the same checkpoint. Each keeps its own patch, trajectory, verification, and usage. Neither is chosen automatically.” |
| 1:45–2:10 | Choose A, clean host `git diff`, then Apply `calc.py` and passing host test | “Choose loads an approach for review. Apply is the separate action that writes the selected files. The host test now passes.” |
| 2:10–2:30 | Rename/close/reopen a work session and show restored evidence | “Named sessions preserve their pending work and check evidence. Switching views leaves work running; reopening restores the saved session.” |
| 2:30–2:45 | End card over the actual finished app | “Prax Bot: Nemotron coding decisions, Token Factory sandbox checkpoints, and evidence you can inspect before applying a change. Source and test build are linked in the submission.” |

The fixture intentionally leaves `multiply` outside the task. Show that it remains unchanged. This is a small workflow demonstration, not a claim about difficult bug-solving performance.

## Recording checklist

- Use the printed Contree launch command, not `make demo`, whose backend is local.
- Confirm model/backend labels, actual tool events, and complete independent checks are visible.
- Use the protected test file and keep the two comparison sessions based on the still-unmodified host.
- Keep API keys, authorization files, local browser tokens, and unrelated desktop content out of frame.
- Show the real outcomes, including a visible error or disclosed retry if one occurs. Label time cuts.
- Current UI footage and visual review are complete. Keep the browser validation record separate from the earlier HTTP integration test, retain unsuccessful takes as diagnostics, and disclose that the final successful prompt explicitly waits for child completion.
- Video checks are recorded, and YouTube Studio confirmed publication with Public visibility. The upload includes 38 English caption cues and discloses synthetic narration.
- The public video URL is included in `submission.md`; keep the source, test build, and video links available during judging.

Optional supporting footage: show Stop cancelling a queued instruction and accepting a later follow-up, or restart the server and reopen saved sessions. Use this only if the core flow still fits under three minutes.
