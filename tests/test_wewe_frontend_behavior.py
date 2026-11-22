import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wewe_console_fail_closed_behavior():
    node = shutil.which("node")
    assert node, "Node.js is required for the real WeWe console behavior test"
    result = subprocess.run(
        [
            node,
            str(ROOT / "tests" / "js" / "wewe_fail_closed_test.js"),
            str(ROOT / "app" / "static" / "wewe.js"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
