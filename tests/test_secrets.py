import config
import osutil
from conftest import ROOT, repo_files


def test_env_permissions_if_present():
    env = ROOT / ".env"
    if env.exists() and osutil.is_private(env) is not None:
        assert osutil.is_private(env), ".env must be chmod 600"


def test_env_example_lists_every_secret_key_with_empty_value():
    vals = config.parse_env((ROOT / ".env.example").read_text())
    for k in config.SECRET_KEYS:
        assert k in vals and vals[k] == "", k


def test_no_secret_value_anywhere_in_repo_logs_or_web():
    secrets = config.secret_values(config.parse_env(config.ENV_FILE.read_text()) if config.ENV_FILE.exists() else {})
    secrets += config.secret_values()
    if not secrets:
        return
    files = [f for f in repo_files() if f.name != ".env"]
    for d in ("logs", "web", "reports", "data"):
        files += [f for f in (ROOT / d).rglob("*") if f.is_file() and f.suffix != ".db"] if (ROOT / d).exists() else []
    leaks = [str(f.relative_to(ROOT)) for f in files if any(s in f.read_text(errors="ignore") for s in secrets)]
    assert not leaks, leaks


def test_log_redaction_scrubs_secret_values(monkeypatch, tmp_path):
    import logging

    import logsetup

    monkeypatch.setenv("APP_TOKEN", "super-secret-token-value")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    rec = logging.LogRecord("desk.x", logging.INFO, "f", 1, "token is %s ok", ("super-secret-token-value",), None)
    assert logsetup.RedactFilter().filter(rec)
    assert "super-secret" not in rec.getMessage() and "***" in rec.getMessage()


def test_no_wallet_or_signing_code(code_files):
    banned = ("Keypair", "signTransaction", "sign_transaction", "GMGN_PRIVATE_KEY", "/execute", "pumpportal.fun/api/trade")
    for f in code_files:
        if f.suffix == ".py" and not f.name.startswith("test_"):
            t = f.read_text(errors="ignore")
            assert not [b for b in banned if b in t], f.name
