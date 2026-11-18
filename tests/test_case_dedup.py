from verifier.csv_input import parse_csv


def test_syntax_normalization_equality_respects_the_original_mailbox():
    rows, _, error = parse_csv(b'email\n"""Foo""@example.com"\nfoo@example.net\nFoo@example.com\n')
    assert error is None
    assert len(rows) == 3
