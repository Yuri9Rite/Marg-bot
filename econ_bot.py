"""Marg-bot — Telegram-бот для расчёта микроэкономических показателей.

Поддерживает два режима запуска:
  * polling (по умолчанию, локальная разработка):  python econ_bot.py
  * webhook (деплой на Render / Docker):           uvicorn econ_bot:app --host 0.0.0.0 --port $PORT

Переменные окружения:
  BOT_TOKEN          — токен Telegram-бота (обязательно)
  WEBHOOK_SECRET     — секрет для защиты вебхука (обязательно в webhook-режиме)
  RENDER_EXTERNAL_URL — публичный URL сервиса (обязательно в webhook-режиме)
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx логирует каждый GET/POST запрос к Telegram API — приглушаем
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
(
    MENU,
    BREAK_EVEN_FIXED_COSTS, BREAK_EVEN_VARIABLE_COST, BREAK_EVEN_PRICE,
    MARGIN_REVENUE, MARGIN_COST_OF_GOODS,
    ROI_PROFIT, ROI_COST,
    UNIT_COST_FIXED_COSTS, UNIT_COST_VARIABLE_COSTS, UNIT_COST_UNITS,
    ELASTICITY_QUANTITY_CHANGE, ELASTICITY_PRICE_CHANGE,
    PROFIT_LOSS_REVENUE, PROFIT_LOSS_COSTS,
    AVG_MARGINAL_COST_CURRENT_TOTAL_COST, AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST,
    AVG_MARGINAL_COST_CURRENT_QUANTITY, AVG_MARGINAL_COST_PREVIOUS_QUANTITY,
    MARKUP_DISCOUNT_PRICE, MARKUP_DISCOUNT_PERCENTAGE, MARKUP_DISCOUNT_TYPE,
    PAYBACK_INVESTMENT, PAYBACK_CASH_FLOW,
    OPERATING_LEVERAGE_REVENUE, OPERATING_LEVERAGE_VARIABLE_COSTS, OPERATING_LEVERAGE_FIXED_COSTS,
    NPV_INITIAL_INVESTMENT, NPV_CASH_FLOW_YEAR1, NPV_DISCOUNT_RATE,
    OPTIMAL_PRICE_UNIT_COST, OPTIMAL_PRICE_MARKUP,
) = range(32)

MAX_HISTORY_ENTRIES = 50          # максимум записей истории на пользователя
MAX_MESSAGE_LENGTH = 4096         # лимит длины сообщения Telegram

NOT_A_NUMBER_MSG = (
    "⚠️ Пожалуйста, введите корректное число "
    "(например: 100, 12.5, 3.14 или -7)."
)

# ---------------------------------------------------------------------------
# Calculation helpers
# ---------------------------------------------------------------------------

def parse_number(text: str) -> Optional[float]:
    """Безопасно преобразует пользовательский ввод в float."""
    cleaned = text.strip().replace("\u00a0", " ").replace(",", ".", 1) \
        if text.strip().count(",") == 1 and "." not in text else text.strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def calculate_break_even(fixed_costs: float, variable_cost_per_unit: float,
                         price_per_unit: float):
    """Точка безубыточности: (единицы, выручка) или (None, None)."""
    if (price_per_unit - variable_cost_per_unit) <= 0:
        return None, None  # нулевая или отрицательная маржинальность
    break_even_units = fixed_costs / (price_per_unit - variable_cost_per_unit)
    break_even_revenue = break_even_units * price_per_unit
    return break_even_units, break_even_revenue


def calculate_margin(revenue: float, cost_of_goods: float):
    """Маржинальность: (абсолютная, %)."""
    if revenue == 0:
        return None, None
    absolute_margin = revenue - cost_of_goods
    percentage_margin = (absolute_margin / revenue) * 100
    return absolute_margin, percentage_margin


def calculate_roi(profit_from_investment: float, cost_of_investment: float):
    if cost_of_investment == 0:
        return None
    return (profit_from_investment / cost_of_investment) * 100


def calculate_unit_cost(total_fixed_costs: float, total_variable_costs: float,
                        number_of_units: float):
    if number_of_units == 0:
        return None
    return (total_fixed_costs + total_variable_costs) / number_of_units


def calculate_elasticity(quantity_change: float, price_change: float):
    """Эластичность спроса: (коэффициент, интерпретация)."""
    if price_change == 0:
        return None, "Невозможно рассчитать эластичность при нулевом изменении цены."
    elasticity = quantity_change / price_change
    if elasticity < -1:
        interpretation = "Эластичный спрос (значительное изменение спроса при изменении цены)"
    elif -1 < elasticity < 0:
        interpretation = "Неэластичный спрос (незначительное изменение спроса при изменении цены)"
    elif elasticity == -1:
        interpretation = "Единичная эластичность (изменение спроса пропорционально изменению цены)"
    elif elasticity == 0:
        interpretation = "Совершенно неэластичный спрос (спрос не меняется при изменении цены)"
    else:
        interpretation = "Товар Гиффена или Веблена (редкий случай, когда спрос растет с ростом цены)"
    return elasticity, interpretation


def calculate_profit_loss(total_revenue: float, total_costs: float):
    profit_loss = total_revenue - total_costs
    status = "Прибыль" if profit_loss >= 0 else "Убыток"
    return profit_loss, status


def calculate_avg_marginal_costs(current_total_cost: float, previous_total_cost: float,
                                current_quantity: float, previous_quantity: float):
    """Средние и предельные издержки: (avg, marginal)."""
    if current_quantity == 0:
        return None, None
    avg_cost = current_total_cost / current_quantity
    if (current_quantity - previous_quantity) == 0:
        return avg_cost, None  # деление на ноль невозможно
    marginal_cost = (current_total_cost - previous_total_cost) / (current_quantity - previous_quantity)
    return avg_cost, marginal_cost


def calculate_markup_discount(base_price: float, percentage: float, is_discount: bool) -> float:
    if is_discount:
        return base_price * (1 - percentage / 100)
    return base_price * (1 + percentage / 100)


def calculate_payback_period(initial_investment: float, annual_cash_flow: float):
    """Срок окупаемости: (годы, месяцы)."""
    if annual_cash_flow <= 0:
        return None, None
    payback_years = initial_investment / annual_cash_flow
    years = int(payback_years)
    months = round((payback_years - years) * 12)
    if months == 12:  # защита от округления до 12 месяцев
        years, months = years + 1, 0
    return years, months


def calculate_operating_leverage(total_revenue: float, variable_costs: float,
                                 fixed_costs: float):
    contribution_margin = total_revenue - variable_costs
    operating_income = contribution_margin - fixed_costs
    if contribution_margin == 0 or operating_income == 0:
        return None
    return contribution_margin / operating_income


def calculate_npv(initial_investment: float, cash_flow_year1: float,
                  discount_rate: float):
    """Упрощённый NPV для одного года денежных потоков."""
    if discount_rate <= -100:
        return None
    discount_factor = 1 + discount_rate / 100
    return cash_flow_year1 / discount_factor - initial_investment


def calculate_optimal_price(unit_cost: float, desired_markup_percentage: float):
    if desired_markup_percentage <= -100:
        return None
    return unit_cost * (1 + desired_markup_percentage / 100)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

MENU_ITEMS = [
    ("break_even", "📊 Точка безубыточности"),
    ("margin", "💰 Маржинальность"),
    ("roi", "📈 ROI"),
    ("unit_cost", "🏭 Себестоимость единицы"),
    ("elasticity", "🔄 Эластичность спроса"),
    ("profit_loss", "⚖️ Прибыль / убыток"),
    ("avg_marginal_costs", "📉 Средние и предельные издержки"),
    ("markup_discount", "🏷️ Наценка и скидка"),
    ("payback_period", "⏳ Срок окупаемости"),
    ("operating_leverage", "🔧 Операционный рычаг"),
    ("npv", "🧮 Расчёт NPV"),
    ("optimal_price", "🎯 Расчёт оптимальной цены"),
    ("history", "🕘 История расчётов"),
]


def main_menu_markup() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(label, callback_data=f"={cb}")]
                for cb, label in MENU_ITEMS]
    return InlineKeyboardMarkup(keyboard)


def back_to_menu_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Главное меню", callback_data="=main_menu")]]
    )


def add_history(context: ContextTypes.DEFAULT_TYPE, result_text: str) -> None:
    """Сохраняет результат расчёта в историю пользователя (ограничена по размеру)."""
    history: deque = context.user_data.setdefault("history", deque(maxlen=MAX_HISTORY_ENTRIES))
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    calc_name = context.user_data.get("current_calculation", "Расчёт")
    # В историю сохраняем plain-текст первой строки результата (без HTML-разметки)
    first_line = result_text.splitlines()[0] if result_text else calc_name
    summary = re.sub(r"</?b>", "", first_line).lstrip("✅ ").strip() or calc_name
    history.append(f"[{timestamp}] {calc_name}: {summary}")


async def send_result(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      result_text: str) -> None:
    """Отправляет результат расчёта с кнопкой возврата в меню."""
    add_history(context, result_text)
    await update.message.reply_text(
        result_text, reply_markup=back_to_menu_button()
    )


async def safe_edit_message(query, text: str, reply_markup=None) -> None:
    """Редактирует сообщение, игнорируя ошибку «message is not modified»."""
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


# ---------------------------------------------------------------------------
# Entry / navigation handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Приветствие и главное меню."""
    # Инициализация истории (deque со встроенным ограничением размера)
    if "history" not in context.user_data:
        context.user_data["history"] = deque(maxlen=MAX_HISTORY_ENTRIES)

    welcome = (
        "👋 Привет! Я <b>Marg-bot</b> — помощник для расчёта "
        "микроэкономических показателей.\n\n"
        "Выбери нужный расчёт:"
    )
    await update.message.reply_text(
        welcome, reply_markup=main_menu_markup(), parse_mode=ParseMode.HTML
    )
    return MENU


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Краткая справка."""
    text = (
        "ℹ️ <b>Marg-bot</b>\n\n"
        "Я считаю основные экономические показатели по вашим данным:\n"
        "• точка безубыточности, маржинальность, ROI\n"
        "• себестоимость, эластичность спроса, прибыль/убыток\n"
        "• средние и предельные издержки, наценка/скидка\n"
        "• срок окупаемости, операционный рычаг, NPV, оптимальная цена\n\n"
        f"Команды:\n/start — главное меню\n/help — эта справка\n"
        "/cancel — прервать текущий расчёт\n\n"
        "История расчётов хранится только в текущей сессии бота."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    return MENU


async def show_menu_from_callback(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает главное меню по кнопке."""
    query = update.callback_query
    await query.answer()
    await safe_edit_message(query, "Выбери нужный расчёт:",
                             reply_markup=main_menu_markup())
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Прерывает текущий расчёт и возвращает в меню."""
    user = update.effective_user
    logger.info("User %s (id=%s) canceled the conversation.",
                user.first_name if user else "?", user.id if user else "?")
    await update.message.reply_text(
        "❌ Расчёт отменён. Возвращайся в меню командой /start 👇",
        reply_markup=main_menu_markup(),
    )
    return MENU


# ---------------------------------------------------------------------------
# Break-even point
# ---------------------------------------------------------------------------

async def break_even_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Точка безубыточности"
    await safe_edit_message(
        query,
        "📊 <b>Точка безубыточности</b>\n\nВведите постоянные издержки:",
        reply_markup=back_to_menu_button(),
    )
    return BREAK_EVEN_FIXED_COSTS


async def break_even_fixed_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return BREAK_EVEN_FIXED_COSTS
    context.user_data["fixed_costs"] = value
    await update.message.reply_text("Введите переменные издержки на единицу продукции:")
    return BREAK_EVEN_VARIABLE_COST


async def break_even_variable_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return BREAK_EVEN_VARIABLE_COST
    context.user_data["variable_cost_per_unit"] = value
    await update.message.reply_text("Введите цену за единицу продукции:")
    return BREAK_EVEN_PRICE


async def break_even_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return BREAK_EVEN_PRICE
    context.user_data["price_per_unit"] = value

    fixed_costs = context.user_data["fixed_costs"]
    variable_cost_per_unit = context.user_data["variable_cost_per_unit"]
    price_per_unit = context.user_data["price_per_unit"]

    units, revenue = calculate_break_even(fixed_costs, variable_cost_per_unit, price_per_unit)
    if units is None:
        await update.message.reply_text(
            "⚠️ Ошибка: цена за единицу должна быть больше переменных издержек "
            "на единицу. Начнём расчёт заново — введите постоянные издержки:"
        )
        return BREAK_EVEN_FIXED_COSTS

    explanation = (
        f"Это означает, что вам нужно продать минимум {units:.2f} единиц, чтобы "
        f"покрыть все расходы. Всё, что продано сверх этого — ваша прибыль."
    )
    result_text = (
        "✅ <b>Точка безубыточности:</b>\n"
        f"В единицах: {units:.2f}\n"
        f"В выручке: {revenue:.2f}\n\n{explanation}"
    )
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Margin
# ---------------------------------------------------------------------------

async def margin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Маржинальность"
    await safe_edit_message(
        query, "💰 <b>Маржинальность</b>\n\nВведите выручку:",
        reply_markup=back_to_menu_button(),
    )
    return MARGIN_REVENUE


async def margin_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return MARGIN_REVENUE
    context.user_data["revenue"] = value
    await update.message.reply_text("Введите себестоимость товаров:")
    return MARGIN_COST_OF_GOODS


async def margin_cost_of_goods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return MARGIN_COST_OF_GOODS
    context.user_data["cost_of_goods"] = value

    revenue = context.user_data["revenue"]
    cost_of_goods = context.user_data["cost_of_goods"]
    absolute_margin, percentage_margin = calculate_margin(revenue, cost_of_goods)

    if absolute_margin is None:
        await update.message.reply_text(
            "⚠️ Ошибка: выручка не может быть равна нулю. "
            "Начнём заново — введите выручку:"
        )
        return MARGIN_REVENUE

    if percentage_margin >= 50:
        explanation = "Это отличный показатель, указывающий на высокую прибыльность вашего продукта или услуги."
    elif percentage_margin >= 20:
        explanation = "Это хороший показатель, свидетельствующий о здоровой прибыльности."
    else:
        explanation = "Это низкий показатель, возможно, стоит пересмотреть ценообразование или издержки."

    result_text = (
        "✅ <b>Маржинальность:</b>\n"
        f"Абсолютное значение: {absolute_margin:.2f}\n"
        f"В процентах: {percentage_margin:.2f}%\n\n{explanation}"
    )
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# ROI
# ---------------------------------------------------------------------------

async def roi_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "ROI"
    await safe_edit_message(
        query, "📈 <b>ROI</b>\n\nВведите прибыль от инвестиций:",
        reply_markup=back_to_menu_button(),
    )
    return ROI_PROFIT


async def roi_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return ROI_PROFIT
    context.user_data["profit_from_investment"] = value
    await update.message.reply_text("Введите стоимость инвестиций:")
    return ROI_COST


async def roi_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return ROI_COST
    context.user_data["cost_of_investment"] = value

    profit_from_investment = context.user_data["profit_from_investment"]
    cost_of_investment = context.user_data["cost_of_investment"]
    roi = calculate_roi(profit_from_investment, cost_of_investment)

    if roi is None:
        await update.message.reply_text(
            "⚠️ Ошибка: стоимость инвестиций не может быть равна нулю. "
            "Начнём заново — введите прибыль от инвестиций:"
        )
        return ROI_PROFIT

    if roi >= 100:
        explanation = (f"ROI {roi:.2f}% означает, что на каждый вложенный рубль вы "
                       f"получаете {1 + roi / 100:.2f} руб. вместе с вложениями. "
                       f"Это отличный показатель.")
    elif roi >= 0:
        explanation = (f"ROI {roi:.2f}% означает, что на каждый вложенный рубль вы "
                       f"получаете {1 + roi / 100:.2f} руб. вместе с вложениями. "
                       f"Это хороший показатель.")
    else:
        explanation = (f"Отрицательный ROI {roi:.2f}% означает, что ваши инвестиции "
                       f"убыточны. Возможно, стоит пересмотреть стратегию.")

    result_text = f"✅ <b>ROI: {roi:.2f}%</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Unit cost
# ---------------------------------------------------------------------------

async def unit_cost_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Себестоимость единицы продукции"
    await safe_edit_message(
        query, "🏭 <b>Себестоимость единицы продукции</b>\n\nВведите общие постоянные издержки:",
        reply_markup=back_to_menu_button(),
    )
    return UNIT_COST_FIXED_COSTS


async def unit_cost_fixed_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return UNIT_COST_FIXED_COSTS
    context.user_data["total_fixed_costs"] = value
    await update.message.reply_text("Введите общие переменные издержки:")
    return UNIT_COST_VARIABLE_COSTS


async def unit_cost_variable_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return UNIT_COST_VARIABLE_COSTS
    context.user_data["total_variable_costs"] = value
    await update.message.reply_text("Введите количество произведённых единиц:")
    return UNIT_COST_UNITS


async def unit_cost_units(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return UNIT_COST_UNITS
    context.user_data["number_of_units"] = value

    unit_cost = calculate_unit_cost(
        context.user_data["total_fixed_costs"],
        context.user_data["total_variable_costs"],
        context.user_data["number_of_units"],
    )
    if unit_cost is None:
        await update.message.reply_text(
            "⚠️ Ошибка: количество произведённых единиц не может быть равно нулю. "
            "Начнём заново — введите общие постоянные издержки:"
        )
        return UNIT_COST_FIXED_COSTS

    explanation = (
        "Это средняя стоимость производства одной единицы продукции. Важно, чтобы "
        "цена продажи была выше этой цифры для получения прибыли."
    )
    result_text = f"✅ <b>Себестоимость единицы: {unit_cost:.2f}</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Elasticity
# ---------------------------------------------------------------------------

async def elasticity_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Эластичность спроса"
    await safe_edit_message(
        query,
        "🔄 <b>Эластичность спроса</b>\n\nВведите процентное изменение спроса "
        "(например, -10 для снижения на 10%):",
        reply_markup=back_to_menu_button(),
    )
    return ELASTICITY_QUANTITY_CHANGE


async def elasticity_quantity_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return ELASTICITY_QUANTITY_CHANGE
    context.user_data["quantity_change"] = value
    await update.message.reply_text("Введите процентное изменение цены (например, 5 для роста на 5%):")
    return ELASTICITY_PRICE_CHANGE


async def elasticity_price_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return ELASTICITY_PRICE_CHANGE
    context.user_data["price_change"] = value

    quantity_change = context.user_data["quantity_change"]
    price_change = context.user_data["price_change"]
    elasticity, interpretation = calculate_elasticity(quantity_change, price_change)

    if elasticity is None:
        await update.message.reply_text(
            f"⚠️ Ошибка: {interpretation} Начнём заново — введите процентное изменение спроса:"
        )
        return ELASTICITY_QUANTITY_CHANGE

    if elasticity < -1:
        explanation = ("Спрос эластичен. Небольшое изменение цены приведёт к значительному "
                       "изменению спроса. Возможно, стоит рассмотреть снижение цены для "
                       "увеличения общего дохода.")
    elif -1 < elasticity < 0:
        explanation = ("Спрос неэластичен. Изменение цены не сильно повлияет на спрос. Вы "
                       "можете рассмотреть повышение цены без существенной потери клиентов.")
    elif elasticity == -1:
        explanation = ("Единичная эластичность. Изменение цены приведёт к пропорциональному "
                       "изменению спроса. Общий доход останется прежним.")
    elif elasticity == 0:
        explanation = ("Спрос совершенно неэластичен. Изменение цены не повлияет на спрос. "
                       "Это редкий случай для товаров первой необходимости.")
    else:
        explanation = ("Товар Гиффена или Веблена. Спрос растёт с ростом цены. Это очень "
                       "редкий случай, характерный для предметов роскоши или низкокачественных "
                       "товаров в определённых условиях.")

    result_text = (
        "✅ <b>Эластичность спроса:</b>\n"
        f"Коэффициент: {elasticity:.2f}\n"
        f"Интерпретация: {interpretation}\n\n{explanation}"
    )
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Profit / loss
# ---------------------------------------------------------------------------

async def profit_loss_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Прибыль/убыток"
    await safe_edit_message(
        query, "⚖️ <b>Прибыль / убыток</b>\n\nВведите общую выручку:",
        reply_markup=back_to_menu_button(),
    )
    return PROFIT_LOSS_REVENUE


async def profit_loss_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return PROFIT_LOSS_REVENUE
    context.user_data["total_revenue"] = value
    await update.message.reply_text("Введите общие затраты:")
    return PROFIT_LOSS_COSTS


async def profit_loss_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return PROFIT_LOSS_COSTS
    context.user_data["total_costs"] = value

    profit_loss, status = calculate_profit_loss(
        context.user_data["total_revenue"], context.user_data["total_costs"]
    )

    if profit_loss > 0:
        explanation = (f"Ваш бизнес получил прибыль в размере {profit_loss:.2f}. Это хороший "
                       f"результат, показывающий эффективность вашей деятельности.")
    elif profit_loss < 0:
        explanation = (f"Ваш бизнес понёс убытки в размере {abs(profit_loss):.2f}. Необходимо "
                       f"проанализировать доходы и расходы для улучшения ситуации.")
    else:
        explanation = ("Ваш бизнес вышел в ноль. Доходы равны расходам. Для роста необходимо "
                       "увеличивать выручку или сокращать издержки.")

    result_text = (
        "✅ <b>Прибыль / убыток:</b>\n"
        f"Сумма: {profit_loss:.2f}\n"
        f"Статус: {status}\n\n{explanation}"
    )
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Average & marginal costs
# ---------------------------------------------------------------------------

async def avg_marginal_costs_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Средние и предельные издержки"
    await safe_edit_message(
        query,
        "📉 <b>Средние и предельные издержки</b>\n\n"
        "Введите общие издержки при текущем объёме производства:",
        reply_markup=back_to_menu_button(),
    )
    return AVG_MARGINAL_COST_CURRENT_TOTAL_COST


async def avg_marginal_costs_current_total_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST
    context.user_data["current_total_cost"] = value
    await update.message.reply_text("Введите общие издержки при предыдущем объёме производства:")
    return AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST


async def avg_marginal_costs_previous_total_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST
    context.user_data["previous_total_cost"] = value
    await update.message.reply_text("Введите текущее количество произведённых единиц:")
    return AVG_MARGINAL_COST_CURRENT_QUANTITY


async def avg_marginal_costs_current_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return AVG_MARGINAL_COST_CURRENT_QUANTITY
    context.user_data["current_quantity"] = value
    await update.message.reply_text("Введите предыдущее количество произведённых единиц:")
    return AVG_MARGINAL_COST_PREVIOUS_QUANTITY


async def avg_marginal_costs_previous_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return AVG_MARGINAL_COST_PREVIOUS_QUANTITY
    context.user_data["previous_quantity"] = value

    avg_cost, marginal_cost = calculate_avg_marginal_costs(
        context.user_data["current_total_cost"],
        context.user_data["previous_total_cost"],
        context.user_data["current_quantity"],
        context.user_data["previous_quantity"],
    )

    if avg_cost is None:
        await update.message.reply_text(
            "⚠️ Ошибка: текущее количество произведённых единиц не может быть равно нулю. "
            "Начнём заново — введите общие издержки при текущем объёме:"
        )
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST

    if marginal_cost is None:
        await update.message.reply_text(
            "⚠️ Ошибка: изменение количества произведённых единиц не может быть равно нулю. "
            "Начнём заново — введите общие издержки при текущем объёме:"
        )
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST

    explanation = (
        "Средние издержки показывают стоимость одной единицы продукции в среднем. "
        "Предельные издержки показывают, сколько стоит производство дополнительной "
        "единицы. Сравнение этих показателей помогает оптимизировать производство."
    )
    result_text = (
        "✅ <b>Издержки:</b>\n"
        f"Средние издержки: {avg_cost:.2f}\n"
        f"Предельные издержки: {marginal_cost:.2f}\n\n{explanation}"
    )
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Markup / discount
# ---------------------------------------------------------------------------

async def markup_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Наценка и скидка"
    keyboard = [
        [InlineKeyboardButton("⬆️ Наценка", callback_data="=markup")],
        [InlineKeyboardButton("⬇️ Скидка", callback_data="=discount")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="=main_menu")],
    ]
    await safe_edit_message(
        query, "🏷️ <b>Наценка и скидка</b>\n\nВыберите тип операции:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return MARKUP_DISCOUNT_TYPE


async def markup_discount_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["is_discount"] = query.data == "=discount"
    action = "Скидка" if context.user_data["is_discount"] else "Наценка"
    await safe_edit_message(
        query, f"{action}\n\nВведите базовую цену:",
        reply_markup=back_to_menu_button(),
    )
    return MARKUP_DISCOUNT_PRICE


async def markup_discount_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return MARKUP_DISCOUNT_PRICE
    context.user_data["base_price"] = value
    await update.message.reply_text("Введите процент (например, 20 для 20%):")
    return MARKUP_DISCOUNT_PERCENTAGE


async def markup_discount_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return MARKUP_DISCOUNT_PERCENTAGE
    context.user_data["percentage"] = value

    base_price = context.user_data["base_price"]
    percentage = context.user_data["percentage"]
    is_discount = context.user_data["is_discount"]
    new_price = calculate_markup_discount(base_price, percentage, is_discount)

    operation_type = "скидка" if is_discount else "наценка"
    explanation = (
        f"После {operation_type} в {percentage:.2f}% от базовой цены {base_price:.2f} "
        f"новая цена составит {new_price:.2f}. Это позволяет вам гибко управлять "
        f"ценообразованием."
    )
    result_text = f"✅ <b>Новая цена после {operation_type}: {new_price:.2f}</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Payback period
# ---------------------------------------------------------------------------

async def payback_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Срок окупаемости"
    await safe_edit_message(
        query, "⏳ <b>Срок окупаемости</b>\n\nВведите начальные инвестиции:",
        reply_markup=back_to_menu_button(),
    )
    return PAYBACK_INVESTMENT


async def payback_investment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return PAYBACK_INVESTMENT
    context.user_data["initial_investment"] = value
    await update.message.reply_text("Введите годовой денежный поток:")
    return PAYBACK_CASH_FLOW


async def payback_cash_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return PAYBACK_CASH_FLOW
    context.user_data["annual_cash_flow"] = value

    years, months = calculate_payback_period(
        context.user_data["initial_investment"], context.user_data["annual_cash_flow"]
    )
    if years is None:
        await update.message.reply_text(
            "⚠️ Ошибка: годовой денежный поток должен быть положительным. "
            "Начнём заново — введите начальные инвестиции:"
        )
        return PAYBACK_INVESTMENT

    explanation = (
        f"Ваши инвестиции окупятся примерно через {years} лет и {months} месяцев. "
        f"Чем короче срок окупаемости, тем быстрее вы вернёте вложенные средства."
    )
    result_text = f"✅ <b>Срок окупаемости: {years} лет и {months} месяцев</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Operating leverage
# ---------------------------------------------------------------------------

async def operating_leverage_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Операционный рычаг"
    await safe_edit_message(
        query, "🔧 <b>Операционный рычаг</b>\n\nВведите общую выручку:",
        reply_markup=back_to_menu_button(),
    )
    return OPERATING_LEVERAGE_REVENUE


async def operating_leverage_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return OPERATING_LEVERAGE_REVENUE
    context.user_data["total_revenue"] = value
    await update.message.reply_text("Введите переменные издержки:")
    return OPERATING_LEVERAGE_VARIABLE_COSTS


async def operating_leverage_variable_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return OPERATING_LEVERAGE_VARIABLE_COSTS
    context.user_data["variable_costs"] = value
    await update.message.reply_text("Введите постоянные издержки:")
    return OPERATING_LEVERAGE_FIXED_COSTS


async def operating_leverage_fixed_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return OPERATING_LEVERAGE_FIXED_COSTS
    context.user_data["fixed_costs"] = value

    operating_leverage = calculate_operating_leverage(
        context.user_data["total_revenue"],
        context.user_data["variable_costs"],
        context.user_data["fixed_costs"],
    )
    if operating_leverage is None:
        await update.message.reply_text(
            "⚠️ Ошибка: невозможно рассчитать операционный рычаг. Убедитесь, что выручка "
            "больше переменных издержек, а операционная прибыль не равна нулю. "
            "Начнём заново — введите общую выручку:"
        )
        return OPERATING_LEVERAGE_REVENUE

    if operating_leverage > 2:
        explanation = ("Высокий операционный рычаг: небольшое изменение выручки приведёт к "
                       "значительному изменению операционной прибыли. Высокий риск, но и "
                       "высокий потенциал роста.")
    elif operating_leverage > 1:
        explanation = ("Умеренный операционный рычаг. Есть чувствительность к изменениям "
                       "выручки, но риски управляемы.")
    else:
        explanation = ("Низкий операционный рычаг. Стабильность прибыли при изменениях выручки, "
                       "но меньший потенциал для быстрого роста.")

    result_text = f"✅ <b>Операционный рычаг: {operating_leverage:.2f}</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# NPV
# ---------------------------------------------------------------------------

async def npv_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Расчёт NPV"
    await safe_edit_message(
        query, "🧮 <b>Расчёт NPV</b>\n\nВведите начальные инвестиции:",
        reply_markup=back_to_menu_button(),
    )
    return NPV_INITIAL_INVESTMENT


async def npv_initial_investment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return NPV_INITIAL_INVESTMENT
    context.user_data["initial_investment"] = value
    await update.message.reply_text("Введите денежный поток за первый год:")
    return NPV_CASH_FLOW_YEAR1


async def npv_cash_flow_year1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return NPV_CASH_FLOW_YEAR1
    context.user_data["cash_flow_year1"] = value
    await update.message.reply_text("Введите ставку дисконтирования (в процентах, например, 10 для 10%):")
    return NPV_DISCOUNT_RATE


async def npv_discount_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return NPV_DISCOUNT_RATE
    context.user_data["discount_rate"] = value

    npv = calculate_npv(
        context.user_data["initial_investment"],
        context.user_data["cash_flow_year1"],
        context.user_data["discount_rate"],
    )
    if npv is None:
        await update.message.reply_text(
            "⚠️ Ошибка: ставка дисконтирования не может быть меньше -100%. "
            "Начнём заново — введите начальные инвестиции:"
        )
        return NPV_INITIAL_INVESTMENT

    if npv > 0:
        explanation = (f"Положительный NPV ({npv:.2f}) означает, что проект выгоден: ожидается, "
                       f"что он принесёт больше денежных средств, чем было вложено, с учётом "
                       f"временной стоимости денег.")
    elif npv < 0:
        explanation = (f"Отрицательный NPV ({npv:.2f}) означает, что проект невыгоден и, вероятно, "
                       f"не окупит вложенные средства с учётом дисконтирования.")
    else:
        explanation = ("NPV равен нулю. Проект окупится, но не принесёт дополнительной прибыли "
                       "сверх требуемой нормы доходности.")

    result_text = f"✅ <b>NPV: {npv:.2f}</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# Optimal price
# ---------------------------------------------------------------------------

async def optimal_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Расчёт оптимальной цены"
    await safe_edit_message(
        query, "🎯 <b>Расчёт оптимальной цены</b>\n\nВведите себестоимость единицы продукции:",
        reply_markup=back_to_menu_button(),
    )
    return OPTIMAL_PRICE_UNIT_COST


async def optimal_price_unit_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return OPTIMAL_PRICE_UNIT_COST
    context.user_data["unit_cost"] = value
    await update.message.reply_text("Введите желаемую наценку (в процентах, например, 20 для 20%):")
    return OPTIMAL_PRICE_MARKUP


async def optimal_price_markup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = parse_number(update.message.text)
    if value is None:
        await update.message.reply_text(NOT_A_NUMBER_MSG)
        return OPTIMAL_PRICE_MARKUP
    context.user_data["desired_markup_percentage"] = value

    optimal_price = calculate_optimal_price(
        context.user_data["unit_cost"], context.user_data["desired_markup_percentage"]
    )
    if optimal_price is None:
        await update.message.reply_text(
            "⚠️ Ошибка: желаемая наценка не может быть меньше -100%. "
            "Начнём заново — введите себестоимость единицы продукции:"
        )
        return OPTIMAL_PRICE_UNIT_COST

    explanation = (
        f"Оптимальная цена {optimal_price:.2f} установлена с учётом вашей себестоимости "
        f"и желаемой наценки. Эта цена должна обеспечить желаемый уровень прибыли."
    )
    result_text = f"✅ <b>Оптимальная цена: {optimal_price:.2f}</b>\n\n{explanation}"
    await send_result(update, context, result_text)
    return MENU


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    history = list(context.user_data.get("history", []))
    if not history:
        text = "🕘 История расчётов пуста."
    else:
        header = "🕘 <b>История расчётов:</b>\n\n"
        separator = "\n———\n"
        # Telegram ограничивает длину сообщения 4096 символами —
        # берём самые свежие записи, пока умещаемся в лимит.
        kept: list[str] = []
        for entry in reversed(history):
            candidate = separator.join([entry] + kept)
            if len(header) + len(candidate) > MAX_MESSAGE_LENGTH:
                break
            kept.insert(0, entry)
        body = separator.join(kept) if kept else "…слишком длинные записи истории…"
        text = header + body

    await safe_edit_message(query, text, reply_markup=back_to_menu_button())
    return MENU


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "😔 Произошла непредвиденная ошибка. Попробуйте ещё раз или отправьте /start."
            )
        except Exception:  # noqa: BLE001 - best-effort notification
            logger.exception("Failed to notify user about the error")


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------

def build_application(token: str) -> Application:
    """Создаёт Telegram-приложение и регистрирует все обработчики."""
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init_setup)
        .post_shutdown(post_shutdown_cleanup)
        .concurrent_updates(True)
        .build()
    )
    application.add_error_handler(on_error)

    menu_callbacks = "^=(?:" + "|".join(cb for cb, _ in MENU_ITEMS) + ")$"

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(break_even_start, pattern=r"^=break_even$"),
                CallbackQueryHandler(margin_start, pattern=r"^=margin$"),
                CallbackQueryHandler(roi_start, pattern=r"^=roi$"),
                CallbackQueryHandler(unit_cost_start, pattern=r"^=unit_cost$"),
                CallbackQueryHandler(elasticity_start, pattern=r"^=elasticity$"),
                CallbackQueryHandler(profit_loss_start, pattern=r"^=profit_loss$"),
                CallbackQueryHandler(avg_marginal_costs_start, pattern=r"^=avg_marginal_costs$"),
                CallbackQueryHandler(markup_discount_start, pattern=r"^=markup_discount$"),
                CallbackQueryHandler(payback_period_start, pattern=r"^=payback_period$"),
                CallbackQueryHandler(operating_leverage_start, pattern=r"^=operating_leverage$"),
                CallbackQueryHandler(npv_start, pattern=r"^=npv$"),
                CallbackQueryHandler(optimal_price_start, pattern=r"^=optimal_price$"),
                CallbackQueryHandler(show_history, pattern=r"^=history$"),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            BREAK_EVEN_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_fixed_costs)],
            BREAK_EVEN_VARIABLE_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_variable_cost)],
            BREAK_EVEN_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_price),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            MARGIN_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, margin_revenue)],
            MARGIN_COST_OF_GOODS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, margin_cost_of_goods),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            ROI_PROFIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, roi_profit)],
            ROI_COST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, roi_cost),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            UNIT_COST_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_fixed_costs)],
            UNIT_COST_VARIABLE_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_variable_costs)],
            UNIT_COST_UNITS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_units),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            ELASTICITY_QUANTITY_CHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, elasticity_quantity_change)],
            ELASTICITY_PRICE_CHANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, elasticity_price_change),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            PROFIT_LOSS_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_loss_revenue)],
            PROFIT_LOSS_COSTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, profit_loss_costs),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            AVG_MARGINAL_COST_CURRENT_TOTAL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_current_total_cost)],
            AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_previous_total_cost)],
            AVG_MARGINAL_COST_CURRENT_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_current_quantity)],
            AVG_MARGINAL_COST_PREVIOUS_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_previous_quantity),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            MARKUP_DISCOUNT_TYPE: [
                CallbackQueryHandler(markup_discount_type, pattern=r"^=(?:markup|discount)$"),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            MARKUP_DISCOUNT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, markup_discount_price)],
            MARKUP_DISCOUNT_PERCENTAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, markup_discount_percentage),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            PAYBACK_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payback_investment)],
            PAYBACK_CASH_FLOW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payback_cash_flow),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            OPERATING_LEVERAGE_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_revenue)],
            OPERATING_LEVERAGE_VARIABLE_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_variable_costs)],
            OPERATING_LEVERAGE_FIXED_COSTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_fixed_costs),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            NPV_INITIAL_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_initial_investment)],
            NPV_CASH_FLOW_YEAR1: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_cash_flow_year1)],
            NPV_DISCOUNT_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, npv_discount_rate),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
            OPTIMAL_PRICE_UNIT_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, optimal_price_unit_cost)],
            OPTIMAL_PRICE_MARKUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, optimal_price_markup),
                CallbackQueryHandler(show_menu_from_callback, pattern=r"^=main_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(show_menu_from_callback, pattern=menu_callbacks),
            MessageHandler(~filters.COMMAND & ~filters.TEXT,
                           unknown_message_handler),
        ],
    )
    application.add_handler(conv_handler)
    return application


async def unknown_message_handler(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> int:
    """Вежливо отвечает на стикеры, фото и прочие нетекстовые сообщения."""
    await update.message.reply_text(
        "🤔 Я понимаю только числа и команды. Отправьте /start, чтобы начать."
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Webhook lifecycle (Render / Docker)
# ---------------------------------------------------------------------------

async def post_init_setup(application: Application) -> None:
    """Регистрирует вебхук и команды бота при старте."""
    secret = os.environ.get("WEBHOOK_SECRET")
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if secret and external_url:
        webhook_url = f"{external_url.rstrip('/')}/telegram/webhook"
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Telegram webhook configured: %s", webhook_url)
    else:
        logger.warning("WEBHOOK_SECRET / RENDER_EXTERNAL_URL are not set — "
                       "webhook will not be registered.")

    commands = [
        ("start", "Главное меню"),
        ("help", "Справка"),
        ("cancel", "Отменить текущий расчёт"),
    ]
    await application.bot.set_my_commands(commands)


async def post_shutdown_cleanup(application: Application) -> None:
    """Убирает вебхук при остановке, чтобы вернуть pending updates."""
    try:
        await application.bot.delete_webhook(drop_pending_updates=False)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to delete webhook during shutdown")


telegram_application: Optional[Application] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Жизненный цикл webhook-приложения (startup/shutdown)."""
    global telegram_application  # noqa: PLW0603

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Environment variable BOT_TOKEN is not set!")

    telegram_application = build_application(token)
    await telegram_application.initialize()
    await telegram_application.start()
    logger.info("Telegram bot initialized in webhook mode")
    yield
    await telegram_application.stop()
    await telegram_application.shutdown()
    logger.info("Telegram bot stopped")


app = FastAPI(title="Marginal Telegram Bot", version="1.1.0", lifespan=lifespan)


@app.get("/")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok", "bot": "Marg-bot"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if telegram_application is None:
        raise HTTPException(status_code=503, detail="Bot is not initialized yet")

    expected_secret = os.environ.get("WEBHOOK_SECRET")
    if not expected_secret or x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    update = Update.de_json(payload, telegram_application.bot)
    await telegram_application.process_update(update)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Polling mode (local development)
# ---------------------------------------------------------------------------

def main() -> None:
    """Запускает бота в режиме long polling."""
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Environment variable BOT_TOKEN is not set!")
    application = build_application(token)
    logger.info("Starting Marg-bot in polling mode")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
