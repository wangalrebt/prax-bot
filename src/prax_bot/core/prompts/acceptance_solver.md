An acceptance check was authored before this solver phase and failed by assertion on the original repository. This workspace starts from that original repository; exploratory author-phase edits were not carried over. The check covers only the behavior described below and is not a substitute for understanding the whole issue or running relevant project tests.

Requirement checked:
{requirement}

Frozen Python setup:
```python
{setup}
```

Frozen assertion expressions (each should evaluate to True after a correct fix):
{assertions}

Observed original-repository result:
{baseline_output}

The runtime reruns exactly this frozen code on your final workspace after finish. Do not edit or weaken the check. Fix the implementation using the task and project contracts, run appropriate additional checks, and distinguish your observed results from the pending runtime check when finishing. If the authored check conflicts with the task or existing contract, explain the conflict rather than changing behavior merely to satisfy it.
