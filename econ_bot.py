
import logging
import datetime
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# States for ConversationHandler
MENU, \
BREAK_EVEN_FIXED_COSTS, BREAK_EVEN_VARIABLE_COST, BREAK_EVEN_PRICE, \
MARGIN_REVENUE, MARGIN_COST_OF_GOODS, ROI_PROFIT, ROI_COST, \
UNIT_COST_FIXED_COSTS, UNIT_COST_VARIABLE_COSTS, UNIT_COST_UNITS, \
ELASTICITY_QUANTITY_CHANGE, ELASTICITY_PRICE_CHANGE, \
PROFIT_LOSS_REVENUE, PROFIT_LOSS_COSTS, \
AVG_MARGINAL_COST_CURRENT_TOTAL_COST, AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST, \
AVG_MARGINAL_COST_CURRENT_QUANTITY, AVG_MARGINAL_COST_PREVIOUS_QUANTITY, \
MARKUP_DISCOUNT_PRICE, MARKUP_DISCOUNT_PERCENTAGE, MARKUP_DISCOUNT_TYPE, \
PAYBACK_INVESTMENT, PAYBACK_CASH_FLOW, \
OPERATING_LEVERAGE_REVENUE, OPERATING_LEVERAGE_VARIABLE_COSTS, OPERATING_LEVERAGE_FIXED_COSTS, \
NPV_INITIAL_INVESTMENT, NPV_CASH_FLOW_YEAR1, NPV_DISCOUNT_RATE, \
OPTIMAL_PRICE_UNIT_COST, OPTIMAL_PRICE_MARKUP, \
HISTORY = range(33)

# --- Helper functions for calculations ---

def calculate_break_even(fixed_costs, variable_cost_per_unit, price_per_unit):
    if (price_per_unit - variable_cost_per_unit) <= 0:
        return None, None # Avoid division by zero or negative contribution margin
    break_even_units = fixed_costs / (price_per_unit - variable_cost_per_unit)
    break_even_revenue = break_even_units * price_per_unit
    return break_even_units, break_even_revenue

def calculate_margin(revenue, cost_of_goods):
    if revenue == 0:
        return None, None
    absolute_margin = revenue - cost_of_goods
    percentage_margin = (absolute_margin / revenue) * 100
    return absolute_margin, percentage_margin

def calculate_roi(profit_from_investment, cost_of_investment):
    if cost_of_investment == 0:
        return None
    roi = (profit_from_investment / cost_of_investment) * 100
    return roi

def calculate_unit_cost(total_fixed_costs, total_variable_costs, number_of_units):
    if number_of_units == 0:
        return None
    total_costs = total_fixed_costs + total_variable_costs
    unit_cost = total_costs / number_of_units
    return unit_cost

def calculate_elasticity(quantity_change, price_change):
    if price_change == 0:
        return None, "Невозможно рассчитать эластичность при нулевом изменении цены."
    elasticity = quantity_change / price_change
    interpretation = ""
    if elasticity < -1:
        interpretation = "Эластичный спрос (значительное изменение спроса при изменении цены)"
    elif elasticity > -1 and elasticity < 0:
        interpretation = "Неэластичный спрос (незначительное изменение спроса при изменении цены)"
    elif elasticity == -1:
        interpretation = "Единичная эластичность (изменение спроса пропорционально изменению цены)"
    elif elasticity == 0:
        interpretation = "Совершенно неэластичный спрос (спрос не меняется при изменении цены)"
    elif elasticity > 0:
        interpretation = "Товар Гиффена или Веблена (редкий случай, когда спрос растет с ростом цены)"
    return elasticity, interpretation

def calculate_profit_loss(total_revenue, total_costs):
    profit_loss = total_revenue - total_costs
    status = "Прибыль" if profit_loss >= 0 else "Убыток"
    return profit_loss, status

def calculate_avg_marginal_costs(current_total_cost, previous_total_cost, current_quantity, previous_quantity):
    if current_quantity == 0:
        return None, None
    avg_cost = current_total_cost / current_quantity

    if (current_quantity - previous_quantity) == 0:
        marginal_cost = None # Avoid division by zero
    else:
        marginal_cost = (current_total_cost - previous_total_cost) / (current_quantity - previous_quantity)
    return avg_cost, marginal_cost

def calculate_markup_discount(base_price, percentage, is_discount):
    if is_discount:
        new_price = base_price * (1 - percentage / 100)
    else:
        new_price = base_price * (1 + percentage / 100)
    return new_price

def calculate_payback_period(initial_investment, annual_cash_flow):
    if annual_cash_flow <= 0:
        return None, None
    payback_years = initial_investment / annual_cash_flow
    years = int(payback_years)
    months = round((payback_years - years) * 12)
    return years, months

def calculate_operating_leverage(total_revenue, variable_costs, fixed_costs):
    if total_revenue - variable_costs == 0:
        return None
    contribution_margin = total_revenue - variable_costs
    operating_income = contribution_margin - fixed_costs
    if operating_income == 0:
        return None
    operating_leverage = contribution_margin / operating_income
    return operating_leverage

