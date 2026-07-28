from pathlib import Path

from jupyter_jcli.config import AppConfig


def test_app_config_reads_environment(monkeypatch):
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_URL", "http://example.test:9999")
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_TOKEN", "env-token")
    monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", "/tmp/jcli-debug-test")

    config = AppConfig.from_env()

    assert config == AppConfig(
        server_url="http://example.test:9999",
        token="env-token",
        debug_log_dir=Path("/tmp/jcli-debug-test"),
    )


def test_app_config_cli_values_override_environment(monkeypatch):
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_URL", "http://env.test:8888")
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_TOKEN", "env-token")

    config = AppConfig.from_env(server_url="http://cli.test:8888", token="cli-token")

    assert config.server_url == "http://cli.test:8888"
    assert config.token == "cli-token"
