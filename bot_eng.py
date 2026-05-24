#!/usr/bin/env python3
import ccxt
import time
import json
import os
import threading
from datetime import datetime
from telegram.ext import Updater, CommandHandler
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
            'source': source
        }
        save_positions(positions)
        
        msg = f"BUY {symbol}\nPrice: ${price:.8f}\nAmount: ${amount_usdt}"
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

def scan_loop():
    global SCANNER_RUNNING, settings
    
    print("Scanner thread started")
    
    while SCANNER_RUNNING:
        try:
            settings = load_settings()
            
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
                            msg = f"SIGNAL: {symbol} +{change:.1f}% | Vol: ${volume_5m:,.0f}"
                            print(msg)
                            send_telegram(msg)
                            buy_token(symbol, settings["max_amount"], "auto")
                            
                except Exception as e:
                    continue
            
            time.sleep(settings["scan_interval"])
            
        except Exception as e:
            print(f"Scan error: {e}")
            time.sleep(10)

def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()

def cmd_start(update, context):
    global SCANNER_RUNNING
    SCANNER_RUNNING = True
    update.message.reply_text(
        "Crypto Bot Started!\n\n"
        "Commands:\n"
        "/status - Balance and positions\n"
        "/settings - Show settings\n"
        "/set amount 10 - Change trade amount\n"
        "/set sl 15 - Change stop loss (%)\n"
        "/set tp 25 - Change take profit (%)\n"
        "/set threshold 8 - Change signal threshold (%)\n"
        "/set interval 60 - Change scan interval\n"
        "/sellall - Close all positions\n"
        "/buy SYMBOL AMOUNT - Manual buy\n"
        "/sell SYMBOL - Manual sell\n"
        "/stop - Stop scanner"
    )

def cmd_stop(update, context):
    global SCANNER_RUNNING
    SCANNER_RUNNING = False
    update.message.reply_text("Scanner stopped. Use /start to resume.")

def cmd_status(update, context):
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
        
        update.message.reply_text(
            f"Balance: ${usdt:.2f}\n"
            f"Positions: {len(positions)}{pos_text}\n"
            f"Total P&L: ${total_pnl:+.2f}"
        )
    except Exception as e:
        update.message.reply_text(f"Error: {e}")

def cmd_settings(update, context):
    s = load_settings()
    update.message.reply_text(
        f"Current Settings:\n"
        f"Max amount: ${s['max_amount']}\n"
        f"Stop loss: {s['stop_loss']}%\n"
        f"Take profit: {s['take_profit']}%\n"
        f"Signal threshold: {s['min_price_change']}%\n"
        f"Scan interval: {s['scan_interval']} sec\n\n"
        f"To change: /set amount 10"
    )

def cmd_set(update, context):
    args = context.args
    if len(args) < 2:
        update.message.reply_text("Usage: /set <param> <value>")
        return
    
    param = args[0].lower()
    try:
        value = float(args[1])
    except:
        update.message.reply_text("Value must be a number")
        return
    
    s = load_settings()
    
    if param in ["amount", "max_amount"]:
        s["max_amount"] = value
        save_settings(s)
        update.message.reply_text(f"Max amount set to ${value}")
    elif param in ["sl", "stop_loss"]:
        s["stop_loss"] = value
        save_settings(s)
        update.message.reply_text(f"Stop loss set to {value}%")
    elif param in ["tp", "take_profit"]:
        s["take_profit"] = value
        save_settings(s)
        update.message.reply_text(f"Take profit set to {value}%")
    elif param in ["threshold", "min_price_change"]:
        s["min_price_change"] = value
        save_settings(s)
        update.message.reply_text(f"Signal threshold set to {value}%")
    elif param in ["interval", "scan_interval"]:
        s["scan_interval"] = int(value)
        save_settings(s)
        update.message.reply_text(f"Scan interval set to {int(value)} seconds")
    else:
        update.message.reply_text(f"Unknown: {param}\nUse: amount, sl, tp, threshold, interval")

def cmd_sellall(update, context):
    positions = load_positions()
    if not positions:
        update.message.reply_text("No positions to sell")
        return
    sold = []
    for symbol in list(positions.keys()):
        success, _ = sell_token(symbol)
        if success:
            sold.append(symbol)
    update.message.reply_text(f"Sold {len(sold)}: {', '.join(sold) if sold else 'none'}")

def cmd_buy(update, context):
    args = context.args
    if len(args) < 1:
        update.message.reply_text("Usage: /buy SYMBOL [amount]\nExample: /buy BTC 20")
        return
    symbol = args[0].upper()
    if not symbol.endswith('/USDT'):
        symbol = f"{symbol}/USDT"
    amount = settings["max_amount"]
    if len(args) > 1:
        try:
            amount = float(args[1])
        except:
            pass
    success, msg = buy_token(symbol, amount, "manual")
    update.message.reply_text(msg)

def cmd_sell(update, context):
    args = context.args
    if len(args) < 1:
        update.message.reply_text("Usage: /sell SYMBOL\nExample: /sell BTC")
        return
    symbol = args[0].upper()
    if not symbol.endswith('/USDT'):
        symbol = f"{symbol}/USDT"
    success, msg = sell_token(symbol)
    update.message.reply_text(msg)

def main():
    global bot
    
    print("\n" + "="*40)
    print("   CRYPTO BOT v2.0")
    print("="*40 + "\n")
    
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"Balance: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"Connection error: {e}")
        print("Check API keys in .env file")
        return
    
    start_scanner()
    
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    bot = updater.bot
    dp = updater.dispatcher
    
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stop", cmd_stop))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(CommandHandler("settings", cmd_settings))
    dp.add_handler(CommandHandler("set", cmd_set))
    dp.add_handler(CommandHandler("sellall", cmd_sellall))
    dp.add_handler(CommandHandler("buy", cmd_buy))
    dp.add_handler(CommandHandler("sell", cmd_sell))
    
    print("Bot started! Send /start in Telegram\n")
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
