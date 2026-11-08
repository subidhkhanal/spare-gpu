import os
import subprocess
import sys

MODEL_TEXT = "curly ’ dash — check ✓ sparkle ✨"


def run_piped(code):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    return subprocess.run([sys.executable, "-X", "utf8=0", "-c", code], capture_output=True, env=env)


def test_model_text_survives_redirected_output():
    # found on a clean Windows run: a `gtu` command's output redirected to a file crashed with UnicodeEncodeError,
    # because redirected output uses cp1252, which has no check marks or emoji
    proc = run_piped(f"import typer, gpu_token_utilization.cli; typer.echo({MODEL_TEXT!r})")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert proc.stdout.decode("utf-8").strip() == MODEL_TEXT


def test_errors_survive_redirected_output_too():
    proc = run_piped(f"import sys, typer, gpu_token_utilization.cli; typer.echo({MODEL_TEXT!r}, err=True)")
    assert proc.returncode == 0 and proc.stderr.decode("utf-8").strip() == MODEL_TEXT

