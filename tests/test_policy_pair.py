from verifier.policy import ProbeVerdict, detect_catch_all, send_eligible


def test_catch_all_is_not_a_verified_mailbox():
    result = detect_catch_all(
        ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250),
        ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250),
    )
    assert result.status == 'unknown'
    assert result.reason == 'catch_all_domain'
    assert not send_eligible(result.status, result.reason, [])


def test_clear_control_rejection_distinguishes_a_candidate_but_not_guarantee():
    result = detect_catch_all(
        ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250),
        ProbeVerdict('undeliverable', 'smtp_mailbox_rejected', 550),
    )
    assert result.status == 'deliverable_candidate'
    assert result.reason == 'smtp_rcpt_accepted'
    assert send_eligible(result.status, result.reason, [])
    assert not send_eligible(result.status, result.reason, ['role_based'])


def test_explicit_permanent_recipient_rejection_is_not_undone_by_catch_all_control():
    result = detect_catch_all(
        ProbeVerdict('undeliverable', 'smtp_mailbox_rejected', 550),
        ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', 250),
    )
    assert result.status == 'undeliverable'
