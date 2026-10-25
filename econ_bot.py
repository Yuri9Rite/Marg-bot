"""Marg-bot — a Telegram bot for micro-economic calculations.

Deploy-ready entrypoint supporting two modes:

* **Webhook mode** (default for PaaS platforms such as Render/Fly.io/Railway):
  run ``uvicorn econ_bot:app --host 0.0.0.0 --port $PORT``.  The Telegram
  application is initialised inside the ASGI lifespan; the webhook URL is
  derived from ``RENDER_EXTERNAL_URL`` or ``WEBHOOK_URL`` and protected with
  ``WEBHOOK_SECRET``.

* **Polling mode** (local development / containers without a public URL):
  run ``python econ_bot.py`` (or ``python -m econ_bot``).  Set
  ``MODE=polling`` to force polling even under ``uvicorn econ_bot:app``.

Configuration (environment variables):

    BOT_TOKEN            required — Telegram bot token from @BotFather
    WEBHOOK_SECRET       strongly recommended in webhook mode — secret token
                         Telegram echoes back in the
                         ``X-Telegram-Bot-Api-Secret-Token`` header
    RENDER_EXTERNAL_URL  webhook base URL (set automatically by Render)
    WEBHOOK_URL          alternative explicit webhook base URL
    WEBHOOK_PATH         path Telegram posts to (default: /telegram/webhook)
    PORT                 port for uvicorn (handled by the platform)
    MODE                 "webhook" (default) or "polling"
    LOG_LEVEL            logging level (default: INFO)
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Logging & configuration                                                     #
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
# Avoid flooding the log with every GET/POST made by httpx.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("econ_bot")

BOT_TOKEN: str = os.environ["BOT_TOKEN"]  # fail fast if missing
WEBHOOK_SECRET: Optional[str] = os.environ.get("WEBHOOK_SECRET")
WEBHOOK_PATH: str = os.environ.get("WEBHOOK_PATH", "/telegram/webhook")
RUN_MODE: str = os.environ.get("MODE", "webhook").strip().lower()

MAX_HISTORY_ENTRIES = 50  # keep per-user history bounded
TELEGRAM_MESSAGE_LIMIT = 4096

# States for ConversationHandler ---------------------------------------------- #
# fmt: off
(MENU,
 BREAK_EVEN_FIXED_COSTS, BREAK_EVEN_VARIABLE_COST, BREAK_EVEN_PRICE,
 MARGIN_REVENUE, MARGIN_COST_OF_GOODS, ROI_PROFIT, ROI_COST,
 UNIT_COST_FIXED_COSTS, UNIT_COST_VARIABLE_COSTS, UNIT_COST_UNITS,
 ELASTICITY_QUANTITY_CHANGE, ELASTICITY_PRICE_CHANGE,
 PROFIT_LOSS_REVENUE, PROFIT_LOSS_COSTS,
 AVG_MARGINAL_COST_CURRENT_TOTAL_COST, AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST,
 AVG_MARGINAL_COST_CURRENT_QUANTITY, AVG_MARGINAL_COST_PREVIOUS_QUANTITY,
 MARKUP_DISCOUNT_PRICE, MARKUP_DISCOUNT_PERCENTAGE, MARKUP_DISCOUNT_TYPE,
 PAYBACK_INVESTMENT, PAYBACK_CASH_FLOW,
 OPERATING_LEVERAGE_REVENUE, OPERATING_LEVERAGE_VARIABLE_COSTS,
 OPERATING_LEVERAGE_FIXED_COSTS,
 NPV_INITIAL_INVESTMENT, NPV_CASH_FLOW_YEAR1, NPV_DISCOUNT_RATE,
 OPTIMAL_PRICE_UNIT_COST, OPTIMAL_PRICE_MARKUP,
 HISTORY) = range(33)
# fmt: on

# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #


def parse_number(text: str) -> float:
    """Parse a user-supplied number, accepting comma decimal separators."""
    return float(text.strip().replace(",", ".").replace(" ", ""))


MENU_ITEMS: list[tuple[str, str]] = [
    ("Точка безубыточности", "break_even"),
    ("Маржинальность", "margin"),
    ("ROI", "roi"),
    ("Себестоимость единицы продукции", "unit_cost"),
    ("Эластичность спроса", "elasticity"),
    ("Прибыль/убыток", "profit_loss"),
    ("Средние и предельные издержки", "avg_marginal_costs"),
    ("Наценка и скидка", "markup_discount"),
    ("Срок окупаемости", "payback_period"),
    ("Операционный рычаг", "operating_leverage"),
    ("Расчёт NPV", "npv"),
    ("Расчёт оптимальной цены", "optimal_price"),
    ("История расчётов", "history"),
]


def main_menu_markup() -> InlineKeyboardMarkup:
    """Inline keyboard with every available calculation."""
    rows = [[InlineKeyboardButton(label, callback_data=cb)] for label, cb in MENU_ITEMS]
    return InlineKeyboardMarkup(rows)


def result_markup() -> InlineKeyboardMarkup:
    """Keyboard shown underneath a calculation result."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
    )


