"""Facts shown to people connecting an agent: the MCP tool list, kept in one place."""

MCP_TOOLS = [
    ('verify_emails', 'Check a list of addresses (up to 2,000). Returns a job.'),
    ('verify_csv', 'Check a CSV file by path or pasted text. Returns a job.'),
    ('get_verification_job', 'Read a job: status, progress, counts, and a count per reason.'),
    ('get_verification_results', 'Read a page of results, optionally only send-eligible rows.'),
    ('export_verification_csv', 'Write a finished job to a CSV file.'),
    ('cancel_verification_job', 'Stop a queued or running job; finished rows are kept.'),
    ('recheck_verification_job', 'Start a new job from a finished one: the unknowns, or all.'),
    ('record_hard_bounce', 'Add an address that really bounced to the suppression list.'),
    ('get_suppression', 'Check whether an address is on the suppression list.'),
]
