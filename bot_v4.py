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
        "scan_interval": int(os.getenv("SCAN_INTERVAL", "15")),
        "min_price_change": float(os.getenv("MIN_PRICE_CHANGE", "2")),
        "scanner_5m": True,
        "scanner_24h": True,
        "scanner_new": True
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
last_new_pairs = set()

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

def buy_token(symbol, amount_usdt, source="auto", reason=""):
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
            'source': source,
            'reason': reason
        }
        save_positions(positions)
        
        msg = f"🟢 ПОКУПКА {symbol}\nЦена: ${price:.8f}\nСумма: ${amount_usdt}\nПричина: {reason}"
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

def scan_24h_leaders():
    """Сканер монет с большим ростом за 24 часа"""
    try:
        tickers = exchange.fetch_tickers()
        leaders = []
        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'):
                continue
            if ticker.get('percentage', 0) and ticker['percentage'] > 0:
                leaders.append({
                    'symbol': symbol,
                    'change_24h': ticker['percentage'],
                    'volume': ticker.get('quoteVolume', 0),
                    'price': ticker['last']
                })
        leaders.sort(key=lambda x: x['change_24h'], reverse=True)
        return leaders[:20]
    except Exception as e:
        print(f"24h scan error: {e}")
        return []

def scan_new_pairs():
    """Сканер новых монет (первые 24 часа после листинга)"""
    global last_new_pairs
    try:
        markets = exchange.load_markets()
        new_pairs = []
        for symbol, market in markets.items():
            if not symbol.endswith('/USDT'):
                continue
            if market.get('info', {}).get('isNew', False):
                new_pairs.append(symbol)
        
        # Проверяем только новые, которых не было в прошлый раз
        really_new = [s for s in new_pairs if s not in last_new_pairs]
        last_new_pairs = set(new_pairs)
        return really_new
    except Exception as e:
        print(f"New pairs error: {e}")
        return []

def scan_5m_movers():
    """Сканер монет с ростом за 5 минут"""
    try:
        markets = exchange.load_markets()
        pairs = [s for s in markets if markets[s]['spot'] and s.endswith('/USDT')]
        pairs = pairs[:200]
        
        signals = []
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
                    signals.append({
                        'symbol': symbol,
                        'change': change,
                        'volume': volume_5m,
                        'price': current_price
                    })
            except:
                continue
        signals.sort(key=lambda x: x['change'], reverse=True)
        return signals[:10]
    except Exception as e:
        print(f"5m scan error: {e}")
        return []

def scan_loop():
    global SCANNER_RUNNING, settings
    
    print("🔍 Все сканеры запущены")
    
    while SCANNER_RUNNING:
        try:
            settings = load_settings()
            
            # Проверка позиций
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
            
            # Сканер 1: 5-минутный рост
            if settings.get("scanner_5m", True):
                signals = scan_5m_movers()
                for sig in signals[:3]:
                    positions = load_positions()
                    if sig['symbol'] not in positions:
                        msg = f"🎯 СИГНАЛ (5мин): {sig['symbol']} +{sig['change']:.1f}% | Объём: ${sig['volume']:,.0f}"
                        print(msg)
                        send_telegram(msg)
                        buy_token(sig['symbol'], settings["max_amount"], "auto_5m", f"Рост {sig['change']:.1f}% за 5мин")
            
            # Сканер 2: 24-часовые лидеры
            if settings.get("scanner_24h", True):
                leaders = scan_24h_leaders()
                for leader in leaders[:5]:
                    positions = load_positions()
                    if leader['symbol'] not in positions and leader['change_24h'] > 15:
                        # Проверяем также 5-минутный рост
                        try:
                            ohlcv = exchange.fetch_ohlcv(leader['symbol'], '5m', limit=2)
                            if len(ohlcv) >= 2:
                                change_5m = ((ohlcv[1][4] - ohlcv[0][1]) / ohlcv[0][1]) * 100
                                if change_5m > 1:
                                    msg = f"🎯 СИГНАЛ (24ч): {leader['symbol']} +{leader['change_24h']:.1f}% за день"
                                    print(msg)
                                    send_telegram(msg)
                                    buy_token(leader['symbol'], settings["max_amount"], "auto_24h", f"Лидер дня +{leader['change_24h']:.1f}%")
                        except:
                            pass
            
            # Сканер 3: Новые монеты
            if settings.get("scanner_new", True):
                new_pairs = scan_new_pairs()
                for new_symbol in new_pairs[:3]:
                    positions = load_positions()
                    if new_symbol not in positions:
                        msg = f"🎯 НОВАЯ МОНЕТА: {new_symbol} | Свежий листинг на Bybit!"
                        print(msg)
                        send_telegram(msg)
                        buy_token(new_symbol, settings["max_amount"], "auto_new", "Новый листинг")
            
            time.sleep(settings["scan_interval"])
            
        except Exception as e:
            print(f"Ошибка сканера: {e}")
            time.sleep(10)