def record_result(context: ContextTypes.DEFAULT_TYPE, result_text: str) -> None:
    """Append a timestamped one-line summary to the bounded per-user history."""
    history: list[str] = context.user_data.setdefault("history", [])
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    name = context.user_data.get("current_calculation", "Расчёт")
    # Collapse newlines so each entry stays compact in the history view.
    summary = " | ".join(line.strip() for line in result_text.splitlines() if line.strip())
    history.append(f"[{timestamp}] {name}: {summary}")
    del history[:-MAX_HISTORY_ENTRIES]


async def send_long_message(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Reply with text, splitting safely if it exceeds Telegram's limit."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        cut = len(remaining)
        if cut > TELEGRAM_MESSAGE_LIMIT:
            cut = remaining.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)
            if cut <= 0:
                cut = TELEGRAM_MESSAGE_LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    for i, chunk in enumerate(chunks):
        await update.message.reply_text(
            chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None
        )


async def safe_edit(query, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """``edit_message_text`` that ignores 'message is not modified' errors."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def numeric_state_handler(callback: Callable[..., Awaitable[int]]) -> MessageHandler:
    """Wrap a state callback so unexpected errors end the conversation
    gracefully instead of silently stalling the user."""

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            return await callback(update, context)
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("Unhandled error in conversation state")
            await update.message.reply_text(
                "Что-то пошло не так. Расчёт сброшен — набери /start, чтобы начать заново."
            )
            return ConversationHandler.END

    return MessageHandler(filters.TEXT & ~filters.COMMAND, wrapped)


# --------------------------------------------------------------------------- #
# Calculation functions (pure — easy to unit-test)                            #
# --------------------------------------------------------------------------- #


def calculate_break_even(fixed_costs: float, variable_cost_per_unit: float, price_per_unit: float):
    if (price_per_unit - variable_cost_per_unit) <= 0:
        return None, None  # avoid division by zero / negative contribution margin
    break_even_units = fixed_costs / (price_per_unit - variable_cost_per_unit)
    break_even_revenue = break_even_units * price_per_unit
    return break_even_units, break_even_revenue


def calculate_margin(revenue: float, cost_of_goods: float):
    if revenue == 0:
        return None, None
    absolute_margin = revenue - cost_of_goods
    percentage_margin = (absolute_margin / revenue) * 100
    return absolute_margin, percentage_margin


def calculate_roi(profit_from_investment: float, cost_of_investment: float) -> Optional[float]:
    if cost_of_investment == 0:
        return None
    return (profit_from_investment / cost_of_investment) * 100


def calculate_unit_cost(total_fixed_costs: float, total_variable_costs: float,
                        number_of_units: float) -> Optional[float]:
    if number_of_units == 0:
        return None
    return (total_fixed_costs + total_variable_costs) / number_of_units


def calculate_elasticity(quantity_change: float, price_change: float):
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
    if current_quantity == 0:
        return None, None
    avg_cost = current_total_cost / current_quantity
    if (current_quantity - previous_quantity) == 0:
        marginal_cost = None  # avoid division by zero
    else:
        marginal_cost = (current_total_cost - previous_total_cost) / (current_quantity - previous_quantity)
    return avg_cost, marginal_cost


def calculate_markup_discount(base_price: float, percentage: float, is_discount: bool) -> float:
    factor = (1 - percentage / 100) if is_discount else (1 + percentage / 100)
    return base_price * factor


def calculate_payback_period(initial_investment: float, annual_cash_flow: float):
    if annual_cash_flow <= 0:
        return None, None
    payback_years = initial_investment / annual_cash_flow
    years = int(payback_years)
    months = round((payback_years - years) * 12)
    return years, months


def calculate_operating_leverage(total_revenue: float, variable_costs: float,
                                 fixed_costs: float) -> Optional[float]:
    contribution_margin = total_revenue - variable_costs
    if contribution_margin == 0:
        return None
    operating_income = contribution_margin - fixed_costs
    if operating_income == 0:
        return None
    return contribution_margin / operating_income


def calculate_npv(initial_investment: float, cash_flow_year1: float,
                  discount_rate: float) -> Optional[float]:
    if discount_rate <= -100:
        return None  # discount rate cannot be -100% or less
    discount_factor = 1 + (discount_rate / 100)
    return (cash_flow_year1 / discount_factor) - initial_investment


def calculate_optimal_price(unit_cost: float, desired_markup_percentage: float) -> Optional[float]:
    if desired_markup_percentage <= -100:
        return None  # markup cannot be -100% or less
    return unit_cost * (1 + desired_markup_percentage / 100)


# --------------------------------------------------------------------------- #
# Menu / generic handlers                                                     #
# --------------------------------------------------------------------------- #


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a welcome message and show the main menu."""
    user = update.effective_user
    greeting = f"Привет, {user.first_name}!" if user else "Привет!"
    await update.message.reply_text(
        f"{greeting}\nЯ бот для расчёта микроэкономических показателей. Выбери нужный расчёт:",
        reply_markup=main_menu_markup(),
    )
    context.user_data.setdefault("history", [])
    return MENU


async def show_menu_from_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display the main menu in answer to a callback (also used as 'back')."""
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "Выбери нужный расчёт:", reply_markup=main_menu_markup())
    return MENU


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain what the bot does and list its commands."""
    if update.message:
        await update.message.reply_text(
            "Я считаю микроэкономические показатели.\n\n"
            "Команды:\n"
            "/start — главное меню\n"
            "/help — эта справка\n"
            "/cancel — отменить текущий расчёт\n\n"
            "Выбери расчёт кнопками в меню и следуй подсказкам."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current calculation and end the conversation."""
    if update.message:
        await update.message.reply_text("Расчёт отменён. Набери /start, чтобы вернуться в меню.")
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await safe_edit(query, "Расчёт отменён. Набери /start, чтобы вернуться в меню.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Break-even handlers                                                         #
# --------------------------------------------------------------------------- #


async def break_even_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Точка безубыточности"
    await query.edit_message_text("Введите постоянные издержки:")
    return BREAK_EVEN_FIXED_COSTS


async def break_even_fixed_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["fixed_costs"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return BREAK_EVEN_FIXED_COSTS
    await update.message.reply_text("Введите переменные издержки на единицу продукции:")
    return BREAK_EVEN_VARIABLE_COST


async def break_even_variable_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["variable_cost_per_unit"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return BREAK_EVEN_VARIABLE_COST
    await update.message.reply_text("Введите цену за единицу продукции:")
    return BREAK_EVEN_PRICE


async def break_even_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price_per_unit = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return BREAK_EVEN_PRICE

    units, revenue = calculate_break_even(
        context.user_data["fixed_costs"], context.user_data["variable_cost_per_unit"], price_per_unit
    )
    if units is None:
        await update.message.reply_text(
            "Ошибка: Цена за единицу должна быть больше переменных издержек на единицу продукции. "
            "Пожалуйста, попробуйте снова."
        )
        return BREAK_EVEN_FIXED_COSTS  # restart the calculation

    explanation = (
        f"Это означает, что вам нужно продать минимум {units:.2f} единиц, чтобы покрыть все расходы. "
        f"Всё, что продано сверх этого — ваша прибыль."
    )
    result_text = (
        f"Точка безубыточности:\n"
        f"В единицах: {units:.2f}\n"
        f"В выручке: {revenue:.2f}\n\n{explanation}"
    )
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Margin handlers                                                             #
# --------------------------------------------------------------------------- #


async def margin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Маржинальность"
    await query.edit_message_text("Введите выручку:")
    return MARGIN_REVENUE


async def margin_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["revenue"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARGIN_REVENUE
    await update.message.reply_text("Введите себестоимость товаров:")
    return MARGIN_COST_OF_GOODS


async def margin_cost_of_goods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        cost_of_goods = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARGIN_COST_OF_GOODS

    absolute_margin, percentage_margin = calculate_margin(context.user_data["revenue"], cost_of_goods)
    if absolute_margin is None:
        await update.message.reply_text(
            "Ошибка: Выручка не может быть равна нулю. Пожалуйста, попробуйте снова."
        )
        return MARGIN_REVENUE  # restart the calculation

    if percentage_margin >= 50:
        explanation = "Это отличный показатель, указывающий на высокую прибыльность вашего продукта или услуги."
    elif percentage_margin >= 20:
        explanation = "Это хороший показатель, свидетельствующий о здоровой прибыльности."
    else:
        explanation = "Это низкий показатель, возможно, стоит пересмотреть ценообразование или издержки."

    result_text = (
        f"Маржинальность:\n"
        f"Абсолютное значение: {absolute_margin:.2f}\n"
        f"В процентах: {percentage_margin:.2f}%\n\n{explanation}"
    )
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# ROI handlers                                                                #
# --------------------------------------------------------------------------- #


async def roi_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "ROI"
    await query.edit_message_text("Введите прибыль от инвестиций:")
    return ROI_PROFIT


async def roi_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["profit_from_investment"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ROI_PROFIT
    await update.message.reply_text("Введите стоимость инвестиций:")
    return ROI_COST


async def roi_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        cost_of_investment = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ROI_COST

    roi = calculate_roi(context.user_data["profit_from_investment"], cost_of_investment)
    if roi is None:
        await update.message.reply_text(
            "Ошибка: Стоимость инвестиций не может быть равна нулю. Пожалуйста, попробуйте снова."
        )
        return ROI_PROFIT  # restart the calculation

    if roi >= 0:
        quality = "отличный" if roi >= 100 else "хороший"
        explanation = (
            f"ROI {roi:.2f}% означает, что на каждый вложенный рубль вы получаете "
            f"{1 + roi / 100:.2f} руб. вместе с вложением. Это {quality} показатель."
        )
    else:
        explanation = (
            f"Отрицательный ROI {roi:.2f}% означает, что ваши инвестиции убыточны. "
            f"Возможно, стоит пересмотреть стратегию."
        )

    result_text = f"ROI: {roi:.2f}%\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Unit-cost handlers                                                          #
# --------------------------------------------------------------------------- #


async def unit_cost_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Себестоимость единицы продукции"
    await query.edit_message_text("Введите общие постоянные издержки:")
    return UNIT_COST_FIXED_COSTS


async def unit_cost_fixed_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["total_fixed_costs"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return UNIT_COST_FIXED_COSTS
    await update.message.reply_text("Введите общие переменные издержки:")
    return UNIT_COST_VARIABLE_COSTS


async def unit_cost_variable_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["total_variable_costs"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return UNIT_COST_VARIABLE_COSTS
    await update.message.reply_text("Введите количество произведенных единиц:")
    return UNIT_COST_UNITS


async def unit_cost_units(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        number_of_units = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return UNIT_COST_UNITS

    unit_cost = calculate_unit_cost(
        context.user_data["total_fixed_costs"],
        context.user_data["total_variable_costs"],
        number_of_units,
    )
    if unit_cost is None:
        await update.message.reply_text(
            "Ошибка: Количество произведенных единиц не может быть равно нулю. "
            "Пожалуйста, попробуйте снова."
        )
        return UNIT_COST_FIXED_COSTS  # restart the calculation

    explanation = (
        "Это средняя стоимость производства одной единицы продукции. "
        "Важно, чтобы цена продажи была выше этой цифры для получения прибыли."
    )
    result_text = f"Себестоимость единицы продукции: {unit_cost:.2f}\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Elasticity handlers                                                         #
# --------------------------------------------------------------------------- #


async def elasticity_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Эластичность спроса"
    await query.edit_message_text(
        "Введите процентное изменение спроса (например, -10 для снижения на 10%):"
    )
    return ELASTICITY_QUANTITY_CHANGE


async def elasticity_quantity_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["quantity_change"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ELASTICITY_QUANTITY_CHANGE
    await update.message.reply_text(
        "Введите процентное изменение цены (например, 5 для роста на 5%):"
    )
    return ELASTICITY_PRICE_CHANGE


async def elasticity_price_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price_change = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ELASTICITY_PRICE_CHANGE

    elasticity, interpretation = calculate_elasticity(
        context.user_data["quantity_change"], price_change
    )
    if elasticity is None:
        await update.message.reply_text(f"Ошибка: {interpretation} Пожалуйста, попробуйте снова.")
        return ELASTICITY_QUANTITY_CHANGE  # restart the calculation

    if elasticity < -1:
        explanation = (
            "Спрос эластичен. Небольшое изменение цены приведет к значительному изменению спроса. "
            "Возможно, стоит рассмотреть снижение цены для увеличения общего дохода."
        )
    elif -1 < elasticity < 0:
        explanation = (
            "Спрос неэластичен. Изменение цены не сильно повлияет на спрос. "
            "Вы можете рассмотреть возможность повышения цены без существенной потери клиентов."
        )
    elif elasticity == -1:
        explanation = (
            "Единичная эластичность. Изменение цены приведет к пропорциональному изменению спроса. "
            "Общий доход останется прежним."
        )
    elif elasticity == 0:
        explanation = (
            "Спрос совершенно неэластичен. Изменение цены не повлияет на спрос. "
            "Это редкий случай для товаров первой необходимости."
        )
    else:
        explanation = (
            "Товар Гиффена или Веблена. Спрос растет с ростом цены. Это очень редкий случай, "
            "характерный для предметов роскоши или низкокачественных товаров в определенных условиях."
        )

    result_text = (
        f"Эластичность спроса:\n"
        f"Коэффициент: {elasticity:.2f}\n"
        f"Интерпретация: {interpretation}\n\n{explanation}"
    )
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Profit/loss handlers                                                        #
# --------------------------------------------------------------------------- #


async def profit_loss_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Прибыль/убыток"
    await query.edit_message_text("Введите общую выручку:")
    return PROFIT_LOSS_REVENUE


async def profit_loss_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["total_revenue"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PROFIT_LOSS_REVENUE
    await update.message.reply_text("Введите общие затраты:")
    return PROFIT_LOSS_COSTS


async def profit_loss_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        total_costs = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PROFIT_LOSS_COSTS

    profit_loss, status = calculate_profit_loss(context.user_data["total_revenue"], total_costs)

    if profit_loss > 0:
        explanation = (
            f"Ваш бизнес получил прибыль в размере {profit_loss:.2f}. Это хороший результат, "
            f"показывающий эффективность вашей деятельности."
        )
    elif profit_loss < 0:
        explanation = (
            f"Ваш бизнес понес убытки в размере {abs(profit_loss):.2f}. Необходимо проанализировать "
            f"доходы и расходы для улучшения ситуации."
        )
    else:
        explanation = (
            "Ваш бизнес вышел в ноль. Доходы равны расходам. Для роста необходимо увеличивать "
            "выручку или сокращать издержки."
        )

    result_text = (
        f"Прибыль/убыток:\n"
        f"Сумма: {profit_loss:.2f}\n"
        f"Статус: {status}\n\n{explanation}"
    )
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Average & marginal cost handlers                                            #
# --------------------------------------------------------------------------- #


async def avg_marginal_costs_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Средние и предельные издержки"
    await query.edit_message_text("Введите общие издержки при текущем объеме производства:")
    return AVG_MARGINAL_COST_CURRENT_TOTAL_COST


async def avg_marginal_costs_current_total_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["current_total_cost"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST
    await update.message.reply_text("Введите общие издержки при предыдущем объеме производства:")
    return AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST


async def avg_marginal_costs_previous_total_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["previous_total_cost"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST
    await update.message.reply_text("Введите текущее количество произведенных единиц:")
    return AVG_MARGINAL_COST_CURRENT_QUANTITY


async def avg_marginal_costs_current_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["current_quantity"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_CURRENT_QUANTITY
    await update.message.reply_text("Введите предыдущее количество произведенных единиц:")
    return AVG_MARGINAL_COST_PREVIOUS_QUANTITY


async def avg_marginal_costs_previous_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        previous_quantity = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_PREVIOUS_QUANTITY

    avg_cost, marginal_cost = calculate_avg_marginal_costs(
        context.user_data["current_total_cost"],
        context.user_data["previous_total_cost"],
        context.user_data["current_quantity"],
        previous_quantity,
    )
    if avg_cost is None:
        await update.message.reply_text(
            "Ошибка: Текущее количество произведенных единиц не может быть равно нулю. "
            "Пожалуйста, попробуйте снова."
        )
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST  # restart the calculation
    if marginal_cost is None:
        await update.message.reply_text(
            "Ошибка: Изменение количества произведенных единиц не может быть равно нулю. "
            "Пожалуйста, попробуйте снова."
        )
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST  # restart the calculation

    explanation = (
        "Средние издержки показывают стоимость одной единицы продукции в среднем. "
        "Предельные издержки показывают, сколько стоит производство дополнительной единицы. "
        "Сравнение этих показателей помогает оптимизировать производство."
    )
    result_text = (
        f"Средние издержки: {avg_cost:.2f}\n"
        f"Предельные издержки: {marginal_cost:.2f}\n\n{explanation}"
    )
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Markup / discount handlers                                                  #
# --------------------------------------------------------------------------- #


async def markup_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Наценка и скидка"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Наценка", callback_data="markup")],
        [InlineKeyboardButton("Скидка", callback_data="discount")],
    ])
    await query.edit_message_text("Выберите тип операции:", reply_markup=keyboard)
    return MARKUP_DISCOUNT_TYPE


async def markup_discount_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["is_discount"] = query.data == "discount"
    await query.edit_message_text("Введите базовую цену:")
    return MARKUP_DISCOUNT_PRICE


async def markup_discount_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["base_price"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARKUP_DISCOUNT_PRICE
    await update.message.reply_text("Введите процент (например, 20 для 20%):")
    return MARKUP_DISCOUNT_PERCENTAGE


async def markup_discount_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        percentage = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARKUP_DISCOUNT_PERCENTAGE
    base_price = context.user_data["base_price"]
    is_discount = context.user_data["is_discount"]

    new_price = calculate_markup_discount(base_price, percentage, is_discount)
    operation_type = "скидка" if is_discount else "наценка"
    explanation = (
        f"После {operation_type} в {percentage:.2f}% от базовой цены {base_price:.2f}, "
        f"новая цена составит {new_price:.2f}. "
        f"Это позволяет вам гибко управлять ценообразованием."
    )
    result_text = f"Новая цена после {operation_type}: {new_price:.2f}\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Payback-period handlers                                                     #
# --------------------------------------------------------------------------- #


async def payback_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Срок окупаемости"
    await query.edit_message_text("Введите начальные инвестиции:")
    return PAYBACK_INVESTMENT


async def payback_investment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["initial_investment"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PAYBACK_INVESTMENT
    await update.message.reply_text("Введите годовой денежный поток:")
    return PAYBACK_CASH_FLOW


async def payback_cash_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        annual_cash_flow = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PAYBACK_CASH_FLOW

    years, months = calculate_payback_period(context.user_data["initial_investment"], annual_cash_flow)
    if years is None:
        await update.message.reply_text(
            "Ошибка: Годовой денежный поток должен быть положительным. Пожалуйста, попробуйте снова."
        )
        return PAYBACK_INVESTMENT  # restart the calculation

    explanation = (
        f"Ваши инвестиции окупятся примерно через {years} лет и {months} месяцев. "
        f"Чем короче срок окупаемости, тем быстрее вы вернете вложенные средства."
    )
    result_text = f"Срок окупаемости: {years} лет и {months} месяцев\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Operating-leverage handlers                                                 #
# --------------------------------------------------------------------------- #


async def operating_leverage_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Операционный рычаг"
    await query.edit_message_text("Введите общую выручку:")
    return OPERATING_LEVERAGE_REVENUE


async def operating_leverage_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["total_revenue"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPERATING_LEVERAGE_REVENUE
    await update.message.reply_text("Введите переменные издержки:")
    return OPERATING_LEVERAGE_VARIABLE_COSTS


async def operating_leverage_variable_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["variable_costs"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPERATING_LEVERAGE_VARIABLE_COSTS
    await update.message.reply_text("Введите постоянные издержки:")
    return OPERATING_LEVERAGE_FIXED_COSTS


async def operating_leverage_fixed_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        fixed_costs = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPERATING_LEVERAGE_FIXED_COSTS

    operating_leverage = calculate_operating_leverage(
        context.user_data["total_revenue"], context.user_data["variable_costs"], fixed_costs
    )
    if operating_leverage is None:
        await update.message.reply_text(
            "Ошибка: Невозможно рассчитать операционный рычаг. Убедитесь, что выручка больше "
            "переменных издержек, а операционная прибыль не равна нулю. Пожалуйста, попробуйте снова."
        )
        return OPERATING_LEVERAGE_REVENUE  # restart the calculation

    if operating_leverage > 2:
        explanation = (
            "Высокий операционный рычаг. Это означает, что небольшое изменение выручки приведет "
            "к значительному изменению операционной прибыли. Высокий риск, но и высокий потенциал роста."
        )
    elif operating_leverage > 1:
        explanation = (
            "Умеренный операционный рычаг. Есть чувствительность к изменениям выручки, "
            "но риски управляемы."
        )
    else:
        explanation = (
            "Низкий операционный рычаг. Это указывает на стабильность прибыли при изменениях "
            "выручки, но и на меньший потенциал для быстрого роста."
        )

    result_text = f"Операционный рычаг: {operating_leverage:.2f}\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# NPV handlers                                                                #
# --------------------------------------------------------------------------- #


async def npv_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Расчёт NPV"
    await query.edit_message_text("Введите начальные инвестиции:")
    return NPV_INITIAL_INVESTMENT


async def npv_initial_investment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["initial_investment"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return NPV_INITIAL_INVESTMENT
    await update.message.reply_text("Введите денежный поток за первый год:")
    return NPV_CASH_FLOW_YEAR1


async def npv_cash_flow_year1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["cash_flow_year1"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return NPV_CASH_FLOW_YEAR1
    await update.message.reply_text(
        "Введите ставку дисконтирования (в процентах, например, 10 для 10%):"
    )
    return NPV_DISCOUNT_RATE


async def npv_discount_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        discount_rate = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return NPV_DISCOUNT_RATE

    npv = calculate_npv(
        context.user_data["initial_investment"], context.user_data["cash_flow_year1"], discount_rate
    )
    if npv is None:
        await update.message.reply_text(
            "Ошибка: Ставка дисконтирования не может быть меньше -100%. Пожалуйста, попробуйте снова."
        )
        return NPV_INITIAL_INVESTMENT  # restart the calculation

    if npv > 0:
        explanation = (
            f"Положительный NPV ({npv:.2f}) означает, что проект выгоден и ожидается, что он "
            f"принесет больше денежных средств, чем было вложено, с учетом временной стоимости денег."
        )
    elif npv < 0:
        explanation = (
            f"Отрицательный NPV ({npv:.2f}) означает, что проект невыгоден и, вероятно, не окупит "
            f"вложенные средства с учетом дисконтирования."
        )
    else:
        explanation = (
            "NPV равен нулю. Проект окупится, но не принесет дополнительной прибыли сверх "
            "требуемой нормы доходности."
        )

    result_text = f"NPV: {npv:.2f}\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Optimal-price handlers                                                      #
# --------------------------------------------------------------------------- #


async def optimal_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Расчёт оптимальной цены"
    await query.edit_message_text("Введите себестоимость единицы продукции:")
    return OPTIMAL_PRICE_UNIT_COST


async def optimal_price_unit_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["unit_cost"] = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPTIMAL_PRICE_UNIT_COST
    await update.message.reply_text("Введите желаемую наценку (в процентах, например, 20 для 20%):")
    return OPTIMAL_PRICE_MARKUP


async def optimal_price_markup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        desired_markup_percentage = parse_number(update.message.text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPTIMAL_PRICE_MARKUP

    optimal_price = calculate_optimal_price(context.user_data["unit_cost"], desired_markup_percentage)
    if optimal_price is None:
        await update.message.reply_text(
            "Ошибка: Желаемая наценка не может быть меньше -100%. Пожалуйста, попробуйте снова."
        )
        return OPTIMAL_PRICE_UNIT_COST  # restart the calculation

    explanation = (
        f"Оптимальная цена в {optimal_price:.2f} установлена с учетом вашей себестоимости и "
        f"желаемой наценки. Эта цена должна обеспечить желаемый уровень прибыли."
    )
    result_text = f"Оптимальная цена: {optimal_price:.2f}\n\n{explanation}"
    record_result(context, result_text)
    await send_long_message(update, result_text, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# History handler                                                             #
# --------------------------------------------------------------------------- #


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    history: list[str] = list(context.user_data.get("history", []))
    if not history:
        await safe_edit(query, "История расчётов пуста.", reply_markup=result_markup())
        return MENU

    body = "\n---\n".join(history)
    header = f"Ваша история расчётов (последние {len(history)}):\n\n"
    # A single edit_message_text call cannot exceed the Telegram limit, so we
    # drop the oldest entries until the message fits.
    while len(header) + len(body) > TELEGRAM_MESSAGE_LIMIT and len(history) > 1:
        history = history[1:]
        body = "\n---\n".join(history)
        header = f"Ваша история расчётов (последние {len(history)}):\n\n"
    await safe_edit(query, header + body, reply_markup=result_markup())
    return MENU


# --------------------------------------------------------------------------- #
# Global error handler                                                        #
# --------------------------------------------------------------------------- #


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Make sure a single bad update never takes the whole service down."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if not isinstance(update, Update):
        return
    if update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла внутренняя ошибка. Попробуйте ещё раз или начните с /start."
            )
        except Exception:  # pragma: no cover - best-effort notification
            logger.exception("Failed to notify user about the error")
    elif update.callback_query:
        try:
            await update.callback_query.answer("Произошла внутренняя ошибка.", show_alert=True)
        except Exception:  # pragma: no cover - best-effort notification
            logger.exception("Failed to notify user about the error")


# --------------------------------------------------------------------------- #
# Handler wiring                                                              #
# --------------------------------------------------------------------------- #


def _exact(name: str) -> re.Pattern[str]:
    return re.compile(rf"^{name}$")


_CALLBACK_STARTERS = (
    break_even_start, margin_start, roi_start, unit_cost_start,
    elasticity_start, profit_loss_start, avg_marginal_costs_start,
    markup_discount_start, payback_period_start, operating_leverage_start,
    npv_start, optimal_price_start, show_history,
)


def build_conversation_handler() -> ConversationHandler:
    """Create the ConversationHandler shared by both run modes."""
    menu_callbacks = [
        CallbackQueryHandler(handler, pattern=_exact(cb))
        for handler, (_, cb) in zip(_CALLBACK_STARTERS, MENU_ITEMS)
    ]
    back_to_menu = CallbackQueryHandler(show_menu_from_query, pattern=_exact("main_menu"))

    states: dict[int, list[Any]] = {
        MENU: menu_callbacks,
        BREAK_EVEN_FIXED_COSTS: [numeric_state_handler(break_even_fixed_costs)],
        BREAK_EVEN_VARIABLE_COST: [numeric_state_handler(break_even_variable_cost)],
        BREAK_EVEN_PRICE: [numeric_state_handler(break_even_price), back_to_menu],
        MARGIN_REVENUE: [numeric_state_handler(margin_revenue)],
        MARGIN_COST_OF_GOODS: [numeric_state_handler(margin_cost_of_goods), back_to_menu],
        ROI_PROFIT: [numeric_state_handler(roi_profit)],
        ROI_COST: [numeric_state_handler(roi_cost), back_to_menu],
        UNIT_COST_FIXED_COSTS: [numeric_state_handler(unit_cost_fixed_costs)],
        UNIT_COST_VARIABLE_COSTS: [numeric_state_handler(unit_cost_variable_costs)],
        UNIT_COST_UNITS: [numeric_state_handler(unit_cost_units), back_to_menu],
        ELASTICITY_QUANTITY_CHANGE: [numeric_state_handler(elasticity_quantity_change)],
        ELASTICITY_PRICE_CHANGE: [numeric_state_handler(elasticity_price_change), back_to_menu],
        PROFIT_LOSS_REVENUE: [numeric_state_handler(profit_loss_revenue)],
        PROFIT_LOSS_COSTS: [numeric_state_handler(profit_loss_costs), back_to_menu],
        AVG_MARGINAL_COST_CURRENT_TOTAL_COST: [numeric_state_handler(avg_marginal_costs_current_total_cost)],
        AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST: [numeric_state_handler(avg_marginal_costs_previous_total_cost)],
        AVG_MARGINAL_COST_CURRENT_QUANTITY: [numeric_state_handler(avg_marginal_costs_current_quantity)],
        AVG_MARGINAL_COST_PREVIOUS_QUANTITY: [numeric_state_handler(avg_marginal_costs_previous_quantity), back_to_menu],
        MARKUP_DISCOUNT_TYPE: [
            CallbackQueryHandler(markup_discount_type, pattern=re.compile(r"^(markup|discount)$"))
        ],
        MARKUP_DISCOUNT_PRICE: [numeric_state_handler(markup_discount_price)],
        MARKUP_DISCOUNT_PERCENTAGE: [numeric_state_handler(markup_discount_percentage), back_to_menu],
        PAYBACK_INVESTMENT: [numeric_state_handler(payback_investment)],
        PAYBACK_CASH_FLOW: [numeric_state_handler(payback_cash_flow), back_to_menu],
        OPERATING_LEVERAGE_REVENUE: [numeric_state_handler(operating_leverage_revenue)],
        OPERATING_LEVERAGE_VARIABLE_COSTS: [numeric_state_handler(operating_leverage_variable_costs)],
        OPERATING_LEVERAGE_FIXED_COSTS: [numeric_state_handler(operating_leverage_fixed_costs), back_to_menu],
        NPV_INITIAL_INVESTMENT: [numeric_state_handler(npv_initial_investment)],
        NPV_CASH_FLOW_YEAR1: [numeric_state_handler(npv_cash_flow_year1)],
        NPV_DISCOUNT_RATE: [numeric_state_handler(npv_discount_rate), back_to_menu],
        OPTIMAL_PRICE_UNIT_COST: [numeric_state_handler(optimal_price_unit_cost)],
        OPTIMAL_PRICE_MARKUP: [numeric_state_handler(optimal_price_markup), back_to_menu],
        HISTORY: [CallbackQueryHandler(show_history, pattern=_exact("history")), back_to_menu],
    }

    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states=states,
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(show_menu_from_query, pattern=_exact("main_menu")),
            CallbackQueryHandler(cancel, pattern=_exact("cancel")),
        ],
    )


# --------------------------------------------------------------------------- #
# Webhook lifecycle hooks                                                     #
# --------------------------------------------------------------------------- #


async def post_init(application: Application) -> None:
    """Register the webhook right after the bot starts (webhook mode only)."""
    if RUN_MODE != "webhook":
        return
    base_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if not base_url:
        raise RuntimeError(
            "Webhook mode requires RENDER_EXTERNAL_URL or WEBHOOK_URL to be set. "
            "Set MODE=polling to run without a public URL."
        )
    webhook_url = f"{base_url.rstrip('/')}{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    logger.info("Telegram webhook configured: %s", webhook_url)


async def post_shutdown(application: Application) -> None:
    """Remove the webhook on shutdown so a redeploy can fall back cleanly."""
    if RUN_MODE != "webhook" or application.bot is None:
        return
    try:
        await application.bot.delete_webhook()
        logger.info("Telegram webhook removed.")
    except Exception:  # pragma: no cover - best effort during shutdown
        logger.exception("Failed to delete webhook during shutdown")


def build_application() -> Application:
    """Build the Telegram application and register all handlers."""
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(build_conversation_handler())
    application.add_error_handler(on_error)
    return application


# --------------------------------------------------------------------------- #
# Webhook ASGI application                                                    #
# --------------------------------------------------------------------------- #

_lifespan_application: Optional[Application] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop the Telegram application together with the ASGI server."""
    global _lifespan_application
    if RUN_MODE == "webhook" and not (
        os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    ):
        raise RuntimeError(
            "Webhook mode requires RENDER_EXTERNAL_URL or WEBHOOK_URL to be set. "
            "Set MODE=polling to run without a public URL."
        )
    application = build_application()
    _lifespan_application = application
    if RUN_MODE == "polling":
        await application.initialize()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await application.start()
        logger.info("Telegram bot started in polling mode.")
    else:
        await application.initialize()  # triggers post_init -> set_webhook
        await application.start()
        logger.info("Telegram bot started in webhook mode.")
    try:
        yield
    finally:
        if RUN_MODE == "polling":
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        _lifespan_application = None
        logger.info("Telegram bot stopped.")


app = FastAPI(title="Marginal Telegram Bot", version="1.0.0", lifespan=lifespan)


@app.get("/", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Liveness probe used by load balancers / uptime checks."""
    return {"status": "ok"}


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, Any]:
    """Readiness probe: reports whether the Telegram application is running."""
    ready = _lifespan_application is not None and _lifespan_application.running
    return {"status": "ok" if ready else "starting", "bot_ready": ready}


@app.post(WEBHOOK_PATH, tags=["telegram"])
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Receive Telegram updates pushed via webhook (secret-token protected)."""
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    if _lifespan_application is None:
        raise HTTPException(status_code=503, detail="Bot is not ready yet")

    try:
        payload: dict = await request.json()
        update = Update.de_json(payload, _lifespan_application.bot)
    except (TypeError, KeyError, ValueError):
        logger.warning("Discarded malformed webhook payload")
        raise HTTPException(status_code=400, detail="Malformed update")
    await _lifespan_application.process_update(update)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Polling entry point                                                         #
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the bot with long polling (useful locally or in private containers)."""
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
