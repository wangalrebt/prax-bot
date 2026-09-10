Goal: {objective}

This is a goal run of up to {max_rounds} rounds. Each round is a fresh conversation on the same workspace, so leave notes for the next round in the code and the todo list, not in prose. {how}
When the whole goal is achieved and verified, call finish with goal_met=true and say how you verified it. If it is not, call finish with goal_met=false and state exactly what remains, so the next round can start there.
