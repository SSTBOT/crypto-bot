#!/usr/bin/env python3
import ccxt
import time
import json
import os
import threading
from datetime import datetime
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

SETTINGS_FILE = "settings.json"

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    return {
        "max_amount": float(os.getenv("MAX_TRADE_AMOUNT", "5")),
        "stop_loss": float(os.getenv("STOP_LOSS", "10")),
        "take_profit": float(os.getenv("TAKE_PROFIT", "20")),
        "scan_interval": int(os.getenv("SCAN_INTERVAL", "30")),
        "min_price_change": float(os.getenv("MIN_PRICE_CHANGE", "5"))
    }

settings = load_settings()

exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

POSITIONS_FILE = "positions.json"
SCANNER_RUNNING = True
bot = None

def save_settings(s):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(s, f, indent=2)

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_positions(p):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(p, f, indent=2)

def send_telegram(text):
    global bot
    try:
        if bot:
            bot.send_message(chat_id=CHAT_ID, text=text[:4000])
    except Exception as e:
        print(f"TG error: {e}")

def buy_token(symbol, amount_usdt, source="auto"):
    positions = load_positions()
    if symbol in positions:
        return False, "Уже в портфеле"
    
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        quantity = amount_usdt / price
        market = exchange.market(symbol)
        quantity = exchange.amount_to_precision(symbol, quantity)
        
        exchange.create_market_buy_order(symbol, quantity)
        
        positions[symbol] = {
            'symbol': symbol,
            'buy_price': price,
            'buy_time': datetime.now().isoformat(),
            'quantity': float(quantity),
            'amount_usdt': amount_usdt,
            'stop_loss': price * (1 - settings["stop_loss"] / 100),
            'take_profit': price * (1 + settings["take_profit"] / 100),
            'source': source
        }
        save_positions(positions)
        
        msg = f"🟢 ПОКУПКА {symbol}\nЦена: ${price:.8f}\nСумма: ${amount_usdt}"
        send_telegram(msg)
        print(msg)
        return True, f"Куплен {symbol} по ${price:.8f}"
    except Exception as e:
        return False, str(e)

def sell_token(symbol):
    positions = load_positions()
    if symbol not in positions:
        return False, "Нет в портфеле"
    
    pos = positions[symbol]
    try:
        quantity = exchange.amount_to_precision(symbol, pos['quantity'])
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        exchange.create_market_sell_order(symbol, quantity)
        
        profit_usdt = float(pos['quantity']) * price - pos['amount_usdt']
        pnl_percent = ((price - pos['buy_price']) / pos['buy_price']) * 100
        
        del positions[symbol]
        save_positions(positions)
        
        emoji = "🟢" if profit_usdt >= 0 else "🔴"
        msg = f"{emoji} ПРОДАЖА {symbol}\nЦена: ${price:.8f}\nP&L: {pnl_percent:+.1f}% (${profit_usdt:+.2f})"
        send_telegram(msg)
        print(msg)
        return True, f"Продан {symbol}, P&L: {pnl_percent:+.1f}%"
    except Exception as e:
        return False, str(e)

def scan_loop():
    global SCANNER_RUNNING, settings
    
    print("🔍 Сканер запущен")
    
    while SCANNER_RUNNING:
        try:
            settings = load_settings()
            
            positions = load_positions()
            for symbol, pos in list(positions.items()):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current = ticker['last']
                    
                    if current <= pos['stop_loss']:
                        print(f"⚠️ Стоп-лосс: {symbol}")
                        sell_token(symbol)
                    elif current >= pos['take_profit']:
                        print(f"✅ Тейк-профит: {symbol}")
                        sell_token(symbol)
                except:
                    continue
            
            markets = exchange.load_markets()
            pairs = [s for s in markets if markets[s]['spot'] and s.endswith('/USDT')]
            pairs = pairs[:150]
            
            for symbol in pairs:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=2)
                    if len(ohlcv) < 2:
                        continue
                    
                    old_price = ohlcv[0][1]
                    current_price = ohlcv[1][4]
                    change = ((current_price - old_price) / old_price) * 100
                    
                    if change > settings["min_price_change"]:
                        volume_5m = ohlcv[1][5] * current_price
                        positions = load_positions()
                        
                        if symbol not in positions:
                            msg = f"🎯 СИГНАЛ: {symbol} +{change:.1f}% | Объём: ${volume_5m:,.0f}"
                            print(msg)
                            send_telegram(msg)
                            buy_token(symbol, settings["max_amount"], "auto")
                            
                except Exception as e:
                    continue
            
            time.sleep(settings["scan_interval"])
            
        except Exception as e:
            print(f"Ошибка сканера: {e}")
            time.sleep(10)

def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()

