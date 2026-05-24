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

DEFAULT_SETTINGS = {
    "max_amount": 5.0,
    "stop_loss": 10.0,
    "take_profit": 20.0,
    "scan_interval": 15,
    "min_price_change": 2.0,
    "scanner_5m": True,
    "scanner_24h": True,
    "scanner_new": True
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            saved = json.load(f)
            # Merge with defaults
            settings = DEFAULT_SETTINGS.copy()
            settings.update(saved)
            return settings
    return DEFAULT_SETTINGS.copy()

def save_settings(s):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(s, f, indent=2)

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

def save_positions(p):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(p, f, indent=2)

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

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
        return False, "Already in portfolio"
    
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
        
        msg = f"🟢 BUY {symbol}\nPrice: ${price:.8f}\nAmount: ${amount_usdt}\nReason: {reason}"
        send_telegram(msg)
        print(msg)
        return True, f"Bought {symbol} at ${price:.8f}"
    except Exception as e:
        return False, str(e)

def sell_token(symbol):
    positions = load_positions()
    if symbol not in positions:
        return False, "Not in portfolio"
    
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
        
        emoji = "PROFIT" if profit_usdt >= 0 else "LOSS"
        msg = f"{emoji} SELL {symbol}\nPrice: ${price:.8f}\nP&L: {pnl_percent:+.1f}% (${profit_usdt:+.2f})"
        send_telegram(msg)
        print(msg)
        return True, f"Sold {symbol}, P&L: {pnl_percent:+.1f}%"
    except Exception as e:
        return False, str(e)

def scan_24h_leaders():
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
    global last_new_pairs
    try:
        markets = exchange.load_markets()
        new_pairs = []
        for symbol, market in markets.items():
            if not symbol.endswith('/USDT'):
                continue
            if market.get('info', {}).get('isNew', False):
                new_pairs.append(symbol)
        really_new = [s for s in new_pairs if s not in last_new_pairs]
        last_new_pairs = set(new_pairs)
        return really_new
    except Exception as e:
        print(f"New pairs error: {e}")
        return []

def scan_5m_movers():
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
    
    print("All scanners started")
    
    while SCANNER_RUNNING:
        try:
            settings = load_settings()
            
            # Check positions
            positions = load_positions()
            for symbol, pos in list(positions.items()):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current = ticker['last']
                    if current <= pos['stop_loss']:
                        print(f"Stop loss: {symbol}")
                        sell_token(symbol)
                    elif current >= pos['take_profit']:
                        print(f"Take profit: {symbol}")
                        sell_token(symbol)
                except:
                    continue
            
            # Scanner 1: 5-minute gain
            if settings.get("scanner_5m", True):
                signals = scan_5m_movers()
                for sig in signals[:3]:
                    positions = load_positions()
                    if sig['symbol'] not in positions:
                        msg = f"SIGNAL (5m): {sig['symbol']} +{sig['change']:.1f}% | Vol: ${sig['volume']:,.0f}"
                        print(msg)
                        send_telegram(msg)
                        buy_token(sig['symbol'], settings["max_amount"], "auto_5m", f"Gain {sig['change']:.1f}% in 5m")
            
            # Scanner 2: 24h leaders
            if settings.get("scanner_24h", True):
                leaders = scan_24h_leaders()
                for leader in leaders[:5]:
                    positions = load_positions()
                    if leader['symbol'] not in positions and leader['change_24h'] > 15:
                        try:
                            ohlcv = exchange.fetch_ohlcv(leader['symbol'], '5m', limit=2)
                            if len(ohlcv) >= 2:
                                change_5m = ((ohlcv[1][4] - ohlcv[0][1]) / ohlcv[0][1]) * 100
                                if change_5m > 1:
                                    msg = f"SIGNAL (24h): {leader['symbol']} +{leader['change_24h']:.1f}% today"
                                    print(msg)
                                    send_telegram(msg)
                                    buy_token(leader['symbol'], settings["max_amount"], "auto_24h", f"Leader +{leader['change_24h']:.1f}%")
                        except:
                            pass
            
            # Scanner 3: New pairs
            if settings.get("scanner_new", True):
                new_pairs = scan_new_pairs()
                for new_symbol in new_pairs[:3]:
                    positions = load_positions()
                    if new_symbol not in positions:
                        msg = f"NEW COIN: {new_symbol} | Fresh listing on Bybit!"
                        print(msg)
                        send_telegram(msg)
                        buy_token(new_symbol, settings["max_amount"], "auto_new", "New listing")
            
            time.sleep(settings["scan_interval"])
            
        except Exception as e:
            print(f"Scan error: {e}")
            time.sleep(10)

def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()

def main_menu():
    keyboard = [
        [InlineKeyboardButton("Status", callback_data="status")],
        [InlineKeyboardButton("Settings", callback_data="settings")],
        [InlineKeyboardButton("My Positions", callback_data="positions")],
        [InlineKeyboardButton("Manual Buy", callback_data="buy_menu")],
        [InlineKeyboardButton("Sell All", callback_data="sellall")],
        [InlineKeyboardButton("Top 24h", callback_data="top24h")],
        [InlineKeyboardButton("New Coins", callback_data="newcoins")],
        [InlineKeyboardButton("Start Scanner", callback_data="start"),
         InlineKeyboardButton("Stop Scanner", callback_data="stop")],
    ]
    return InlineKeyboardMarkup(keyboard)

def scanner_menu():
    s = load_settings()
    keyboard = [
        [InlineKeyboardButton(f"{'ON' if s.get('scanner_5m', True) else 'OFF'} 5-min gain", callback_data="toggle_5m")],
        [InlineKeyboardButton(f"{'ON' if s.get('scanner_24h', True) else 'OFF'} 24h leaders", callback_data="toggle_24h")],
        [InlineKeyboardButton(f"{'ON' if s.get('scanner_new', True) else 'OFF'} New coins", callback_data="toggle_new")],
        [InlineKeyboardButton("Back", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def settings_menu():
    s = load_settings()
    keyboard = [
        [InlineKeyboardButton(f"Amount: ${s['max_amount']}", callback_data="set_amount")],
        [InlineKeyboardButton(f"Stop loss: {s['stop_loss']}%", callback_data="set_sl")],
        [InlineKeyboardButton(f"Take profit: {s['take_profit']}%", callback_data="set_tp")],
        [InlineKeyboardButton(f"Threshold: {s['min_price_change']}%", callback_data="set_threshold")],
        [InlineKeyboardButton(f"Interval: {s['scan_interval']}s", callback_data="set_interval")],
        [InlineKeyboardButton("Scanner settings", callback_data="scanner_menu")],
        [InlineKeyboardButton("Back", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def amount_menu():
    keyboard = [
        [InlineKeyboardButton("$5", callback_data="amount_5"),
         InlineKeyboardButton("$10", callback_data="amount_10")],
        [InlineKeyboardButton("$20", callback_data="amount_20"),
         InlineKeyboardButton("$50", callback_data="amount_50")],
        [InlineKeyboardButton("Back", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def threshold_menu():
    keyboard = [
        [InlineKeyboardButton("1% (many signals)", callback_data="thresh_1")],
        [InlineKeyboardButton("2% (medium)", callback_data="thresh_2")],
        [InlineKeyboardButton("5% (few)", callback_data="thresh_5")],
        [InlineKeyboardButton("8% (rare)", callback_data="thresh_8")],
        [InlineKeyboardButton("Back", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def coin_menu():
    keyboard = [
        [InlineKeyboardButton("DOGE", callback_data="buy_DOGE"),
         InlineKeyboardButton("SHIB", callback_data="buy_SHIB")],
        [InlineKeyboardButton("PEPE", callback_data="buy_PEPE"),
         InlineKeyboardButton("AVL", callback_data="buy_AVL")],
        [InlineKeyboardButton("BTC", callback_data="buy_BTC"),
         InlineKeyboardButton("ETH", callback_data="buy_ETH")],
        [InlineKeyboardButton("SOL", callback_data="buy_SOL"),
         InlineKeyboardButton("XRP", callback_data="buy_XRP")],
        [InlineKeyboardButton("Back", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def cmd_start(update, context):
    update.message.reply_text(
        "🤖 Crypto Bot v4.0\n\n"
        "3 scanners:\n"
        "• 5-min gain - fast pumps\n"
        "• 24h leaders - trending coins\n"
        "• New coins - fresh listings\n\n"
        "Use buttons below 👇",
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
                f"Balance: ${usdt:.2f}\n"
                f"Positions: {len(positions)}{pos_text}\n"
                f"Total P&L: ${total_pnl:+.2f}",
                reply_markup=main_menu()
            )
        except Exception as e:
            query.edit_message_text(f"Error: {e}", reply_markup=main_menu())
    
    elif query.data == "settings":
        s = load_settings()
        query.edit_message_text(
            f"Settings\n\n"
            f"Amount: ${s['max_amount']}\n"
            f"Stop loss: {s['stop_loss']}%\n"
            f"Take profit: {s['take_profit']}%\n"
            f"Threshold: {s['min_price_change']}%\n"
            f"Interval: {s['scan_interval']} sec\n",
            reply_markup=settings_menu()
        )
    
    elif query.data == "scanner_menu":
        query.edit_message_text("Scanner settings:", reply_markup=scanner_menu())
    
    elif query.data == "toggle_5m":
        s = load_settings()
        s["scanner_5m"] = not s.get("scanner_5m", True)
        save_settings(s)
        query.edit_message_text(f"5-min scanner: {'ON' if s['scanner_5m'] else 'OFF'}", reply_markup=scanner_menu())
    
    elif query.data == "toggle_24h":
        s = load_settings()
        s["scanner_24h"] = not s.get("scanner_24h", True)
        save_settings(s)
        query.edit_message_text(f"24h scanner: {'ON' if s['scanner_24h'] else 'OFF'}", reply_markup=scanner_menu())
    
    elif query.data == "toggle_new":
        s = load_settings()
        s["scanner_new"] = not s.get("scanner_new", True)
        save_settings(s)
        query.edit_message_text(f"New coins scanner: {'ON' if s['scanner_new'] else 'OFF'}", reply_markup=scanner_menu())
    
    elif query.data == "top24h":
        leaders = scan_24h_leaders()
        text = "Top 24h coins\n\n"
        for i, l in enumerate(leaders[:10], 1):
            text += f"{i}. {l['symbol']}: +{l['change_24h']:.1f}%\n"
        query.edit_message_text(text, reply_markup=main_menu())
    
    elif query.data == "newcoins":
        new_pairs = scan_new_pairs()
        if new_pairs:
            text = "New coins on Bybit\n\n" + "\n".join(new_pairs[:10])
        else:
            text = "No new coins at the moment"
        query.edit_message_text(text, reply_markup=main_menu())
    
    elif query.data == "positions":
        positions = load_positions()
        if not positions:
            query.edit_message_text("No open positions", reply_markup=main_menu())
            return
        text = "Your positions\n\n"
        for symbol, pos in positions.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                current = ticker['last']
                pnl = ((current - pos['buy_price']) / pos['buy_price']) * 100
                text += f"{symbol}\n  Entry: ${pos['buy_price']:.6f}\n  Current: ${current:.6f}\n  P&L: {pnl:+.1f}%\n\n"
            except:
                continue
        query.edit_message_text(text, reply_markup=main_menu())
    
    elif query.data == "buy_menu":
        query.edit_message_text("Choose coin:", reply_markup=coin_menu())
    
    elif query.data.startswith("buy_"):
        coin = query.data.replace("buy_", "")
        symbol = f"{coin}/USDT"
        amount = load_settings()["max_amount"]
        success, msg = buy_token(symbol, amount, "manual", "Manual buy")
        query.edit_message_text(msg, reply_markup=main_menu())
    
    elif query.data == "sellall":
        positions = load_positions()
        if not positions:
            query.edit_message_text("No positions", reply_markup=main_menu())
            return
        sold = []
        for symbol in list(positions.keys()):
            success, _ = sell_token(symbol)
            if success:
                sold.append(symbol)
        query.edit_message_text(f"Sold: {', '.join(sold) if sold else 'none'}", reply_markup=main_menu())
    
    elif query.data == "start":
        SCANNER_RUNNING = True
        query.edit_message_text("Scanners started", reply_markup=main_menu())
    
    elif query.data == "stop":
        SCANNER_RUNNING = False
        query.edit_message_text("Scanners stopped", reply_markup=main_menu())
    
    elif query.data == "set_amount":
        query.edit_message_text("Choose amount:", reply_markup=amount_menu())
    
    elif query.data.startswith("amount_"):
        amount = int(query.data.replace("amount_", ""))
        s = load_settings()
        s["max_amount"] = amount
        save_settings(s)
        query.edit_message_text(f"Amount set to ${amount}", reply_markup=settings_menu())
    
    elif query.data == "set_threshold":
        query.edit_message_text("Choose threshold:", reply_markup=threshold_menu())
    
    elif query.data.startswith("thresh_"):
        thresh = int(query.data.replace("thresh_", ""))
        s = load_settings()
        s["min_price_change"] = thresh
        save_settings(s)
        query.edit_message_text(f"Threshold set to {thresh}%", reply_markup=settings_menu())
    
    elif query.data == "set_sl":
        query.edit_message_text("Enter stop loss % (number)", reply_markup=None)
        context.user_data['waiting_for'] = 'sl'
    
    elif query.data == "set_tp":
        query.edit_message_text("Enter take profit % (number)", reply_markup=None)
        context.user_data['waiting_for'] = 'tp'
    
    elif query.data == "set_interval":
        query.edit_message_text("Enter scan interval in seconds", reply_markup=None)
        context.user_data['waiting_for'] = 'interval'
    
    elif query.data == "back":
        query.edit_message_text("Main menu", reply_markup=main_menu())

def handle_message(update, context):
    if 'waiting_for' in context.user_data:
        waiting = context.user_data['waiting_for']
        s = load_settings()
        try:
            value = float(update.message.text)
            if waiting == 'sl':
                s["stop_loss"] = value
                save_settings(s)
                update.message.reply_text(f"Stop loss set to {value}%", reply_markup=main_menu())
            elif waiting == 'tp':
                s["take_profit"] = value
                save_settings(s)
                update.message.reply_text(f"Take profit set to {value}%", reply_markup=main_menu())
            elif waiting == 'interval':
                s["scan_interval"] = int(value)
                save_settings(s)
                update.message.reply_text(f"Scan interval set to {int(value)} sec", reply_markup=main_menu())
        except:
            update.message.reply_text("Enter a number", reply_markup=main_menu())
        del context.user_data['waiting_for']

def main():
    global bot
    
    print("\n" + "="*40)
    print("   CRYPTO BOT v4.0")
    print("   3 SCANNERS")
    print("="*40 + "\n")
    
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"Balance: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"Connection error: {e}")
        return
    
    start_scanner()
    
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    bot = updater.bot
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CallbackQueryHandler(callback_handler))
    
    print("Bot started! Send /start in Telegram\n")
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