def calculate_npv(initial_investment, cash_flow_year1, discount_rate):
    if discount_rate <= -100:
        return None # Discount rate cannot be -100% or less
    discount_factor = 1 + (discount_rate / 100)
    npv = (cash_flow_year1 / discount_factor) - initial_investment
    return npv

def calculate_optimal_price(unit_cost, desired_markup_percentage):
    if desired_markup_percentage <= -100:
        return None # Markup cannot be -100% or less
    optimal_price = unit_cost * (1 + desired_markup_percentage / 100)
    return optimal_price

# --- Bot commands and handlers ---

async def start(update: Update, context) -> int:
    """Sends a welcome message and shows the main menu."""
    keyboard = [
        [InlineKeyboardButton("Точка безубыточности", callback_data='break_even')],
        [InlineKeyboardButton("Маржинальность", callback_data='margin')],
        [InlineKeyboardButton("ROI", callback_data='roi')],
        [InlineKeyboardButton("Себестоимость единицы продукции", callback_data='unit_cost')],
        [InlineKeyboardButton("Эластичность спроса", callback_data='elasticity')],
        [InlineKeyboardButton("Прибыль/убыток", callback_data='profit_loss')],
        [InlineKeyboardButton("Средние и предельные издержки", callback_data='avg_marginal_costs')],
        [InlineKeyboardButton("Наценка и скидка", callback_data='markup_discount')],
        [InlineKeyboardButton("Срок окупаемости", callback_data='payback_period')],
        [InlineKeyboardButton("Операционный рычаг", callback_data='operating_leverage')],
        [InlineKeyboardButton("Расчёт NPV", callback_data='npv')],
        [InlineKeyboardButton("Расчёт оптимальной цены", callback_data='optimal_price')],
        [InlineKeyboardButton("История расчётов", callback_data='history')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Привет! Я бот для расчета микроэкономических показателей. Выбери нужный расчет:",
        reply_markup=reply_markup
    )
    # Initialize history for the user if it doesn't exist
    if 'history' not in context.user_data:
        context.user_data['history'] = []
    return MENU

async def menu(update: Update, context) -> int:
    """Displays the main menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Точка безубыточности", callback_data='break_even')],
        [InlineKeyboardButton("Маржинальность", callback_data='margin')],
        [InlineKeyboardButton("ROI", callback_data='roi')],
        [InlineKeyboardButton("Себестоимость единицы продукции", callback_data='unit_cost')],
        [InlineKeyboardButton("Эластичность спроса", callback_data='elasticity')],
        [InlineKeyboardButton("Прибыль/убыток", callback_data='profit_loss')],
        [InlineKeyboardButton("Средние и предельные издержки", callback_data='avg_marginal_costs')],
        [InlineKeyboardButton("Наценка и скидка", callback_data='markup_discount')],
        [InlineKeyboardButton("Срок окупаемости", callback_data='payback_period')],
        [InlineKeyboardButton("Операционный рычаг", callback_data='operating_leverage')],
        [InlineKeyboardButton("Расчёт NPV", callback_data='npv')],
        [InlineKeyboardButton("Расчёт оптимальной цены", callback_data='optimal_price')],
        [InlineKeyboardButton("История расчётов", callback_data='history')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Выбери нужный расчет:", reply_markup=reply_markup
    )
    return MENU

async def back_to_menu(update: Update, context) -> int:
    """Returns to the main menu."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Точка безубыточности", callback_data='break_even')],
        [InlineKeyboardButton("Маржинальность", callback_data='margin')],
        [InlineKeyboardButton("ROI", callback_data='roi')],
        [InlineKeyboardButton("Себестоимость единицы продукции", callback_data='unit_cost')],
        [InlineKeyboardButton("Эластичность спроса", callback_data='elasticity')],
        [InlineKeyboardButton("Прибыль/убыток", callback_data='profit_loss')],
        [InlineKeyboardButton("Средние и предельные издержки", callback_data='avg_marginal_costs')],
        [InlineKeyboardButton("Наценка и скидка", callback_data='markup_discount')],
        [InlineKeyboardButton("Срок окупаемости", callback_data='payback_period')],
        [InlineKeyboardButton("Операционный рычаг", callback_data='operating_leverage')],
        [InlineKeyboardButton("Расчёт NPV", callback_data='npv')],
        [InlineKeyboardButton("Расчёт оптимальной цены", callback_data='optimal_price')],
        [InlineKeyboardButton("История расчётов", callback_data='history')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Выбери нужный расчет:", reply_markup=reply_markup
    )
    return MENU

# --- Break-even point handlers ---

async def break_even_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Точка безубыточности"
    await query.edit_message_text("Введите постоянные издержки:")
    return BREAK_EVEN_FIXED_COSTS

async def break_even_fixed_costs(update: Update, context) -> int:
    try:
        context.user_data["fixed_costs"] = float(update.message.text)
        await update.message.reply_text("Введите переменные издержки на единицу продукции:")
        return BREAK_EVEN_VARIABLE_COST
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return BREAK_EVEN_FIXED_COSTS

