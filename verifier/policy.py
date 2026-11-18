"""Conservative SMTP result interpretations."""
import re
from dataclasses import dataclass, field, replace


@dataclass
class ProbeVerdict:
    status: str
    reason: str
    smtp_code: int | None = None
    flags: list[str] = field(default_factory=list)
    host: str | None = None


ENHANCED = re.compile(r'\b([245])\.(\d{1,3})\.(\d{1,3})\b')
# Wording servers use when they are refusing US (our IP, reputation, policy), not describing the mailbox.
POLICY_WORDS = re.compile(
    r'block(?:ed|list)|spamhaus|black\s?list|\bsbl\b|\bpbl\b|reputation|denied|not authori[sz]ed|'
    r'relay|policy|banned|suspicious|\bspam\b|too many|rate limit|dynamic|residential|rdns|'
    r'reverse dns|ptr record|access denied|client host|connection refused|authentication required',
    re.IGNORECASE)


# Wording that positively says the recipient/mailbox is the problem.
MAILBOX_WORDS = re.compile(
    r"user unknown|unknown user|unknown recipient|no such (?:user|recipient|mailbox|person|address)|"
    r"(?:does(?:n't| not)|do not) exist|invalid (?:recipient|mailbox|address|user)|"
    r"mailbox (?:unavailable|not found|disabled|unknown|is inactive)|recipient (?:not found|unknown)|"
    r"unrout?eable|no mailbox|account (?:disabled|inactive|not found|has been disabled)|"
    r"not a valid (?:recipient|mailbox|user)|bad destination|user not found|address rejected",
    re.IGNORECASE)


def names_a_missing_mailbox(text: str) -> bool:
    enhanced = ENHANCED.search(text or '')
    return bool((enhanced and enhanced.group(2) == '1') or MAILBOX_WORDS.search(text or ''))


def refused_by_policy(text: str) -> bool:
    """True when a 5xx reply is about the *sender* (blocklist, reputation, policy), not the mailbox."""
    if re.search(r'5\.4\.1\b.*recipient address rejected', text or '', re.IGNORECASE):
        return False  # Microsoft 365 answers this way for a recipient that does not exist
    enhanced = ENHANCED.search(text or '')
    if enhanced and enhanced.group(2) == '1':
        return False  # X.1.x = bad destination mailbox/address: genuinely about the recipient
    if enhanced and enhanced.group(2) in ('7', '4'):
        return True   # X.7.x security/policy, X.4.x network/routing
    return bool(POLICY_WORDS.search(text or ''))


def interpret_rcpt(code: int, text: str = '') -> ProbeVerdict:
    if code == 250:
        return ProbeVerdict('deliverable_candidate', 'smtp_rcpt_accepted', code)
    if 500 <= code <= 599 and refused_by_policy(text):
        return ProbeVerdict('unknown', 'smtp_blocked_by_policy', code)
    if 550 <= code <= 556:
        if text.strip() and not names_a_missing_mailbox(text):
            # A bare "550 send email" says nothing about the mailbox; do not call it undeliverable.
            return ProbeVerdict('unknown', 'smtp_ambiguous_response', code)
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
