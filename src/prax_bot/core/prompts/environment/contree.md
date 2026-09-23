Execution environment:
- Commands run inside a remote Linux sandbox. The repository and initial command directory are {workdir}; file tools resolve relative paths from there.
- Temporary reproduction and debugging files belong under {scratch_dir}, accessible at that path to both file tools and shell commands.
- The runtime handles exporting and applying the repository diff.