async def break_even_variable_cost(update: Update, context) -> int:
    try:
        context.user_data["variable_cost_per_unit"] = float(update.message.text)
        await update.message.reply_text("Введите цену за единицу продукции:")
        return BREAK_EVEN_PRICE
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return BREAK_EVEN_VARIABLE_COST

async def break_even_price(update: Update, context) -> int:
    try:
        context.user_data["price_per_unit"] = float(update.message.text)
        fixed_costs = context.user_data["fixed_costs"]
        variable_cost_per_unit = context.user_data["variable_cost_per_unit"]
        price_per_unit = context.user_data["price_per_unit"]

        units, revenue = calculate_break_even(fixed_costs, variable_cost_per_unit, price_per_unit)

        if units is None:
            await update.message.reply_text(
                "Ошибка: Цена за единицу должна быть больше переменных издержек на единицу продукции. "
                "Пожалуйста, попробуйте снова."
            )
            return BREAK_EVEN_FIXED_COSTS # Restart the calculation

        explanation = (
            f"Это означает, что вам нужно продать минимум {units:.2f} единиц, чтобы покрыть все расходы. "
            f"Всё, что продано сверх этого — ваша прибыль."
        )
        result_text = (
            f"Точка безубыточности:\n"\
            f"В единицах: {units:.2f}\n"\
            f"В выручке: {revenue:.2f}\n\n"\
            f"{explanation}"
        )
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return BREAK_EVEN_PRICE

# --- Margin handlers ---

async def margin_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Маржинальность"
    await query.edit_message_text("Введите выручку:")
    return MARGIN_REVENUE

async def margin_revenue(update: Update, context) -> int:
    try:
        context.user_data["revenue"] = float(update.message.text)
        await update.message.reply_text("Введите себестоимость товаров:")
        return MARGIN_COST_OF_GOODS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARGIN_REVENUE

async def margin_cost_of_goods(update: Update, context) -> int:
    try:
        context.user_data["cost_of_goods"] = float(update.message.text)
        revenue = context.user_data["revenue"]
        cost_of_goods = context.user_data["cost_of_goods"]

        absolute_margin, percentage_margin = calculate_margin(revenue, cost_of_goods)

        if absolute_margin is None:
            await update.message.reply_text(
                "Ошибка: Выручка не может быть равна нулю. Пожалуйста, попробуйте снова."
            )
            return MARGIN_REVENUE # Restart the calculation

        explanation = ""
        if percentage_margin >= 50:
            explanation = "Это отличный показатель, указывающий на высокую прибыльность вашего продукта или услуги."
        elif percentage_margin >= 20:
            explanation = "Это хороший показатель, свидетельствующий о здоровой прибыльности."
        else:
            explanation = "Это низкий показатель, возможно, стоит пересмотреть ценообразование или издержки."

        result_text = (
            f"Маржинальность:\n"\
            f"Абсолютное значение: {absolute_margin:.2f}\n"\
            f"В процентах: {percentage_margin:.2f}%\n\n"\
            f"{explanation}"
        )
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARGIN_COST_OF_GOODS

# --- ROI handlers ---

async def roi_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "ROI"
    await query.edit_message_text("Введите прибыль от инвестиций:")
    return ROI_PROFIT

async def roi_profit(update: Update, context) -> int:
    try:
        context.user_data["profit_from_investment"] = float(update.message.text)
        await update.message.reply_text("Введите стоимость инвестиций:")
        return ROI_COST
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ROI_PROFIT

async def roi_cost(update: Update, context) -> int:
    try:
        context.user_data["cost_of_investment"] = float(update.message.text)
        profit_from_investment = context.user_data["profit_from_investment"]
        cost_of_investment = context.user_data["cost_of_investment"]

        roi = calculate_roi(profit_from_investment, cost_of_investment)

        if roi is None:
            await update.message.reply_text(
                "Ошибка: Стоимость инвестиций не может быть равна нулю. Пожалуйста, попробуйте снова."
            )
            return ROI_PROFIT # Restart the calculation

        explanation = ""
        if roi >= 100:
            explanation = f"ROI {roi:.2f}% означает, что на каждый вложенный рубль вы получаете {1 + roi/100:.2f} руб. чистой прибыли. Это отличный показатель."
        elif roi >= 0:
            explanation = f"ROI {roi:.2f}% означает, что на каждый вложенный рубль вы получаете {1 + roi/100:.2f} руб. чистой прибыли. Это хороший показатель."
        else:
            explanation = f"Отрицательный ROI {roi:.2f}% означает, что ваши инвестиции убыточны. Возможно, стоит пересмотреть стратегию."

        result_text = f"ROI: {roi:.2f}%\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ROI_COST

# --- Unit Cost handlers ---

async def unit_cost_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Себестоимость единицы продукции"
    await query.edit_message_text("Введите общие постоянные издержки:")
    return UNIT_COST_FIXED_COSTS

async def unit_cost_fixed_costs(update: Update, context) -> int:
    try:
        context.user_data["total_fixed_costs"] = float(update.message.text)
        await update.message.reply_text("Введите общие переменные издержки:")
        return UNIT_COST_VARIABLE_COSTS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return UNIT_COST_FIXED_COSTS

