import pytest

from substrate.core import Substrate


@pytest.fixture
def sub():
    s = Substrate(workspace="test", db_path=":memory:")
    yield s
    s.close()
