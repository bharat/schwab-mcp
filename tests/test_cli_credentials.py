from __future__ import annotations

import os
from typing import Any

import yaml
from click.testing import CliRunner
from conftest import CliFakeAsyncClient

from schwab_mcp import cli


class TestAuthCredentialsFile:
    def test_falls_back_to_credentials_file(
        self,
        cli_auth_capture,
        cli_credentials_writer,
        cli_runner,
        cli_credentials_file,
    ):
        """Use credentials loaded from the configured credentials file."""
        captured = cli_auth_capture
        cli_credentials_writer("file-id", "file-secret")

        result = cli_runner.invoke(
            cli.cli,
            ["auth", "--token-path", str(cli_credentials_file.with_name("token.yaml"))],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert captured["easy_client_kwargs"]["client_id"] == "file-id"
        assert captured["easy_client_kwargs"]["client_secret"] == "file-secret"

    def test_cli_args_override_credentials_file(
        self,
        cli_auth_capture,
        cli_credentials_writer,
        cli_runner,
        cli_credentials_file,
    ):
        """Prefer explicit CLI credentials over values from the credentials file."""
        captured = cli_auth_capture
        cli_credentials_writer("file-id", "file-secret")

        result = cli_runner.invoke(
            cli.cli,
            [
                "auth",
                "--token-path",
                str(cli_credentials_file.with_name("token.yaml")),
                "--client-id",
                "cli-id",
                "--client-secret",
                "cli-secret",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert captured["easy_client_kwargs"]["client_id"] == "cli-id"
        assert captured["easy_client_kwargs"]["client_secret"] == "cli-secret"

    def test_errors_when_no_credentials_available(
        self,
        cli_runner,
        cli_credentials_file,
    ):
        """Reject authentication when neither CLI nor file credentials exist."""
        result = cli_runner.invoke(
            cli.cli,
            ["auth", "--token-path", str(cli_credentials_file.with_name("t.yaml"))],
        )

        assert result.exit_code == 1
        assert "client-id and client-secret are required" in result.output


class TestServerCredentialsFile:
    def test_falls_back_to_credentials_file(
        self,
        cli_server_capture,
        cli_credentials_writer,
        cli_runner,
        cli_credentials_file,
    ):
        """Use credentials loaded from the configured credentials file."""
        captured = cli_server_capture
        cli_credentials_writer("file-id", "file-secret")

        result = cli_runner.invoke(
            cli.cli,
            [
                "server",
                "--token-path",
                str(cli_credentials_file.with_name("token.yaml")),
                "--jesus-take-the-wheel",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert captured["easy_client_kwargs"]["client_id"] == "file-id"
        assert captured["easy_client_kwargs"]["client_secret"] == "file-secret"

    def test_cli_args_override_credentials_file(
        self,
        cli_server_capture,
        cli_credentials_writer,
        cli_runner,
        cli_credentials_file,
    ):
        """Prefer explicit CLI credentials over values from the credentials file."""
        captured = cli_server_capture
        cli_credentials_writer("file-id", "file-secret")

        result = cli_runner.invoke(
            cli.cli,
            [
                "server",
                "--token-path",
                str(cli_credentials_file.with_name("token.yaml")),
                "--client-id",
                "cli-id",
                "--client-secret",
                "cli-secret",
                "--jesus-take-the-wheel",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert captured["easy_client_kwargs"]["client_id"] == "cli-id"
        assert captured["easy_client_kwargs"]["client_secret"] == "cli-secret"

    def test_errors_when_no_credentials_available(
        self,
        cli_server_capture,
        cli_runner,
        cli_credentials_file,
    ):
        """Reject server startup before attempting client authentication."""
        captured = cli_server_capture
        result = cli_runner.invoke(
            cli.cli,
            ["server", "--token-path", str(cli_credentials_file.with_name("t.yaml"))],
        )

        assert result.exit_code == 1
        assert "client-id and client-secret are required" in result.output
        assert "easy_client_called" not in captured


class TestSaveCredentialsCommand:
    def test_saves_credentials_with_prompts(self, cli_credentials_file, cli_runner):
        """Save credentials supplied through interactive prompts."""
        result = cli_runner.invoke(
            cli.cli,
            ["save-credentials"],
            input="my-client-id\nmy-client-secret\n\n",
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "Credentials saved to:" in result.output

        with cli_credentials_file.open() as credentials:
            data = yaml.safe_load(credentials)

        assert data == {
            "client_id": "my-client-id",
            "client_secret": "my-client-secret",
        }

    def test_secret_prompts_do_not_echo(self, cli_credentials_file, cli_runner):
        """Secret prompt input must not be echoed back to the terminal."""
        result = cli_runner.invoke(
            cli.cli,
            ["save-credentials"],
            input="visible-id\nhidden-secret\nhidden-discord-token\n",
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "visible-id" in result.output
        assert "hidden-secret" not in result.output
        assert "hidden-discord-token" not in result.output

    def test_saves_discord_token_when_prompted(self, cli_credentials_file, cli_runner):
        """Persist the Discord token when supplied at the interactive prompt."""
        result = cli_runner.invoke(
            cli.cli,
            ["save-credentials"],
            input="my-client-id\nmy-client-secret\nmy-discord-token\n",
            catch_exceptions=False,
        )

        assert result.exit_code == 0

        with cli_credentials_file.open() as credentials:
            data = yaml.safe_load(credentials)

        assert data == {
            "client_id": "my-client-id",
            "client_secret": "my-client-secret",
            "discord_token": "my-discord-token",
        }

    def test_saves_credentials_with_flags(self, cli_credentials_file, cli_runner):
        """Save credentials supplied through command-line flags."""
        result = cli_runner.invoke(
            cli.cli,
            [
                "save-credentials",
                "--client-id",
                "flag-id",
                "--client-secret",
                "flag-secret",
                "--discord-token",
                "",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0

        with cli_credentials_file.open() as credentials:
            data = yaml.safe_load(credentials)

        assert data == {"client_id": "flag-id", "client_secret": "flag-secret"}

    def test_file_has_restricted_permissions(self, cli_credentials_file, cli_runner):
        """Create the credentials file with owner-only permissions."""
        cli_runner.invoke(
            cli.cli,
            [
                "save-credentials",
                "--client-id",
                "id",
                "--client-secret",
                "secret",
                "--discord-token",
                "",
            ],
            catch_exceptions=False,
        )

        mode = os.stat(cli_credentials_file).st_mode & 0o777
        assert mode == 0o600


class TestServerDiscordTokenCredentialsFile:
    def _patch_server(self, monkeypatch, captured: dict[str, Any]) -> None:
        monkeypatch.setattr(cli, "AsyncClient", CliFakeAsyncClient)
        monkeypatch.setattr(cli.tokens, "Manager", lambda p: type("M", (), {"path": p})())
        monkeypatch.setattr(cli.schwab_auth, "easy_client", lambda **kw: CliFakeAsyncClient())

        class FakeDiscordSettings:
            def __init__(self, **kwargs):
                captured["discord_settings"] = kwargs

        class FakeDiscordManager:
            authorized_user_ids = staticmethod(lambda ids: frozenset(ids) if ids else frozenset())

            def __init__(self, settings):
                captured["discord_manager_settings"] = settings

        monkeypatch.setattr(cli, "DiscordApprovalSettings", FakeDiscordSettings)
        monkeypatch.setattr(cli, "DiscordApprovalManager", FakeDiscordManager)
        monkeypatch.setattr(
            cli,
            "SchwabMCPServer",
            lambda *a, **kw: type("S", (), {"run": staticmethod(lambda: None)})(),
        )
        monkeypatch.setattr(cli.anyio, "run", lambda func, *args, **kw: None)

    def test_server_reads_discord_token_from_credentials_file(self, monkeypatch, tmp_path):
        captured: dict[str, Any] = {}
        self._patch_server(monkeypatch, captured)

        creds_path = tmp_path / "credentials.yaml"
        with open(creds_path, "w") as f:
            yaml.safe_dump(
                {
                    "client_id": "file-id",
                    "client_secret": "file-secret",
                    "discord_token": "file-discord-token",
                },
                f,
            )

        monkeypatch.setattr(cli.tokens, "credentials_path", lambda app: str(creds_path))
        for var in (
            "SCHWAB_CLIENT_ID",
            "SCHWAB_CLIENT_SECRET",
            "SCHWAB_MCP_DISCORD_TOKEN",
            "SCHWAB_MCP_DISCORD_APPROVERS",
        ):
            monkeypatch.delenv(var, raising=False)

        runner = CliRunner()
        result = runner.invoke(
            cli.cli,
            [
                "server",
                "--token-path",
                str(tmp_path / "token.yaml"),
                "--discord-channel-id",
                "12345",
                "--discord-approver",
                "67890",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert captured["discord_settings"]["token"] == "file-discord-token"