async def unit_cost_variable_costs(update: Update, context) -> int:
    try:
        context.user_data["total_variable_costs"] = float(update.message.text)
        await update.message.reply_text("Введите количество произведенных единиц:")
        return UNIT_COST_UNITS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return UNIT_COST_VARIABLE_COSTS

async def unit_cost_units(update: Update, context) -> int:
    try:
        context.user_data["number_of_units"] = float(update.message.text)
        total_fixed_costs = context.user_data["total_fixed_costs"]
        total_variable_costs = context.user_data["total_variable_costs"]
        number_of_units = context.user_data["number_of_units"]

        unit_cost = calculate_unit_cost(total_fixed_costs, total_variable_costs, number_of_units)

        if unit_cost is None:
            await update.message.reply_text(
                "Ошибка: Количество произведенных единиц не может быть равно нулю. Пожалуйста, попробуйте снова."
            )
            return UNIT_COST_FIXED_COSTS # Restart the calculation

        explanation = (
            f"Это средняя стоимость производства одной единицы продукции. "
            f"Важно, чтобы цена продажи была выше этой цифры для получения прибыли."
        )
        result_text = f"Себестоимость единицы продукции: {unit_cost:.2f}\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return UNIT_COST_UNITS

# --- Elasticity handlers ---

async def elasticity_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Эластичность спроса"
    await query.edit_message_text("Введите процентное изменение спроса (например, -10 для снижения на 10%):")
    return ELASTICITY_QUANTITY_CHANGE

async def elasticity_quantity_change(update: Update, context) -> int:
    try:
        context.user_data["quantity_change"] = float(update.message.text)
        await update.message.reply_text("Введите процентное изменение цены (например, 5 для роста на 5%):")
        return ELASTICITY_PRICE_CHANGE
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ELASTICITY_QUANTITY_CHANGE

async def elasticity_price_change(update: Update, context) -> int:
    try:
        context.user_data["price_change"] = float(update.message.text)
        quantity_change = context.user_data["quantity_change"]
        price_change = context.user_data["price_change"]

        elasticity, interpretation = calculate_elasticity(quantity_change, price_change)

        if elasticity is None:
            await update.message.reply_text(
                f"Ошибка: {interpretation} Пожалуйста, попробуйте снова."
            )
            return ELASTICITY_QUANTITY_CHANGE # Restart the calculation

        explanation = ""
        if elasticity < -1:
            explanation = "Спрос эластичен. Небольшое изменение цены приведет к значительному изменению спроса. Возможно, стоит рассмотреть снижение цены для увеличения общего дохода."
        elif elasticity > -1 and elasticity < 0:
            explanation = "Спрос неэластичен. Изменение цены не сильно повлияет на спрос. Вы можете рассмотреть возможность повышения цены без существенной потери клиентов."
        elif elasticity == -1:
            explanation = "Единичная эластичность. Изменение цены приведет к пропорциональному изменению спроса. Общий доход останется прежним."
        elif elasticity == 0:
            explanation = "Спрос совершенно неэластичен. Изменение цены не повлияет на спрос. Это редкий случай для товаров первой необходимости."
        elif elasticity > 0:
            explanation = "Товар Гиффена или Веблена. Спрос растет с ростом цены. Это очень редкий случай, характерный для предметов роскоши или низкокачественных товаров в определенных условиях."

        result_text = (
            f"Эластичность спроса:\n"\
            f"Коэффициент: {elasticity:.2f}\n"\
            f"Интерпретация: {interpretation}\n\n"\
            f"{explanation}"
        )
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return ELASTICITY_PRICE_CHANGE

# --- Profit/Loss handlers ---

async def profit_loss_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Прибыль/убыток"
    await query.edit_message_text("Введите общую выручку:")
    return PROFIT_LOSS_REVENUE

async def profit_loss_revenue(update: Update, context) -> int:
    try:
        context.user_data["total_revenue"] = float(update.message.text)
        await update.message.reply_text("Введите общие затраты:")
        return PROFIT_LOSS_COSTS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PROFIT_LOSS_REVENUE

async def profit_loss_costs(update: Update, context) -> int:
    try:
        context.user_data["total_costs"] = float(update.message.text)
        total_revenue = context.user_data["total_revenue"]
        total_costs = context.user_data["total_costs"]

        profit_loss, status = calculate_profit_loss(total_revenue, total_costs)

        explanation = ""
        if profit_loss > 0:
            explanation = f"Ваш бизнес получил прибыль в размере {profit_loss:.2f}. Это хороший результат, показывающий эффективность вашей деятельности."
        elif profit_loss < 0:
            explanation = f"Ваш бизнес понес убытки в размере {abs(profit_loss):.2f}. Необходимо проанализировать доходы и расходы для улучшения ситуации."
        else:
            explanation = "Ваш бизнес вышел в ноль. Доходы равны расходам. Для роста необходимо увеличивать выручку или сокращать издержки."

        result_text = (
            f"Прибыль/убыток:\n"\
            f"Сумма: {profit_loss:.2f}\n"\
            f"Статус: {status}\n\n"\
            f"{explanation}"
        )
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PROFIT_LOSS_COSTS

# --- Average and Marginal Costs handlers ---

