"""Small calculator module with intentional bugs for the coding-agent demo."""


def add(a: int | float, b: int | float) -> int | float:
    return a + b


def divide(a: int | float, b: int | float) -> float:
    if b == 0:
        return 0
    return a / b


def average(numbers: list[int | float]) -> float:
    return sum(numbers) / len(numbers)
