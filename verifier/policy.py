"""Conservative SMTP result interpretations."""
from dataclasses import dataclass, field, replace


@dataclass
class ProbeVerdict:
    status: str
    reason: str
    smtp_code: int | None = None
    flags: list[str] = field(default_factory=list)
    host: str | None = None


def interpret_rcpt(code: int) -> ProbeVerdict:
    if code == 250:
        return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', code)
    if 550 <= code <= 556:
        return ProbeVerdict('undeliverable', 'smtp_mailbox_rejected', code)
    if 400 <= code <= 499:
        return ProbeVerdict('unknown', 'smtp_temporary_failure', code)
    return ProbeVerdict('unknown', 'smtp_ambiguous_response', code)


def detect_catch_all(candidate: ProbeVerdict, random_recipient: ProbeVerdict) -> ProbeVerdict:
    # If the random mailbox is accepted too, no per-mailbox claim is possible.
    if candidate.smtp_code == 250 and random_recipient.smtp_code == 250:
        return ProbeVerdict('unknown', 'catch_all_domain', 250, ['catch_all'])
    if candidate.smtp_code == 250:
        if random_recipient.status == 'undeliverable':
            return replace(candidate, status='deliverable_candidate', reason='smtp_rcpt_accepted')
        return ProbeVerdict('unknown', 'smtp_control_inconclusive', 250)
    if candidate.status == 'undeliverable' and random_recipient.smtp_code == 250:
        return candidate
    return ProbeVerdict('unknown', 'smtp_control_inconclusive', candidate.smtp_code)


def send_eligible(status: str, reason: str, flags: list[str], allow_role: bool = False) -> bool:
    return (
        status == 'deliverable_candidate'
        and reason == 'smtp_rcpt_accepted'
        and 'catch_all' not in flags
        and 'disposable' not in flags
        and (allow_role or 'role_based' not in flags)
    )