async def avg_marginal_costs_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Средние и предельные издержки"
    await query.edit_message_text("Введите общие издержки при текущем объеме производства:")
    return AVG_MARGINAL_COST_CURRENT_TOTAL_COST

async def avg_marginal_costs_current_total_cost(update: Update, context) -> int:
    try:
        context.user_data["current_total_cost"] = float(update.message.text)
        await update.message.reply_text("Введите общие издержки при предыдущем объеме производства:")
        return AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_CURRENT_TOTAL_COST

async def avg_marginal_costs_previous_total_cost(update: Update, context) -> int:
    try:
        context.user_data["previous_total_cost"] = float(update.message.text)
        await update.message.reply_text("Введите текущее количество произведенных единиц:")
        return AVG_MARGINAL_COST_CURRENT_QUANTITY
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST

async def avg_marginal_costs_current_quantity(update: Update, context) -> int:
    try:
        context.user_data["current_quantity"] = float(update.message.text)
        await update.message.reply_text("Введите предыдущее количество произведенных единиц:")
        return AVG_MARGINAL_COST_PREVIOUS_QUANTITY
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_CURRENT_QUANTITY

async def avg_marginal_costs_previous_quantity(update: Update, context) -> int:
    try:
        context.user_data["previous_quantity"] = float(update.message.text)
        current_total_cost = context.user_data["current_total_cost"]
        previous_total_cost = context.user_data["previous_total_cost"]
        current_quantity = context.user_data["current_quantity"]
        previous_quantity = context.user_data["previous_quantity"]

        avg_cost, marginal_cost = calculate_avg_marginal_costs(
            current_total_cost, previous_total_cost, current_quantity, previous_quantity
        )

        if avg_cost is None:
            await update.message.reply_text(
                "Ошибка: Текущее количество произведенных единиц не может быть равно нулю. Пожалуйста, попробуйте снова."
            )
            return AVG_MARGINAL_COST_CURRENT_TOTAL_COST # Restart the calculation

        if marginal_cost is None:
            await update.message.reply_text(
                "Ошибка: Изменение количества произведенных единиц не может быть равно нулю. Пожалуйста, попробуйте снова."
            )
            return AVG_MARGINAL_COST_CURRENT_TOTAL_COST # Restart the calculation

        explanation = (
            f"Средние издержки показывают стоимость одной единицы продукции в среднем. "
            f"Предельные издержки показывают, сколько стоит производство дополнительной единицы. "
            f"Сравнение этих показателей помогает оптимизировать производство."
        )
        result_text = (
            f"Средние издержки: {avg_cost:.2f}\n"\
            f"Предельные издержки: {marginal_cost:.2f}\n\n"\
            f"{explanation}"
        )
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return AVG_MARGINAL_COST_PREVIOUS_QUANTITY

# --- Markup and Discount handlers ---

