from datetime import time
from pathlib import Path

import pytest

from nasbot.config import ConfigError, from_env, level_rank, normalize_truenas_url

from .conftest import BASE_ENV


@pytest.mark.parametrize(("raw", "expected"), [
    ("truenas.tailnet.ts.net", "wss://truenas.tailnet.ts.net/api/current"),
    ("https://10.0.0.5", "wss://10.0.0.5/api/current"),
    ("http://10.0.0.5:8080/", "ws://10.0.0.5:8080/api/current"),
    ("wss://nas/api/current", "wss://nas/api/current"),
    ("wss://nas/api", "wss://nas/api/current"),
    ("ws://nas/api/v25.10.0", "ws://nas/api/v25.10.0"),
    ("  nas.local  ", "wss://nas.local/api/current"),
])
def test_url_normalization(raw, expected):
    assert normalize_truenas_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://nas", "wss:///api/current", "wss://nas/websocket"])
def test_url_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        normalize_truenas_url(raw)


def test_minimal_env_gives_sane_defaults():
    cfg = from_env(BASE_ENV)
    assert cfg.truenas_url == "wss://truenas.tailnet.ts.net/api/current"
    assert cfg.truenas_auth == "plain"
    assert cfg.truenas_verify_ssl is True
    assert cfg.poll_interval == 60
    assert cfg.min_alert_level == "WARNING"
    assert cfg.mention is None
    assert cfg.access_is_open
    assert cfg.state_path == Path("/data/state.json")


def test_reports_every_problem_at_once():
    with pytest.raises(ConfigError) as err:
        from_env({"POLL_INTERVAL_SECONDS": "5", "TRUENAS_AUTH": "kerberos", "ALERT_MIN_LEVEL": "loud"})
    text = str(err.value)
    for needle in ("DISCORD_TOKEN", "TRUENAS_API_KEY", "DISCORD_ALERT_CHANNEL_ID", "TRUENAS_URL",
                   "TRUENAS_USERNAME", "POLL_INTERVAL_SECONDS", "TRUENAS_AUTH", "ALERT_MIN_LEVEL"):
        assert needle in text
    assert len(err.value.problems) >= 8


def test_secrets_from_files(tmp_path):
    (tmp_path / "token").write_text("file-token\n")
    (tmp_path / "key").write_text("  9-keyFromFile \n")
    env = {k: v for k, v in BASE_ENV.items() if k not in ("DISCORD_TOKEN", "TRUENAS_API_KEY")}
    env |= {"DISCORD_TOKEN_FILE": str(tmp_path / "token"), "TRUENAS_API_KEY_FILE": str(tmp_path / "key")}
    cfg = from_env(env)
    assert cfg.discord_token == "file-token"
    assert cfg.truenas_api_key == "9-keyFromFile"


def test_missing_secret_file_is_reported(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "TRUENAS_API_KEY"}
    env["TRUENAS_API_KEY_FILE"] = str(tmp_path / "nope")
    with pytest.raises(ConfigError, match="TRUENAS_API_KEY_FILE"):
        from_env(env)


def test_full_options_parse():
    cfg = from_env(BASE_ENV | {
        "DISCORD_GUILD_ID": "42",
        "DISCORD_ALLOWED_USER_IDS": "1, 2;3",
        "DISCORD_ALLOWED_ROLE_IDS": "7",
        "DISCORD_MENTION_ROLE_ID": "99",
        "MENTION_MIN_LEVEL": "error",
        "TRUENAS_AUTH": "SCRAM",
        "TRUENAS_VERIFY_SSL": "no",
        "ALERT_IGNORE_CLASSES": "HasUpdate, ScrubPaused",
        "WATCH_APPS": "false",
        "DIGEST_TIME": "08:30",
        "DIGEST_TZ": "Asia/Seoul",
        "DATA_DIR": "/tmp/x",
        "LOG_LEVEL": "debug",
    })
    assert cfg.guild_id == 42
    assert cfg.allowed_user_ids == {1, 2, 3}
    assert cfg.allowed_role_ids == {7}
    assert cfg.mention == "<@&99>"
    assert cfg.mention_min_level == "ERROR"
    assert cfg.truenas_auth == "scram"
    assert cfg.truenas_verify_ssl is False
    assert cfg.ignore_alert_classes == {"HasUpdate", "ScrubPaused"}
    assert cfg.watch_apps is False
    assert cfg.digest_time == time(8, 30)
    assert cfg.tz.key == "Asia/Seoul"
    assert cfg.log_level == "DEBUG"
    assert not cfg.access_is_open


def test_user_mention_when_no_role():
    assert from_env(BASE_ENV | {"DISCORD_MENTION_USER_ID": "5"}).mention == "<@5>"


def test_bad_values():
    with pytest.raises(ConfigError) as err:
        from_env(BASE_ENV | {"DIGEST_TZ": "Mars/Olympus", "DIGEST_TIME": "25:99", "TRUENAS_VERIFY_SSL": "maybe",
                             "DISCORD_ALLOWED_USER_IDS": "abc"})
    assert len(err.value.problems) == 4


def test_plain_key_over_unencrypted_url_is_refused():
    env = BASE_ENV | {"TRUENAS_URL": "http://10.0.0.5"}
    with pytest.raises(ConfigError, match="REVOKES"):
        from_env(env)
    assert from_env(env | {"TRUENAS_ALLOW_INSECURE": "true"}).truenas_url.startswith("ws://")
    assert from_env(env | {"TRUENAS_URL": "https://10.0.0.5"}).truenas_url.startswith("wss://")


