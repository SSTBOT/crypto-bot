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

# ========== CONFIG ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

MAX_TRADE_AMOUNT_USDT = float(os.getenv("MAX_TRADE_AMOUNT", "10"))
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS", "10"))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT", "25"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "10"))

# ========== INIT ==========
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

POSITIONS_FILE = "positions.json"
SCANNER_RUNNING = True
bot = None

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_positions(positions):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(positions, f, indent=2)

def send_telegram(text):
    global bot
    try:
        if bot:
            bot.send_message(chat_id=CHAT_ID, text=text[:4000])
    except Exception as e:
        print(f"Telegram error: {e}")

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
            'stop_loss': price * (1 - STOP_LOSS_PERCENT / 100),
            'take_profit': price * (1 + TAKE_PROFIT_PERCENT / 100),
            'source': source
        }
        save_positions(positions)
        
        msg = f"🟢 BUY {symbol}\nPrice: ${price:.8f}\nAmount: ${amount_usdt}"
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
        
        emoji = "🟢" if profit_usdt >= 0 else "🔴"
        msg = f"{emoji} SELL {symbol}\nPrice: ${price:.8f}\nP&L: {pnl_percent:+.1f}% (${profit_usdt:+.2f})"
        send_telegram(msg)
        print(msg)
        return True, f"Sold {symbol}, P&L: {pnl_percent:+.1f}%"
    except Exception as e:
        return False, str(e)

def scan_loop():
    global SCANNER_RUNNING
    
    print("🔍 Scanner thread started")
    
    while SCANNER_RUNNING:
        try:
            # Check existing positions
            positions = load_positions()
            for symbol, pos in list(positions.items()):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current = ticker['last']
                    buy = pos['buy_price']
                    
                    if current <= pos['stop_loss']:
                        print(f"⚠️ Stop loss triggered for {symbol}")
                        sell_token(symbol)
                    elif current >= pos['take_profit']:
                        print(f"✅ Take profit triggered for {symbol}")
                        sell_token(symbol)
                except:
                    continue
            
            # Scan for new signals
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
                    
                    if change > MIN_PRICE_CHANGE:
                        volume_5m = ohlcv[1][5] * current_price
                        
                        positions = load_positions()
                        if symbol not in positions:
                            msg = f"🎯 SIGNAL: {symbol} +{change:.1f}% | Vol: ${volume_5m:,.0f}"
                            print(msg)
                            send_telegram(msg)
                            
                            buy_token(symbol, MAX_TRADE_AMOUNT_USDT, "auto")
                            
                except Exception as e:
                    continue
            
            time.sleep(SCAN_INTERVAL)
            
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(10)

def start_scanner():
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()

# ========== TELEGRAM COMMANDS ==========
def cmd_start(update, context):
    global SCANNER_RUNNING
    SCANNER_RUNNING = True
    update.message.reply_text(
        "✅ Crypto Bot Started!\n\n"
        "Commands:\n"
        "/status - Balance & positions\n"
        "/sellall - Close all positions\n"
        "/buy SYMBOL AMOUNT - Manual buy\n"
        "/sell SYMBOL - Manual sell\n"
        "/stop - Stop scanner"
    )

def cmd_stop(update, context):
    global SCANNER_RUNNING
    SCANNER_RUNNING = False
    update.message.reply_text("⏹️ Scanner stopped. Use /start to resume.")

def cmd_status(update, context):
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        
        positions = load_positions()
        total_pnl = 0
        pos_text = ""
        
        for symbol, pos in positions.items():
            ticker = exchange.fetch_ticker(symbol)
            current = ticker['last']
            pnl = ((current - pos['buy_price']) / pos['buy_price']) * 100
            total_pnl += (current - pos['buy_price']) * pos['quantity']
            pos_text += f"\n{symbol}: {pnl:+.1f}%"
        
        update.message.reply_text(
            f"💰 Balance: ${usdt:.2f}\n"
            f"📊 Positions: {len(positions)}{pos_text}\n"
            f"📈 Total P&L: ${total_pnl:+.2f}"
        )
    except Exception as e:
        update.message.reply_text(f"Error: {e}")

def cmd_sellall(update, context):
    positions = load_positions()
    sold = []
    for symbol in list(positions.keys()):
        success, _ = sell_token(symbol)
        if success:
            sold.append(symbol)
    update.message.reply_text(f"Sold {len(sold)} positions: {', '.join(sold) if sold else 'none'}")

def cmd_buy(update, context):
    args = context.args
    if len(args) < 1:
        update.message.reply_text("Usage: /buy SYMBOL [amount]\nExample: /buy BTC 20")
        return
    
    symbol = args[0].upper()
    if not symbol.endswith('/USDT'):
        symbol = f"{symbol}/USDT"
    
    amount = MAX_TRADE_AMOUNT_USDT
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
    
    print("""
    ╔════════════════════════════════════════════╗
    ║   🤖 CRYPTO BOT with Telegram Control     ║
    ╚════════════════════════════════════════════╝
    """)
    
    # Check connection
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"💰 Balance: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"Connection error: {e}")
        print("Check API keys in .env file")
        return
    
    # Start scanner in background
    start_scanner()
    
    # Setup Telegram bot
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    bot = updater.bot
    dp = updater.dispatcher
    
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stop", cmd_stop))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(CommandHandler("sellall", cmd_sellall))
    dp.add_handler(CommandHandler("buy", cmd_buy))
    dp.add_handler(CommandHandler("sell", cmd_sell))
    
    print("✅ Bot started! Send /start in Telegram\n")
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