# ========== ГЛАВНОЕ МЕНЮ С КНОПКАМИ ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("📈 Мои позиции", callback_data="positions")],
        [InlineKeyboardButton("💰 Ручная покупка", callback_data="buy_menu")],
        [InlineKeyboardButton("🔴 Продать всё", callback_data="sellall")],
        [InlineKeyboardButton("▶️ Старт сканера", callback_data="start"),
         InlineKeyboardButton("⏹️ Стоп сканера", callback_data="stop")],
        [InlineKeyboardButton("📊 Выбрать монету", callback_data="select_coin")]
    ]
    return InlineKeyboardMarkup(keyboard)

def settings_menu():
    keyboard = [
        [InlineKeyboardButton("💰 Сумма сделки", callback_data="set_amount")],
        [InlineKeyboardButton("🛑 Стоп-лосс %", callback_data="set_sl")],
        [InlineKeyboardButton("✅ Тейк-профит %", callback_data="set_tp")],
        [InlineKeyboardButton("📊 Порог сигнала %", callback_data="set_threshold")],
        [InlineKeyboardButton("⏱️ Интервал скана", callback_data="set_interval")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def amount_menu():
    keyboard = [
        [InlineKeyboardButton("💵 $5", callback_data="amount_5"),
         InlineKeyboardButton("💵 $10", callback_data="amount_10")],
        [InlineKeyboardButton("💵 $20", callback_data="amount_20"),
         InlineKeyboardButton("💵 $50", callback_data="amount_50")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def threshold_menu():
    keyboard = [
        [InlineKeyboardButton("📊 2% (много сигналов)", callback_data="thresh_2")],
        [InlineKeyboardButton("📊 5% (средне)", callback_data="thresh_5")],
        [InlineKeyboardButton("📊 8% (мало сигналов)", callback_data="thresh_8")],
        [InlineKeyboardButton("📊 10% (редко)", callback_data="thresh_10")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def coin_menu():
    keyboard = [
        [InlineKeyboardButton("🪙 DOGE", callback_data="buy_DOGE"),
         InlineKeyboardButton("🪙 SHIB", callback_data="buy_SHIB")],
        [InlineKeyboardButton("🪙 PEPE", callback_data="buy_PEPE"),
         InlineKeyboardButton("🪙 BONK", callback_data="buy_BONK")],
        [InlineKeyboardButton("🪙 BTC", callback_data="buy_BTC"),
         InlineKeyboardButton("🪙 ETH", callback_data="buy_ETH")],
        [InlineKeyboardButton("🪙 SOL", callback_data="buy_SOL"),
         InlineKeyboardButton("🪙 XRP", callback_data="buy_XRP")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== TELEGRAM КОМАНДЫ ==========
def cmd_start(update, context):
    update.message.reply_text(
        "🤖 **Крипто Бот**\n\n"
        "Я автоматически сканирую рынок и покупаю монеты с ростом.\n"
        "Управляй мной через кнопки ниже 👇",
        parse_mode='Markdown',
        reply_markup=main_menu()
    )

def callback_handler(update, context):
    global SCANNER_RUNNING
    query = update.callback_query
    query.answer()
    
    if query.data == "status":
        try:
            balance = exchange.fetch_balance()
            usdt = balance['USDT']['free']
            positions = load_positions()
            total_pnl = 0.0
            pos_text = ""
            for symbol, pos in positions.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current = ticker['last']
                    pnl = ((current - pos['buy_price']) / pos['buy_price']) * 100
                    total_pnl += (current - pos['buy_price']) * pos['quantity']
                    pos_text += f"\n{symbol}: {pnl:+.1f}%"
                except:
                    continue
            query.edit_message_text(
                f"💰 **Баланс:** ${usdt:.2f}\n"
                f"📊 **Позиции:** {len(positions)}{pos_text}\n"
                f"📈 **Общий P&L:** ${total_pnl:+.2f}",
                parse_mode='Markdown',
                reply_markup=main_menu()
            )
        except Exception as e:
            query.edit_message_text(f"Ошибка: {e}", reply_markup=main_menu())
    
    elif query.data == "settings":
        s = load_settings()
        query.edit_message_text(
            f"⚙️ **Текущие настройки**\n\n"
            f"💰 Сумма сделки: `${s['max_amount']}`\n"
            f"🛑 Стоп-лосс: `{s['stop_loss']}%`\n"
            f"✅ Тейк-профит: `{s['take_profit']}%`\n"
            f"📊 Порог сигнала: `{s['min_price_change']}%`\n"
            f"⏱️ Интервал скана: `{s['scan_interval']} сек`\n",
            parse_mode='Markdown',
            reply_markup=settings_menu()
        )
    
    elif query.data == "positions":
        positions = load_positions()
        if not positions:
            query.edit_message_text("📭 Нет открытых позиций", reply_markup=main_menu())
            return
        text = "📈 **Ваши позиции**\n\n"
        for symbol, pos in positions.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                current = ticker['last']
                pnl = ((current - pos['buy_price']) / pos['buy_price']) * 100
                text += f"• {symbol}\n  Вход: ${pos['buy_price']:.6f}\n  Текущая: ${current:.6f}\n  P&L: {pnl:+.1f}%\n\n"
            except:
                continue
        query.edit_message_text(text, parse_mode='Markdown', reply_markup=main_menu())
    
    elif query.data == "buy_menu":
        query.edit_message_text("💰 **Выбери монету для покупки**", parse_mode='Markdown', reply_markup=coin_menu())
    
    elif query.data.startswith("buy_"):
        coin = query.data.replace("buy_", "")
        symbol = f"{coin}/USDT"
        amount = settings["max_amount"]
        success, msg = buy_token(symbol, amount, "manual")
        query.edit_message_text(f"{msg}", reply_markup=main_menu())
    
    elif query.data == "sellall":
        positions = load_positions()
        if not positions:
            query.edit_message_text("Нет позиций для продажи", reply_markup=main_menu())
            return
        sold = []
        for symbol in list(positions.keys()):
            success, _ = sell_token(symbol)
            if success:
                sold.append(symbol)
        query.edit_message_text(f"Продано {len(sold)} позиций", reply_markup=main_menu())
    
    elif query.data == "start":
        SCANNER_RUNNING = True
        query.edit_message_text("✅ Сканер запущен", reply_markup=main_menu())
    
    elif query.data == "stop":
        SCANNER_RUNNING = False
        query.edit_message_text("⏹️ Сканер остановлен", reply_markup=main_menu())
    
    elif query.data == "set_amount":
        query.edit_message_text("💰 **Выбери сумму сделки**", parse_mode='Markdown', reply_markup=amount_menu())
    
    elif query.data.startswith("amount_"):
        amount = int(query.data.replace("amount_", ""))
        s = load_settings()
        s["max_amount"] = amount
        save_settings(s)
        query.edit_message_text(f"✅ Сумма сделки установлена: ${amount}", reply_markup=settings_menu())
    
    elif query.data == "set_sl":
        query.edit_message_text("Введи процент стоп-лосса (например: 10)", reply_markup=None)
        context.user_data['waiting_for'] = 'sl'
    
    elif query.data == "set_tp":
        query.edit_message_text("Введи процент тейк-профита (например: 20)", reply_markup=None)
        context.user_data['waiting_for'] = 'tp'
    
    elif query.data == "set_threshold":
        query.edit_message_text("📊 **Выбери порог сигнала**", parse_mode='Markdown', reply_markup=threshold_menu())
    
    elif query.data.startswith("thresh_"):
        thresh = int(query.data.replace("thresh_", ""))
        s = load_settings()
        s["min_price_change"] = thresh
        save_settings(s)
        query.edit_message_text(f"✅ Порог сигнала установлен: {thresh}%", reply_markup=settings_menu())
    
    elif query.data == "set_interval":
        query.edit_message_text("Введи интервал скана в секундах (например: 30)", reply_markup=None)
        context.user_data['waiting_for'] = 'interval'
    
    elif query.data == "select_coin":
        query.edit_message_text("💰 **Выбери монету**", parse_mode='Markdown', reply_markup=coin_menu())
    
    elif query.data == "back":
        query.edit_message_text("🤖 **Главное меню**", parse_mode='Markdown', reply_markup=main_menu())

def handle_message(update, context):
    text = update.message.text
    if 'waiting_for' in context.user_data:
        waiting = context.user_data['waiting_for']
        s = load_settings()
        try:
            value = float(text)
            if waiting == 'sl':
                s["stop_loss"] = value
                save_settings(s)
                update.message.reply_text(f"✅ Стоп-лосс установлен: {value}%", reply_markup=main_menu())
            elif waiting == 'tp':
                s["take_profit"] = value
                save_settings(s)
                update.message.reply_text(f"✅ Тейк-профит установлен: {value}%", reply_markup=main_menu())
            elif waiting == 'interval':
                s["scan_interval"] = int(value)
                save_settings(s)
                update.message.reply_text(f"✅ Интервал установлен: {int(value)} сек", reply_markup=main_menu())
        except:
            update.message.reply_text("❌ Введи число", reply_markup=main_menu())
        del context.user_data['waiting_for']

def main():
    global bot
    
    print("\n" + "="*40)
    print("   КРИПТО БОТ v3.0 (С КНОПКАМИ)")
    print("="*40 + "\n")
    
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"💰 Баланс: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        print("Проверь API ключи в .env файле")
        return
    
    start_scanner()
    
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    bot = updater.bot
    
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CallbackQueryHandler(callback_handler))
    dp.add_handler(CommandHandler("menu", cmd_start))
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_start))
    dp.add_handler(CommandHandler("settings", cmd_start))
    dp.add_handler(CommandHandler("status", cmd_start))
    
    print("✅ Бот запущен! Напиши /start в Telegram\n")
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
