import pytest

from verifier.syntax import normalize


@pytest.mark.parametrize(('raw', 'normalized'), [
    (' Person@EXAMPLE.COM ', 'Person@example.com'),
    ('"Jane.Doe"@Gmail.com', '"Jane.Doe"@gmail.com'),
    ('p\u00e5@b\u00fccher.de', None),  # Leave SMTPUTF8 out of this ASCII-only MVP.
])
def test_normalization_preserves_the_mailbox(raw, normalized):
    assert normalize(raw) == normalized


@pytest.mark.parametrize('raw', [
    'jane..doe@example.com', 'no-at.example.com', 'p@invalid', 'a@example.com extra',
    'p@example.com<mailto:p@example.com>', 'a' * 65 + '@example.com', '',
])
def test_bad_syntax_cannot_reach_network_checks(raw):
    assert normalize(raw) is None
