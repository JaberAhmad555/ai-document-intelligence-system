"""Keep deployed credentials out of isolated test configuration."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolate_demo_access_environment(monkeypatch):
    # Tests choose app_env/test credentials explicitly. Never borrow a real
    # Render/demo credential from the terminal that launches pytest.
    names = {"document_api_token", "document_auth_disabled", "render"}
    for name in list(os.environ):
        if name.casefold() in names:
            monkeypatch.delenv(name, raising=False)
