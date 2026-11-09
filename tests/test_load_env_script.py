"""scripts/load-env.sh and scripts/deploy.sh, run with bash and a fake `aws` command.

No AWS is contacted. The tests check that settings land in .env with the right quoting and
mode, that a failure leaves the old .env untouched, and that no value is ever printed.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

SECRET = "sk-VERY-SECRET-VALUE"


def posix(p: Path) -> str:
    return p.as_posix()


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ -n "${FAKE_AWS_FAIL:-}" ]]; then echo "AccessDenied" >&2; exit 255; fi\n'
        'cat "$FAKE_AWS_JSON"\n'
    )
    aws.chmod(0o755)
    target = tmp_path / ".env"
    target.write_text("OLD=1\n")

    def run(parameters, *, fail=False, path="/irb-watch/prod", args=None, extra_env=None):
        fake = tmp_path / "params.json"
        fake.write_text(json.dumps({"Parameters": parameters}))
        env = {
            **os.environ,
            "PATH": f"{posix(bin_dir)}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AWS_JSON": posix(fake),
            "ENV_FILE": posix(target),
            "PYTHON": posix(Path(sys.executable)),
            "SSM_PATH": path,
            # Git Bash on Windows rewrites arguments that start with "/" into Windows paths
            # when it starts a native program; the real server is Linux and does not.
            "MSYS_NO_PATHCONV": "1",
        }
        env.pop("FAKE_AWS_FAIL", None)
        if fail:
            env["FAKE_AWS_FAIL"] = "1"
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, posix(ROOT / "scripts" / "load-env.sh"), *(args or [])],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    return run, target, tmp_path


def p(name, value, prefix="/irb-watch/prod"):
    return {"Name": f"{prefix}/{name}", "Value": value, "Type": "SecureString"}


def test_writes_one_quoted_line_per_setting_sorted_and_prints_no_value(box):
    run, target, tmp = box
    result = run([p("OPENAI_API_KEY", SECRET), p("APP_URL", "https://app.example.com"), p("MAIL_FROM", "IRB Watch <a@b.c>")])
    assert result.returncode == 0, result.stderr
    assert target.read_text() == (
        "APP_URL='https://app.example.com'\n"
        "MAIL_FROM='IRB Watch <a@b.c>'\n"
        f"OPENAI_API_KEY='{SECRET}'\n"
    )
    assert "wrote 3 settings" in result.stdout
    assert SECRET not in result.stdout + result.stderr
    assert sorted(x.name for x in tmp.iterdir() if x.name.startswith(".env")) == [".env"]  # no temp files left


@pytest.mark.skipif(os.name == "nt", reason="file modes are not meaningful on Windows")
def test_the_file_is_private(box):
    run, target, _ = box
    assert run([p("A", "1")]).returncode == 0
    assert (target.stat().st_mode & 0o777) == 0o600


def test_spaces_and_hash_signs_stay_inside_the_quotes(box):
    run, target, _ = box
    assert run([p("NOTE", "a b # c $d")]).returncode == 0
    assert target.read_text() == "NOTE='a b # c $d'\n"


def test_only_parameters_directly_under_the_path_are_used(box):
    run, target, _ = box
    assert run([p("A", "1"), p("nested/B", "2"), p("OTHER", "3", prefix="/irb-watch/prod2")]).returncode == 0
    assert target.read_text() == "A='1'\n"


def test_an_aws_failure_leaves_the_old_env_and_says_so(box):
    run, target, _ = box
    result = run([p("A", "1")], fail=True)
    assert result.returncode == 3 and "could not read the parameters" in result.stderr
    assert target.read_text() == "OLD=1\n"


@pytest.mark.parametrize("bad", ["lower_case", "1STARTS_WITH_DIGIT", "HAS-DASH"])
def test_a_bad_setting_name_is_refused_by_name(box, bad):
    run, target, _ = box
    result = run([p(bad, SECRET)])
    assert result.returncode != 0 and bad in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert target.read_text() == "OLD=1\n"


@pytest.mark.parametrize("value", ["it's", "two\nlines", "carriage\rreturn"])
def test_a_value_that_cannot_be_quoted_is_refused_without_printing_it(box, value):
    run, target, _ = box
    result = run([p("GOOD", "fine"), p("BAD_ONE", value)])
    assert result.returncode != 0 and "BAD_ONE" in result.stderr
    assert value not in result.stdout + result.stderr
    assert target.read_text() == "OLD=1\n"  # nothing partial


def test_no_parameters_at_all_is_an_error_and_keeps_the_old_file(box):
    run, target, _ = box
    result = run([])
    assert result.returncode != 0 and "no parameters" in result.stderr
    assert target.read_text() == "OLD=1\n"


def test_the_path_is_required_and_must_look_like_a_path(box):
    run, _, _ = box
    assert run([p("A", "1")], path="").returncode == 2
    assert run([p("A", "1")], path="no-slash").returncode == 2
    assert run([p("A", "1")], path="/ok/$(touch evil)").returncode == 2


def test_the_path_can_be_given_as_the_first_argument(box):
    run, target, _ = box
    assert run([p("A", "1")], path="", args=["/irb-watch/prod"]).returncode == 0
    assert target.read_text() == "A='1'\n"


# ---- deploy.sh decides before it touches git or docker ---------------------------------------


def deploy(command: str):
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": command}
    return subprocess.run(
        [BASH, posix(ROOT / "scripts" / "deploy.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_refuses_anything_but_a_commit_hash_or_refresh():
    for command in ("", "rm -rf /", "REFRESH", "refresh now", "abc123"):
        result = deploy(command)
        assert result.returncode == 2, command
        assert "expected a 40-character commit hash or 'refresh'" in result.stderr


@pytest.mark.skipif((ROOT / ".ssm-path").exists(), reason="this checkout has a .ssm-path")
def test_refresh_without_the_ssm_path_file_stops_with_an_explanation():
    result = deploy("refresh")
    assert result.returncode == 4 and ".ssm-path" in result.stderr