def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()

# ========== МЕНЮ ==========
def main_menu():
    s = load_settings()
    keyboard = [
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("📈 Мои позиции", callback_data="positions")],
        [InlineKeyboardButton("💰 Ручная покупка", callback_data="buy_menu")],
        [InlineKeyboardButton("🔴 Продать всё", callback_data="sellall")],
        [InlineKeyboardButton("🏆 Топ 24ч", callback_data="top24h")],
        [InlineKeyboardButton("🆕 Новые монеты", callback_data="newcoins")],
        [InlineKeyboardButton("▶️ Старт сканера", callback_data="start"),
         InlineKeyboardButton("⏹️ Стоп сканера", callback_data="stop")],
    ]
    return InlineKeyboardMarkup(keyboard)

def scanner_menu():
    s = load_settings()
    keyboard = [
        [InlineKeyboardButton(f"{'✅' if s['scanner_5m'] else '❌'} 5-минутный рост", callback_data="toggle_5m")],
        [InlineKeyboardButton(f"{'✅' if s['scanner_24h'] else '❌'} 24-часовые лидеры", callback_data="toggle_24h")],
        [InlineKeyboardButton(f"{'✅' if s['scanner_new'] else '❌'} Новые монеты", callback_data="toggle_new")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def settings_menu():
    s = load_settings()
    keyboard = [
        [InlineKeyboardButton(f"💰 Сумма: ${s['max_amount']}", callback_data="set_amount")],
        [InlineKeyboardButton(f"🛑 Стоп-лосс: {s['stop_loss']}%", callback_data="set_sl")],
        [InlineKeyboardButton(f"✅ Тейк-профит: {s['take_profit']}%", callback_data="set_tp")],
        [InlineKeyboardButton(f"📊 Порог 5мин: {s['min_price_change']}%", callback_data="set_threshold")],
        [InlineKeyboardButton(f"⏱️ Интервал: {s['scan_interval']}c", callback_data="set_interval")],
        [InlineKeyboardButton("🎯 Настройка сканеров", callback_data="scanner_menu")],
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
        [InlineKeyboardButton("📊 1% (много сигналов)", callback_data="thresh_1")],
        [InlineKeyboardButton("📊 2% (средне)", callback_data="thresh_2")],
        [InlineKeyboardButton("📊 5% (мало)", callback_data="thresh_5")],
        [InlineKeyboardButton("📊 8% (редко)", callback_data="thresh_8")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def coin_menu():
    keyboard = [
        [InlineKeyboardButton("🪙 DOGE", callback_data="buy_DOGE"),
         InlineKeyboardButton("🪙 SHIB", callback_data="buy_SHIB")],
        [InlineKeyboardButton("🪙 PEPE", callback_data="buy_PEPE"),
         InlineKeyboardButton("🪙 AVL", callback_data="buy_AVL")],
        [InlineKeyboardButton("🪙 BTC", callback_data="buy_BTC"),
         InlineKeyboardButton("🪙 ETH", callback_data="buy_ETH")],
        [InlineKeyboardButton("🪙 SOL", callback_data="buy_SOL"),
         InlineKeyboardButton("🪙 XRP", callback_data="buy_XRP")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def cmd_start(update, context):
    update.message.reply_text(
        "🤖 **Крипто Бот v4.0**\n\n"
        "3 мощных сканера:\n"
        "• 📊 5-минутный рост - быстрые пам-пы\n"
        "• 🏆 24-часовые лидеры - трендовые монеты\n"
        "• 🆕 Новые монеты - свежие листинги\n\n"
        "Управляй через кнопки 👇",
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
            f"💰 Сумма: `${s['max_amount']}`\n"
            f"🛑 Стоп-лосс: `{s['stop_loss']}%`\n"
            f"✅ Тейк-профит: `{s['take_profit']}%`\n"
            f"📊 Порог: `{s['min_price_change']}%`\n"
            f"⏱️ Интервал: `{s['scan_interval']} сек`\n",
            parse_mode='Markdown',
            reply_markup=settings_menu()
        )
    
    elif query.data == "scanner_menu":
        s = load_settings()
        query.edit_message_text(
            "🎯 **Настройка сканеров**\n\n"
            "Включи/выключи нужные сканеры:",
            reply_markup=scanner_menu()
        )
    
    elif query.data == "toggle_5m":
        s = load_settings()
        s["scanner_5m"] = not s.get("scanner_5m", True)
        save_settings(s)
        status = "включён" if s["scanner_5m"] else "выключен"
        query.edit_message_text(f"✅ 5-минутный сканер {status}", reply_markup=scanner_menu())
    
    elif query.data == "toggle_24h":
        s = load_settings()
        s["scanner_24h"] = not s.get("scanner_24h", True)
        save_settings(s)
        status = "включён" if s["scanner_24h"] else "выключен"
        query.edit_message_text(f"✅ 24-часовой сканер {status}", reply_markup=scanner_menu())
    
    elif query.data == "toggle_new":
        s = load_settings()
        s["scanner_new"] = not s.get("scanner_new", True)
        save_settings(s)
        status = "включён" if s["scanner_new"] else "выключен"
        query.edit_message_text(f"✅ Сканер новых монет {status}", reply_markup=scanner_menu())
    
    elif query.data == "top24h":
        leaders = scan_24h_leaders()
        text = "🏆 **Топ монет за 24ч**\n\n"
        for i, l in enumerate(leaders[:10], 1):
            text += f"{i}. {l['symbol']}: +{l['change_24h']:.1f}%\n"
        query.edit_message_text(text, parse_mode='Markdown', reply_markup=main_menu())
    
    elif query.data == "newcoins":
        new_pairs = scan_new_pairs()
        if new_pairs:
            text = "🆕 **Новые монеты на Bybit**\n\n" + "\n".join(new_pairs[:10])
        else:
            text = "🆕 Новых монет пока нет"
        query.edit_message_text(text, reply_markup=main_menu())
    
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
        query.edit_message_text("💰 **Выбери монету**", parse_mode='Markdown', reply_markup=coin_menu())
    
    elif query.data.startswith("buy_"):
        coin = query.data.replace("buy_", "")
        symbol = f"{coin}/USDT"
        amount = load_settings()["max_amount"]
        success, msg = buy_token(symbol, amount, "manual", "Ручная покупка")
        query.edit_message_text(msg, reply_markup=main_menu())
    
    elif query.data == "sellall":
        positions = load_positions()
        if not positions:
            query.edit_message_text("Нет позиций", reply_markup=main_menu())
            return
        sold = []
        for symbol in list(positions.keys()):
            success, _ = sell_token(symbol)
            if success:
                sold.append(symbol)
        query.edit_message_text(f"Продано: {', '.join(sold) if sold else 'нет'}", reply_markup=main_menu())
    
    elif query.data == "start":
        SCANNER_RUNNING = True
        query.edit_message_text("✅ Сканеры запущены", reply_markup=main_menu())
    
    elif query.data == "stop":
        SCANNER_RUNNING = False
        query.edit_message_text("⏹️ Сканеры остановлены", reply_markup=main_menu())
    
    elif query.data == "set_amount":
        query.edit_message_text("💰 Выбери сумму", reply_markup=amount_menu())
    
    elif query.data.startswith("amount_"):
        amount = int(query.data.replace("amount_", ""))
        s = load_settings()
        s["max_amount"] = amount
        save_settings(s)
        query.edit_message_text(f"✅ Сумма: ${amount}", reply_markup=settings_menu())
    
    elif query.data == "set_threshold":
        query.edit_message_text("📊 Выбери порог", reply_markup=threshold_menu())
    
    elif query.data.startswith("thresh_"):
        thresh = int(query.data.replace("thresh_", ""))
        s = load_settings()
        s["min_price_change"] = thresh
        save_settings(s)
        query.edit_message_text(f"✅ Порог: {thresh}%", reply_markup=settings_menu())
    
    elif query.data == "set_sl":
        query.edit_message_text("Введи % стоп-лосса (число)", reply_markup=None)
        context.user_data['waiting_for'] = 'sl'
    
    elif query.data == "set_tp":
        query.edit_message_text("Введи % тейк-профита (число)", reply_markup=None)
        context.user_data['waiting_for'] = 'tp'
    
    elif query.data == "set_interval":
        query.edit_message_text("Введи интервал в секундах", reply_markup=None)
        context.user_data['waiting_for'] = 'interval'
    
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
                update.message.reply_text(f"✅ Стоп-лосс: {value}%", reply_markup=main_menu())
            elif waiting == 'tp':
                s["take_profit"] = value
                save_settings(s)
                update.message.reply_text(f"✅ Тейк-профит: {value}%", reply_markup=main_menu())
            elif waiting == 'interval':
                s["scan_interval"] = int(value)
                save_settings(s)
                update.message.reply_text(f"✅ Интервал: {int(value)} сек", reply_markup=main_menu())
        except:
            update.message.reply_text("❌ Введи число", reply_markup=main_menu())
        del context.user_data['waiting_for']

def main():
    global bot
    
    print("\n" + "="*40)
    print("   КРИПТО БОТ v4.0")
    print("   3 СКАНЕРА: 5мин | 24ч | Новые монеты")
    print("="*40 + "\n")
    
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"💰 Баланс: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return
    
    start_scanner()
    
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    bot = updater.bot
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CallbackQueryHandler(callback_handler))
    dp.add_handler(CommandHandler("help", cmd_start))
    
    print("✅ Бот с 3 сканерами запущен! Напиши /start\n")
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
