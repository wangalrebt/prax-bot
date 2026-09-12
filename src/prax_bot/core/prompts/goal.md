Goal: {objective}

This is a goal run of up to {max_rounds} rounds. Each round is a fresh conversation on the same workspace; your finish summary and the current diff carry forward. Record decisions and remaining work in that summary. {how}
Call finish with goal_met=true when the evidence available supports completion of the whole goal. Report observed checks and label scheduled runtime checks as pending; the runtime also requires its configured checks to pass. Otherwise call finish with goal_met=false and state exactly what remains, so the next round can start there.
