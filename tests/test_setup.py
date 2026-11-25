import os
import stat

import bot.setup as setup


def test_env_write_keeps_other_lines_and_is_private(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nIMESSAGE_TO=+27820000000\nEXN_API_KEY=old\n")
    monkeypatch.setattr(setup, "ENV_PATH", env)
    setup._write_env({"EXN_API_KEY": "exnsk-new", "EXN_ACCOUNT_ID": "42"})
    text = env.read_text()
    assert "# comment" in text and "IMESSAGE_TO=+27820000000" in text
    assert "EXN_API_KEY=exnsk-new" in text and "EXN_ACCOUNT_ID=42" in text and "old" not in text
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_pin_live_edits_only_those_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('# keep me\nmode: dry-run\nallow_live: false\nlive_account_id: ""\nrisk:\n  x: 1\n')
    monkeypatch.setattr(setup, "CONFIG_PATH", cfg)
    setup.pin_live("123456")
    assert cfg.read_text() == ('# keep me\nmode: live\nallow_live: true\n'
                               'live_account_id: "123456"\nrisk:\n  x: 1\n')