async def markup_discount_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Наценка и скидка"
    keyboard = [
        [InlineKeyboardButton("Наценка", callback_data='markup')],
        [InlineKeyboardButton("Скидка", callback_data='discount')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите тип операции:", reply_markup=reply_markup)
    return MARKUP_DISCOUNT_TYPE

async def markup_discount_type(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["is_discount"] = (query.data == 'discount')
    await query.edit_message_text("Введите базовую цену:")
    return MARKUP_DISCOUNT_PRICE

async def markup_discount_price(update: Update, context) -> int:
    try:
        context.user_data["base_price"] = float(update.message.text)
        await update.message.reply_text("Введите процент (например, 20 для 20%):")
        return MARKUP_DISCOUNT_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARKUP_DISCOUNT_PRICE

async def markup_discount_percentage(update: Update, context) -> int:
    try:
        context.user_data["percentage"] = float(update.message.text)
        base_price = context.user_data["base_price"]
        percentage = context.user_data["percentage"]
        is_discount = context.user_data["is_discount"]

        new_price = calculate_markup_discount(base_price, percentage, is_discount)

        operation_type = "скидка" if is_discount else "наценка"
        explanation = (
            f"После {operation_type} в {percentage:.2f}% от базовой цены {base_price:.2f}, "
            f"новая цена составит {new_price:.2f}. "
            f"Это позволяет вам гибко управлять ценообразованием."
        )
        result_text = f"Новая цена после {operation_type}: {new_price:.2f}\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return MARKUP_DISCOUNT_PERCENTAGE

# --- Payback Period handlers ---

async def payback_period_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Срок окупаемости"
    await query.edit_message_text("Введите начальные инвестиции:")
    return PAYBACK_INVESTMENT

async def payback_investment(update: Update, context) -> int:
    try:
        context.user_data["initial_investment"] = float(update.message.text)
        await update.message.reply_text("Введите годовой денежный поток:")
        return PAYBACK_CASH_FLOW
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PAYBACK_INVESTMENT

async def payback_cash_flow(update: Update, context) -> int:
    try:
        context.user_data["annual_cash_flow"] = float(update.message.text)
        initial_investment = context.user_data["initial_investment"]
        annual_cash_flow = context.user_data["annual_cash_flow"]

        years, months = calculate_payback_period(initial_investment, annual_cash_flow)

        if years is None:
            await update.message.reply_text(
                "Ошибка: Годовой денежный поток должен быть положительным. Пожалуйста, попробуйте снова."
            )
            return PAYBACK_INVESTMENT # Restart the calculation

        explanation = (
            f"Ваши инвестиции окупятся примерно через {years} лет и {months} месяцев. "
            f"Чем короче срок окупаемости, тем быстрее вы вернете вложенные средства."
        )
        result_text = f"Срок окупаемости: {years} лет и {months} месяцев\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return PAYBACK_CASH_FLOW

# --- Operating Leverage handlers ---

async def operating_leverage_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Операционный рычаг"
    await query.edit_message_text("Введите общую выручку:")
    return OPERATING_LEVERAGE_REVENUE

async def operating_leverage_revenue(update: Update, context) -> int:
    try:
        context.user_data["total_revenue"] = float(update.message.text)
        await update.message.reply_text("Введите переменные издержки:")
        return OPERATING_LEVERAGE_VARIABLE_COSTS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPERATING_LEVERAGE_REVENUE

async def operating_leverage_variable_costs(update: Update, context) -> int:
    try:
        context.user_data["variable_costs"] = float(update.message.text)
        await update.message.reply_text("Введите постоянные издержки:")
        return OPERATING_LEVERAGE_FIXED_COSTS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPERATING_LEVERAGE_VARIABLE_COSTS

async def operating_leverage_fixed_costs(update: Update, context) -> int:
    try:
        context.user_data["fixed_costs"] = float(update.message.text)
        total_revenue = context.user_data["total_revenue"]
        variable_costs = context.user_data["variable_costs"]
        fixed_costs = context.user_data["fixed_costs"]

        operating_leverage = calculate_operating_leverage(total_revenue, variable_costs, fixed_costs)

        if operating_leverage is None:
            await update.message.reply_text(
                "Ошибка: Невозможно рассчитать операционный рычаг. Убедитесь, что выручка больше переменных издержек, а операционная прибыль не равна нулю. Пожалуйста, попробуйте снова."
            )
            return OPERATING_LEVERAGE_REVENUE # Restart the calculation

        explanation = ""
        if operating_leverage > 2:
            explanation = "Высокий операционный рычаг. Это означает, что небольшое изменение выручки приведет к значительному изменению операционной прибыли. Высокий риск, но и высокий потенциал роста."
        elif operating_leverage > 1:
            explanation = "Умеренный операционный рычаг. Есть чувствительность к изменениям выручки, но риски управляемы."
        else:
            explanation = "Низкий операционный рычаг. Это указывает на стабильность прибыли при изменениях выручки, но и на меньший потенциал для быстрого роста."

        result_text = f"Операционный рычаг: {operating_leverage:.2f}\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPERATING_LEVERAGE_FIXED_COSTS

# --- NPV handlers ---

async def npv_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Расчёт NPV"
    await query.edit_message_text("Введите начальные инвестиции:")
    return NPV_INITIAL_INVESTMENT

async def npv_initial_investment(update: Update, context) -> int:
    try:
        context.user_data["initial_investment"] = float(update.message.text)
        await update.message.reply_text("Введите денежный поток за первый год:")
        return NPV_CASH_FLOW_YEAR1
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return NPV_INITIAL_INVESTMENT

async def npv_cash_flow_year1(update: Update, context) -> int:
    try:
        context.user_data["cash_flow_year1"] = float(update.message.text)
        await update.message.reply_text("Введите ставку дисконтирования (в процентах, например, 10 для 10%):")
        return NPV_DISCOUNT_RATE
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return NPV_CASH_FLOW_YEAR1

async def npv_discount_rate(update: Update, context) -> int:
    try:
        context.user_data["discount_rate"] = float(update.message.text)
        initial_investment = context.user_data["initial_investment"]
        cash_flow_year1 = context.user_data["cash_flow_year1"]
        discount_rate = context.user_data["discount_rate"]

        npv = calculate_npv(initial_investment, cash_flow_year1, discount_rate)

        if npv is None:
            await update.message.reply_text(
                "Ошибка: Ставка дисконтирования не может быть меньше -100%. Пожалуйста, попробуйте снова."
            )
            return NPV_INITIAL_INVESTMENT # Restart the calculation

        explanation = ""
        if npv > 0:
            explanation = f"Положительный NPV ({npv:.2f}) означает, что проект выгоден и ожидается, что он принесет больше денежных средств, чем было вложено, с учетом временной стоимости денег."
        elif npv < 0:
            explanation = f"Отрицательный NPV ({npv:.2f}) означает, что проект невыгоден и, вероятно, не окупит вложенные средства с учетом дисконтирования."
        else:
            explanation = "NPV равен нулю. Проект окупится, но не принесет дополнительной прибыли сверх требуемой нормы доходности."

        result_text = f"NPV: {npv:.2f}\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return NPV_DISCOUNT_RATE

# --- Optimal Price handlers ---

async def optimal_price_start(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_calculation"] = "Расчёт оптимальной цены"
    await query.edit_message_text("Введите себестоимость единицы продукции:")
    return OPTIMAL_PRICE_UNIT_COST

async def optimal_price_unit_cost(update: Update, context) -> int:
    try:
        context.user_data["unit_cost"] = float(update.message.text)
        await update.message.reply_text("Введите желаемую наценку (в процентах, например, 20 для 20%):")
        return OPTIMAL_PRICE_MARKUP
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPTIMAL_PRICE_UNIT_COST

async def optimal_price_markup(update: Update, context) -> int:
    try:
        context.user_data["desired_markup_percentage"] = float(update.message.text)
        unit_cost = context.user_data["unit_cost"]
        desired_markup_percentage = context.user_data["desired_markup_percentage"]

        optimal_price = calculate_optimal_price(unit_cost, desired_markup_percentage)

        if optimal_price is None:
            await update.message.reply_text(
                "Ошибка: Желаемая наценка не может быть меньше -100%. Пожалуйста, попробуйте снова."
            )
            return OPTIMAL_PRICE_UNIT_COST # Restart the calculation

        explanation = (
            f"Оптимальная цена в {optimal_price:.2f} установлена с учетом вашей себестоимости и желаемой наценки. "
            f"Эта цена должна обеспечить желаемый уровень прибыли."
        )
        result_text = f"Оптимальная цена: {optimal_price:.2f}\n\n{explanation}"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context.user_data["history"].append(f"[{timestamp}] {context.user_data["current_calculation"]}: {result_text}")
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return MENU
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите числовое значение.")
        return OPTIMAL_PRICE_MARKUP

# --- History handler ---

async def show_history(update: Update, context) -> int:
    query = update.callback_query
    await query.answer()

    if not context.user_data['history']:
        history_text = "История расчётов пуста."
    else:
        history_text = "Ваша история расчётов:\n\n" + "\n---\n".join(context.user_data['history'])

    keyboard = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(history_text, reply_markup=reply_markup)
    return MENU

async def cancel(update: Update, context) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        "До свидания! Надеюсь, я был полезен.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Token from environment variable
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")
    application = Application.builder().token(TOKEN).build()

    # Add conversation handler with states
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(break_even_start, pattern='^break_even$\\b'),
                CallbackQueryHandler(margin_start, pattern='^margin$\\b'),
                CallbackQueryHandler(roi_start, pattern='^roi$\\b'),
                CallbackQueryHandler(unit_cost_start, pattern='^unit_cost$\\b'),
                CallbackQueryHandler(elasticity_start, pattern='^elasticity$\\b'),
                CallbackQueryHandler(profit_loss_start, pattern='^profit_loss$\\b'),
                CallbackQueryHandler(avg_marginal_costs_start, pattern='^avg_marginal_costs$\\b'),
                CallbackQueryHandler(markup_discount_start, pattern='^markup_discount$\\b'),
                CallbackQueryHandler(payback_period_start, pattern='^payback_period$\\b'),
                CallbackQueryHandler(operating_leverage_start, pattern='^operating_leverage$\\b'),
                CallbackQueryHandler(npv_start, pattern='^npv$\\b'),
                CallbackQueryHandler(optimal_price_start, pattern='^optimal_price$\\b'),
                CallbackQueryHandler(show_history, pattern='^history$\\b'),
            ],
            BREAK_EVEN_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_fixed_costs)],
            BREAK_EVEN_VARIABLE_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_variable_cost)],
            BREAK_EVEN_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_price),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            MARGIN_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, margin_revenue)],
            MARGIN_COST_OF_GOODS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, margin_cost_of_goods),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            ROI_PROFIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, roi_profit)],
            ROI_COST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, roi_cost),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            UNIT_COST_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_fixed_costs)],
            UNIT_COST_VARIABLE_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_variable_costs)],
            UNIT_COST_UNITS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_units),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            ELASTICITY_QUANTITY_CHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, elasticity_quantity_change)],
            ELASTICITY_PRICE_CHANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, elasticity_price_change),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            PROFIT_LOSS_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_loss_revenue)],
            PROFIT_LOSS_COSTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, profit_loss_costs),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            AVG_MARGINAL_COST_CURRENT_TOTAL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_current_total_cost)],
            AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_previous_total_cost)],
            AVG_MARGINAL_COST_CURRENT_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_current_quantity)],
            AVG_MARGINAL_COST_PREVIOUS_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_previous_quantity),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            MARKUP_DISCOUNT_TYPE: [CallbackQueryHandler(markup_discount_type, pattern='^markup$|^discount$\\b')],
            MARKUP_DISCOUNT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, markup_discount_price)],
            MARKUP_DISCOUNT_PERCENTAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, markup_discount_percentage),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            PAYBACK_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payback_investment)],
            PAYBACK_CASH_FLOW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payback_cash_flow),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            OPERATING_LEVERAGE_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_revenue)],
            OPERATING_LEVERAGE_VARIABLE_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_variable_costs)],
            OPERATING_LEVERAGE_FIXED_COSTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_fixed_costs),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            NPV_INITIAL_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_initial_investment)],
            NPV_CASH_FLOW_YEAR1: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_cash_flow_year1)],
            NPV_DISCOUNT_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, npv_discount_rate),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            OPTIMAL_PRICE_UNIT_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, optimal_price_unit_cost)],
            OPTIMAL_PRICE_MARKUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, optimal_price_markup),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
            HISTORY: [
                CallbackQueryHandler(show_history, pattern='^history$\\b'),
                CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')
            ],
        },
        fallbacks=[CommandHandler("start", start), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def build_application() -> Application:
    """Build the Telegram application and register all handlers."""
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN environment variable is not set!")

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(break_even_start, pattern='^break_even$\\b'),
                CallbackQueryHandler(margin_start, pattern='^margin$\\b'),
                CallbackQueryHandler(roi_start, pattern='^roi$\\b'),
                CallbackQueryHandler(unit_cost_start, pattern='^unit_cost$\\b'),
                CallbackQueryHandler(elasticity_start, pattern='^elasticity$\\b'),
                CallbackQueryHandler(profit_loss_start, pattern='^profit_loss$\\b'),
                CallbackQueryHandler(avg_marginal_costs_start, pattern='^avg_marginal_costs$\\b'),
                CallbackQueryHandler(markup_discount_start, pattern='^markup_discount$\\b'),
                CallbackQueryHandler(payback_period_start, pattern='^payback_period$\\b'),
                CallbackQueryHandler(operating_leverage_start, pattern='^operating_leverage$\\b'),
                CallbackQueryHandler(npv_start, pattern='^npv$\\b'),
                CallbackQueryHandler(optimal_price_start, pattern='^optimal_price$\\b'),
                CallbackQueryHandler(show_history, pattern='^history$\\b'),
            ],
            BREAK_EVEN_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_fixed_costs)],
            BREAK_EVEN_VARIABLE_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_variable_cost)],
            BREAK_EVEN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, break_even_price), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            MARGIN_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, margin_revenue)],
            MARGIN_COST_OF_GOODS: [MessageHandler(filters.TEXT & ~filters.COMMAND, margin_cost_of_goods), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            ROI_PROFIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, roi_profit)],
            ROI_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, roi_cost), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            UNIT_COST_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_fixed_costs)],
            UNIT_COST_VARIABLE_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_variable_costs)],
            UNIT_COST_UNITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, unit_cost_units), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            ELASTICITY_QUANTITY_CHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, elasticity_quantity_change)],
            ELASTICITY_PRICE_CHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, elasticity_price_change), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            PROFIT_LOSS_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_loss_revenue)],
            PROFIT_LOSS_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_loss_costs), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            AVG_MARGINAL_COST_CURRENT_TOTAL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_current_total_cost)],
            AVG_MARGINAL_COST_PREVIOUS_TOTAL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_previous_total_cost)],
            AVG_MARGINAL_COST_CURRENT_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_current_quantity)],
            AVG_MARGINAL_COST_PREVIOUS_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, avg_marginal_costs_previous_quantity), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            MARKUP_DISCOUNT_TYPE: [CallbackQueryHandler(markup_discount_type, pattern='^markup$|^discount$\\b')],
            MARKUP_DISCOUNT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, markup_discount_price)],
            MARKUP_DISCOUNT_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, markup_discount_percentage), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            PAYBACK_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payback_investment)],
            PAYBACK_CASH_FLOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, payback_cash_flow), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            OPERATING_LEVERAGE_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_revenue)],
            OPERATING_LEVERAGE_VARIABLE_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_variable_costs)],
            OPERATING_LEVERAGE_FIXED_COSTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, operating_leverage_fixed_costs), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            NPV_INITIAL_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_initial_investment)],
            NPV_CASH_FLOW_YEAR1: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_cash_flow_year1)],
            NPV_DISCOUNT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, npv_discount_rate), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            OPTIMAL_PRICE_UNIT_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, optimal_price_unit_cost)],
            OPTIMAL_PRICE_MARKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, optimal_price_markup), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
            HISTORY: [CallbackQueryHandler(show_history, pattern='^history$\\b'), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
        },
        fallbacks=[CommandHandler("start", start), CallbackQueryHandler(back_to_menu, pattern='^main_menu$\\b')],
    )
    application.add_handler(conv_handler)
    return application


telegram_application = build_application()
app = FastAPI(title="Marginal Telegram Bot")


@app.on_event("startup")
async def startup() -> None:
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    secret = os.environ.get("WEBHOOK_SECRET")
    if not external_url or not secret:
        raise RuntimeError("RENDER_EXTERNAL_URL and WEBHOOK_SECRET must be set")

    await telegram_application.initialize()
    await telegram_application.start()
    webhook_url = f"{external_url.rstrip('/')}/telegram/webhook"
    await telegram_application.bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Telegram webhook configured: %s", webhook_url)


@app.on_event("shutdown")
async def shutdown() -> None:
    await telegram_application.bot.delete_webhook()
    await telegram_application.stop()
    await telegram_application.shutdown()


@app.get("/")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    expected_secret = os.environ.get("WEBHOOK_SECRET")
    if not expected_secret or x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    update = Update.de_json(payload, telegram_application.bot)
    await telegram_application.process_update(update)
    return JSONResponse({"ok": True})


if __name__ == '__main__':
    main()
