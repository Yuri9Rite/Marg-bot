"""Unit tests for the pure calculation helpers in econ_bot."""

import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import pytest  # noqa: E402

import econ_bot  # noqa: E402


def test_break_even():
    assert econ_bot.calculate_break_even(1000, 10, 20) == (100.0, 2000.0)
    # price must exceed variable cost
    assert econ_bot.calculate_break_even(1000, 20, 20) == (None, None)
    assert econ_bot.calculate_break_even(1000, 25, 20) == (None, None)


def test_margin():
    assert econ_bot.calculate_margin(200, 100) == (100.0, 50.0)
    assert econ_bot.calculate_margin(0, 100) == (None, None)


def test_roi():
    assert econ_bot.calculate_roi(50, 100) == 50.0
    assert econ_bot.calculate_roi(50, 0) is None


def test_unit_cost():
    assert econ_bot.calculate_unit_cost(100, 200, 30) == 10.0
    assert econ_bot.calculate_unit_cost(100, 200, 0) is None


def test_elasticity():
    e, _ = econ_bot.calculate_elasticity(-20, 10)
    assert e == -2.0
    assert econ_bot.calculate_elasticity(10, 0)[0] is None


def test_profit_loss():
    assert econ_bot.calculate_profit_loss(300, 100) == (200, "Прибыль")
    assert econ_bot.calculate_profit_loss(100, 300) == (-200, "Убыток")


def test_avg_marginal_costs():
    avg, marginal = econ_bot.calculate_avg_marginal_costs(1000, 800, 100, 80)
    assert avg == 10.0
    assert marginal == 10.0
    assert econ_bot.calculate_avg_marginal_costs(1000, 800, 0, 80) == (None, None)
    assert econ_bot.calculate_avg_marginal_costs(1000, 800, 100, 100)[1] is None


def test_markup_discount():
    assert econ_bot.calculate_markup_discount(100, 20, is_discount=False) == 120.0
    assert econ_bot.calculate_markup_discount(100, 20, is_discount=True) == 80.0


def test_payback_period():
    assert econ_bot.calculate_payback_period(1000, 300) == (3, 4)
    assert econ_bot.calculate_payback_period(1000, 0) == (None, None)


def test_operating_leverage():
    assert econ_bot.calculate_operating_leverage(1000, 400, 200) == pytest.approx(1.5)
    assert econ_bot.calculate_operating_leverage(1000, 1000, 100) is None


def test_npv():
    assert econ_bot.calculate_npv(100, 110, 10) == pytest.approx(0.0)
    assert econ_bot.calculate_npv(100, 110, -100) is None


def test_optimal_price():
    assert econ_bot.calculate_optimal_price(100, 20) == 120.0
    assert econ_bot.calculate_optimal_price(100, -100) is None


def test_parse_number_accepts_comma_and_spaces():
    assert econ_bot.parse_number("1 000,5") == 1000.5
    assert econ_bot.parse_number("-3.5") == -3.5
    with pytest.raises(ValueError):
        econ_bot.parse_number("abc")


def test_conversation_handler_has_all_states():
    handler = econ_bot.build_conversation_handler()
    assert len(handler.states) == 33
