Finish deferred for one focused review. You have at most {left} more tool turns, including the final finish, within the original budget. You may finish sooner if the evidence is sufficient.

Review the changed behavior against the task and the input contract in relevant code and callers. Look for a missed boundary, exception path, or existing fallback that the change affects. Derive any additional check from that evidence; do not invent requirements. If a distinct gap exists, run a small focused check, correct the implementation if needed, and verify the correction. Reuse checks already observed on unchanged code. Follow the verification plan; scheduled runtime checks may remain pending.

Then call finish again with the outcome, observed review/check results, and remaining gaps. Report blocked or unverified cases honestly. This review will not be requested again.
