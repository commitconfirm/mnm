"""Root pytest configuration for MNM controller tests.

Sets pytest-asyncio mode to "auto" so all async test functions are
automatically treated as asyncio coroutines without needing the
@pytest.mark.asyncio decorator on each one.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "asyncio: mark test as async (handled by pytest-asyncio)",
    )