def test_scram_over_plain_ws_needs_binding_off():
    env = BASE_ENV | {"TRUENAS_URL": "http://10.0.0.5", "TRUENAS_AUTH": "scram"}
    with pytest.raises(ConfigError, match="CHANNEL_BINDING"):
        from_env(env)
    assert from_env(env | {"TRUENAS_CHANNEL_BINDING": "false"}).truenas_channel_binding is False


def test_check_mode_needs_no_discord():
    env = {k: v for k, v in BASE_ENV.items() if not k.startswith("DISCORD")}
    cfg = from_env(env, require_discord=False)
    assert cfg.truenas_username == "nasbot"


def test_level_rank_ordering():
    assert level_rank("INFO") < level_rank("warning") < level_rank("CRITICAL") < level_rank("EMERGENCY")
    assert level_rank("bogus") == level_rank("WARNING")


def test_env_example_is_valid(tmp_path):
    """Guards against the documented template drifting from the parser."""
    example = Path(__file__).parent.parent / ".env.example"
    env = {}
    for line in example.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    documented = set(env)
    env |= {"DISCORD_TOKEN": "t", "DISCORD_ALERT_CHANNEL_ID": "1", "TRUENAS_API_KEY": "1-k"}
    cfg = from_env(env)
    assert cfg.truenas_url == "wss://truenas.your-tailnet.ts.net/api/current"
    # every variable the parser reads is documented in the template
    src = (Path(__file__).parent.parent / "src" / "nasbot" / "config.py").read_text()
    import re
    read = set(re.findall(r'get\("([A-Z_]+)"', src)) | {"DISCORD_TOKEN", "TRUENAS_API_KEY", "TRUENAS_AUTH",
                                                         "ALERT_MIN_LEVEL", "MENTION_MIN_LEVEL"}
    advanced = {"TRUENAS_CALL_TIMEOUT", "DATA_DIR", "TRUENAS_ALLOW_INSECURE",  # README-only, on purpose
                "MEDIA_TIMEOUT", "MEDIA_VERIFY_SSL"}
    assert read - advanced <= documented, read - advanced - documented


# ------------------------------------------------------------------ media (Sonarr/Radarr)

def test_media_off_by_default():
    cfg = from_env(BASE_ENV)
    assert cfg.arr_instances == () and not cfg.media_enabled
    assert (cfg.media_poll_interval, cfg.media_grace_minutes) == (300, 10)
    assert not cfg.media_watch_stalled and not cfg.media_notify_imports and not cfg.media_mention


def test_media_instances_and_options(tmp_path):
    keyfile = tmp_path / "radarr.key"
    keyfile.write_text("radarr-key\n")
    cfg = from_env({**BASE_ENV, "SONARR_URL": "192.168.1.158:8989/", "SONARR_API_KEY": "s-key",
                    "SONARR_LINK_URL": "http://nas.soay-terrapin.ts.net:8989",
                    "RADARR_URL": "http://192.168.1.158:7878/api/v3", "RADARR_API_KEY_FILE": str(keyfile),
                    "MEDIA_POLL_INTERVAL_SECONDS": "120", "MEDIA_GRACE_MINUTES": "0",
                    "MEDIA_WATCH_STALLED": "yes", "MEDIA_NOTIFY_IMPORTS": "true", "MEDIA_MENTION": "on",
                    "MEDIA_HEALTH_IGNORE": "UpdateCheck, IndexerLongTermStatusCheck",
                    "MEDIA_VERIFY_SSL": "false", "MEDIA_TIMEOUT": "5"})
    sonarr, radarr = cfg.arr_instances
    assert (sonarr.kind, sonarr.url, sonarr.api_key) == ("sonarr", "http://192.168.1.158:8989", "s-key")
    assert sonarr.web_url == "http://nas.soay-terrapin.ts.net:8989"
    assert (radarr.url, radarr.api_key, radarr.web_url) == ("http://192.168.1.158:7878", "radarr-key",
                                                             "http://192.168.1.158:7878")
    assert cfg.media_enabled and cfg.media_poll_interval == 120 and cfg.media_grace_minutes == 0
    assert cfg.media_watch_stalled and cfg.media_notify_imports and cfg.media_mention
    assert cfg.media_health_ignore == {"UpdateCheck", "IndexerLongTermStatusCheck"}
    assert cfg.media_verify_ssl is False and cfg.media_timeout == 5.0


def test_media_problems_are_all_reported():
    with pytest.raises(ConfigError) as err:
        from_env({**BASE_ENV, "SONARR_URL": "http://x:8989", "RADARR_API_KEY": "k",
                  "MEDIA_POLL_INTERVAL_SECONDS": "10", "MEDIA_GRACE_MINUTES": "-1", "MEDIA_TIMEOUT": "0"})
    text = "\n".join(err.value.problems)
    assert "SONARR_URL is set but SONARR_API_KEY" in text and "Settings → General → Security" in text
    assert "RADARR_API_KEY is set but RADARR_URL is missing" in text
    assert "MEDIA_POLL_INTERVAL_SECONDS must be >= 60" in text
    assert "MEDIA_GRACE_MINUTES must be >= 0" in text and "MEDIA_TIMEOUT must be > 0" in text


def test_media_bad_url():
    with pytest.raises(ConfigError, match="SONARR_URL / SONARR_LINK_URL: unsupported scheme"):
        from_env({**BASE_ENV, "SONARR_URL": "ftp://x", "SONARR_API_KEY": "k"})
