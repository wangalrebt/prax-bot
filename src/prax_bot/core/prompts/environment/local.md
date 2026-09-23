Execution environment:
- Commands run on the user's machine in a private repository copy and virtualenv, without isolation. Shell commands can affect files outside that copy; keep them scoped to the task.
- Commands start at the copy's root; use relative repository paths. File tools also accept {workdir} as a virtual repository root, but that is not a shell directory.
- Scratch paths differ between tools. For example, write_file uses {scratch_dir}/repro.py, then run uses python "$TMPDIR/praxbot/scratch/repro.py". The file-tool path is virtual: never pass it directly to a shell command. The shell scratch directory is "$TMPDIR/praxbot/scratch". File tools refuse any other path outside the repository.
- The runtime handles exporting and applying the repository diff.
