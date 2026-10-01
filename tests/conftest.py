"""Test setup: the app reads synthetic data from a temporary folder, never the network or real data."""
import os
import pathlib
import tempfile

_DATA = pathlib.Path(tempfile.mkdtemp(prefix="firecal-test-"))
os.environ["FIRECAL_DATA"] = str(_DATA)      # app.main / pipeline read this at import
os.environ["FIRECAL_NO_LIVE"] = "1"          # no NASA downloads from the server's lifespan
os.environ["FIRECAL_NO_GITHUB"] = "1"        # no publishing from tests


def pytest_configure(config):
    from tests.test_analysis import make_country
    make_country(_DATA, "Nigeria", seed=3)
