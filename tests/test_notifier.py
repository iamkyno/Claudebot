from types import SimpleNamespace

from bot.notifier import Notifier


def test_imessage_passes_text_as_argument():
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    n = Notifier(True, "+27820000000", runner=runner, platform="darwin", background=False)
    n.send('BUY "XAUUSD" \\ end tell')
    cmd = calls[0]
    assert cmd[0] == "osascript" and cmd[-2:] == ['BUY "XAUUSD" \\ end tell', "+27820000000"]
    assert "XAUUSD" not in cmd[2]  # text never pasted into the AppleScript


def test_off_without_recipient_or_mac():
    called = []
    run = lambda *a, **k: called.append(a)  # noqa: E731
    Notifier(True, "", runner=run, platform="darwin", background=False).send("x")
    Notifier(True, "+27", runner=run, platform="linux", background=False).send("x")
    assert called == []


def test_failure_is_logged_not_raised():
    def runner(cmd, **kw):
        raise OSError("no osascript")

    Notifier(True, "+27", runner=runner, platform="darwin", background=False).send("x")


def test_background_flush():
    sent = []
    n = Notifier(True, "+27", platform="darwin",
                 runner=lambda cmd, **kw: sent.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
    n.send("hello")
    n.flush()
    assert sent and sent[0][-2] == "hello"
