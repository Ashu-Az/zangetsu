import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
FIXTURES = Path(__file__).parent / "fixtures"

import pytest
import yaml


@pytest.fixture(autouse=True)
def example_profile(monkeypatch):
    """Tests never read the real profile/profile.yaml: its values would change rule answers."""
    data = yaml.safe_load((Path(__file__).resolve().parent.parent / "profile" / "profile.example.yaml").read_text())
    monkeypatch.setattr("naukri_bot.config.load_profile", lambda: data)
