Code style:
- Match the surrounding code's formatting, naming, imports, error handling and logging. Avoid unrelated renames, reformatting and dependencies.
- Add helpers, options or abstractions only for a concrete requirement supported by the code and its callers. Keep data access separate from policy; do not infer correctness from ordering or a convenient default.
- Comments explain why. Remove temporary debugging code and notes from the deliverable.
- Use the project's configured tools and the verification plan below. Scope formatter and linter changes to the task.
- Test requested behaviour and the motivating case, using the existing test structure and fixtures. Preserve public interfaces and behaviour outside the requested change.
