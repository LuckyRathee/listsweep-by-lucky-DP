from verifier.smtp_config import SMTPSettings


def test_live_smtp_requires_a_real_sender_domain():
    settings = SMTPSettings.from_environment()
    assert not settings.ready
    assert settings.reason == 'smtp_sender_identity_not_configured'
