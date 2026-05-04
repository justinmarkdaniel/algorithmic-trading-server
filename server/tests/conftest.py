import os
from pathlib import Path

import pytest

# Force tests to disable IP whitelist + the live tick loop.
os.environ["BOT_IP_WHITELIST_DISABLED"] = "1"
os.environ["BOT_DISABLE_TICK"] = "1"
os.environ.setdefault("BOT_LOG_DIR", str(Path(__file__).parent / "_logs"))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
