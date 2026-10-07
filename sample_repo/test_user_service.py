from user_service import find_duplicate_emails, average_account_age_days


def test_find_duplicate_emails():
    users = [
        {"email": "a@x.com"},
        {"email": "b@x.com"},
        {"email": "a@x.com"},
    ]
    assert find_duplicate_emails(users) == ["a@x.com"]


def test_find_duplicate_emails_none():
    users = [{"email": "a@x.com"}, {"email": "b@x.com"}]
    assert find_duplicate_emails(users) == []


def test_average_account_age_days():
    users = [{"account_age_days": 10}, {"account_age_days": 20}]
    assert average_account_age_days(users) == 15.0


def test_average_account_age_days_empty():
    assert average_account_age_days([]) == 0.0
