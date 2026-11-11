1. Align the least manufactured intent: give a helper the user's own words and exact crumbs, not your summary of them.
2. Crumbs are chess pieces: find them with `crumb_find`, then name each one as `id` or `id:a-b` at level 1. Level 1 is the same text in each store; a lower level can differ.
3. Write the brief in this order: the user's request in quotes with its crumb id, the pieces, the test for done, then your own notes, marked as yours.
4. A `delegate_task` child or a `message_agent` teammate cannot open your crumbs, so paste the level-1 text. A child can also run `cd "${HERMES_SKILL_DIR}/../../.." && python3 -m breadcrumbs bc <id> 1`. A `cron` job can open them.
5. A helper's answer is a report: compare each quote with `crumb(id, 1)`, and cite the ids to the user.
