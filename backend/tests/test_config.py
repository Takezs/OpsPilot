from opspilot.config import Settings


def test_settings_use_supported_deepseek_defaults() -> None:
    settings = Settings()

    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-v4-flash"
