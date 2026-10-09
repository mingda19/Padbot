import os
import subprocess
import sys
from pathlib import Path

from padbot import config

ROOT = Path(__file__).resolve().parents[1]


def test_root_is_the_project_directory():
    assert config.ROOT == ROOT and (ROOT / "pyproject.toml").exists()


def test_relative_paths_are_anchored_to_the_project_not_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.resolve_path("padbot.db") == str(ROOT / "padbot.db")
    assert config.resolve_path("data/raw") == str(ROOT / "data" / "raw")


def test_absolute_paths_and_home_are_respected(tmp_path):
    assert config.resolve_path(str(tmp_path / "x.db")) == str(tmp_path / "x.db")
    assert config.resolve_path("~/pads.db") == str(Path("~/pads.db").expanduser())


def test_defaults_are_the_same_wherever_the_process_is_started(tmp_path):
    """The incident: starting the bot from src/padbot made padbot.db and raw/ resolve there,
    creating a second, empty database. Start a fresh interpreter from a different folder."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADBOT_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    out = subprocess.run(
        [sys.executable, "-c", "from padbot import config; print(config.DB_PATH); print(config.RAW_DIR)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert out.stdout.split("\n")[:2] == [str(ROOT / "padbot.db"), str(ROOT / "raw")]
    assert not list(tmp_path.iterdir())  # and nothing was created in the folder we started from


def test_env_override_is_also_anchored(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADBOT_")}
    env.update(PYTHONPATH=str(ROOT / "src"), PADBOT_DB="elsewhere/pads.db")
    out = subprocess.run(
        [sys.executable, "-c", "from padbot import config; print(config.DB_PATH)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert out.stdout.strip() == str(ROOT / "elsewhere" / "pads.db")
