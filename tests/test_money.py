import pytest

from app.money import fen_to_yuan, yuan_to_fen


@pytest.mark.parametrize("v,fen", [("15.5", 1550), (33.5, 3350), ("0.1", 10), ("1142.5", 114250), (15, 1500), ("22.1", 2210)])
def test_yuan_to_fen_exact(v, fen):
    assert yuan_to_fen(v) == fen


def test_yuan_to_fen_rounds_half_up():
    assert yuan_to_fen("0.005") == 1
    assert yuan_to_fen("1.004") == 100


@pytest.mark.parametrize("fen,s", [(2210, "22.1"), (600, "6"), (114250, "1142.5"), (5, "0.05")])
def test_fen_to_yuan(fen, s):
    assert fen_to_yuan(fen) == s
