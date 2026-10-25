import os

import pytest

# Tests must be fast and offline: always use the hashing embedder, even when the
# optional FastEmbed extra is installed in the developer's environment.
os.environ.setdefault("SUBSTRATE_EMBEDDER", "hashing")

from substrate import embedding  # noqa: E402
from substrate.core import Substrate  # noqa: E402
from substrate.db import Database  # noqa: E402

embedding.reset_embedder()


def _vec_available() -> bool:
    db = Database(":memory:")
    try:
        return db.vec_available
    finally:
        db.close()


# Run the whole suite twice when sqlite-vec is installed: once with the
# accelerated vector backend, once with the brute-force Python scan. Both must
# behave identically from the outside.
_BACKENDS = ["python-scan"] + (["sqlite-vec"] if _vec_available() else [])


@pytest.fixture(params=_BACKENDS)
def vector_backend(request, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_VEC", "auto" if request.param == "sqlite-vec" else "off")
    return request.param


@pytest.fixture
def sub(vector_backend):
    s = Substrate(workspace="test", db_path=":memory:")
    yield s
    s.close()
