import pytest

from calculator import add, average, divide


def test_add() -> None:
    assert add(2, 3) == 5


def test_divide() -> None:
    assert divide(6, 3) == 2


def test_divide_by_zero() -> None:
    with pytest.raises(ValueError, match="cannot divide by zero"):
        divide(1, 0)


def test_average() -> None:
    assert average([2, 4, 6]) == 4


def test_average_empty() -> None:
    with pytest.raises(ValueError, match="cannot average an empty sequence"):
        average([])
