from verifier.policy import interpret_rcpt


def test_only_a_clear_250_smtp_accept_is_a_candidate():
    assert interpret_rcpt(250).status == 'deliverable_candidate'
    assert interpret_rcpt(251).status == 'unknown'
    assert interpret_rcpt(252).status == 'unknown'
    assert interpret_rcpt(550).status == 'undeliverable'
    assert interpret_rcpt(451).status == 'unknown'
