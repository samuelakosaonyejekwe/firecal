"""Test setup: the app reads synthetic data from a temporary folder, never the network or real data."""
import os
import pathlib
import shutil
import tempfile

_DATA = pathlib.Path(tempfile.mkdtemp(prefix="firecal-test-"))
os.environ["FIRECAL_DATA"] = str(_DATA)      # app.main / pipeline read this at import
os.environ["FIRECAL_NO_LIVE"] = "1"          # no NASA downloads from the server's lifespan
os.environ["FIRECAL_NO_GITHUB"] = "1"        # no publishing from tests
os.environ.setdefault("FIRECAL_ALLOWED_HOSTS", "testserver")  # FastAPI's test client's host name


def pytest_configure(config):
    from tests.test_analysis import make_country
    make_country(_DATA, "Nigeria", seed=3)
    make_empty_country(_DATA, "Maldives")


def pytest_unconfigure(config):
    shutil.rmtree(_DATA, ignore_errors=True)  # leave nothing behind in the temp folder


def make_empty_country(root, cid):
    """A processed country with no fire records at all (NASA publishes some, e.g. the Maldives)."""
    import duckdb
    out = root / "countries" / cid
    out.mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"COPY (SELECT * FROM '{root}/countries/Nigeria/grid_daily.parquet' WHERE false) TO '{out}/grid_daily.parquet'")
