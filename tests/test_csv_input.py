import csv

from verifier.csv_input import parse_csv, MAX_EMAILS


def test_csv_accepts_2000_unique_emails_and_explicit_email_column():
    payload = 'name,email\n' + ''.join(f'Person {i},p{i}@example.com\n' for i in range(2000))
    rows, duplicates, error = parse_csv(payload.encode())
    assert len(rows) == 2000
    assert duplicates == 0 and MAX_EMAILS == 2000
    assert rows[0].email == 'p0@example.com'
    assert error is None


def test_csv_deduplicates_case_insensitively_and_rejects_missing_column():
    rows, duplicates, error = parse_csv(b'Email\nperson@EXAMPLE.COM\nperson@example.com\n')
    assert len(rows) == 1 and rows[0].email == 'person@example.com'
    assert duplicates == 1 and error is None
    assert parse_csv(b'name,website\nJane,example.com\n')[2] == (
        'CSV must have an email column (header: email, emails, or email address)'
    )


def test_csv_rejects_more_than_2000_unique_rows_and_reports_bad_rows():
    payload = 'email\n' + ''.join(f'p{i}@example.com\n' for i in range(2001))
    assert parse_csv(payload.encode())[2] == 'CSV exceeds the 2000 unique-email limit'
    rows, _, error = parse_csv('email,phone\nnot-an-email,123\n'.encode())
    assert rows[0].original == 'not-an-email'
    assert rows[0].original != b'\xff'
    assert error is None


def test_bad_csv_encoding_is_handled_cleanly():
    assert parse_csv(b'\xff')[2] == 'CSV must be UTF-8 (or UTF-8 with BOM)'


def test_different_domains_are_distinct_and_quoted_addresses_are_not_rewritten():
    rows, _, error = parse_csv(b'email\n"""Foo""@example.com"\nfoo@example.net\nFoo@example.com\n')
    assert error is None
    assert len(rows) == 3
