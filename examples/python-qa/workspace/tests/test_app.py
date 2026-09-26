"""Tests for the python-qa example workspace."""
from src.app import add


def test_add():
    assert add(2, 3) == 5
