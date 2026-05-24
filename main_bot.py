#!/usr/bin/env python3
import ccxt
import asyncio
import time
import json
import os
from datetime import datetime
from dotenv import load_dotenv

# Load keys from .env file
load_dotenv()

# Telegram
try:
    from telegram import Bot
    TELEGRAM_ENABLED = True
except:
    TELEGRAM_ENABLED = False
    print("WARNING: python-telegram-bot not installed")

# ========== CONFIGURATION ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

MAX_TRADE_AMOUNT_USDT = 5
STOP_LOSS_PERCENT = 3
TAKE_PROFIT_PERCENT = 25
SCAN_INTERVAL = 30

# Check required keys
if not BYBIT_API_KEY or BYBIT_API_KEY == "НОВЫЙ_API_КЛЮЧ":
    print("ERROR: Please set BYBIT_API_KEY in .env file")
    exit(1)

if not BYBIT_API_SECRET or BYBIT_API_SECRET == "НОВЫЙ_SECRET_КЛЮЧ":
    print("ERROR: Please set BYBIT_API_SECRET in .env file")
    exit(1)

# ========== INIT ==========
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

if TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "НОВЫЙ_ТОКЕН_ОТ_BOTFATHER":
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
else:
    TELEGRAM_ENABLED = False
    print("WARNING: Telegram not configured. Signals will only appear in console.")

POSITIONS_FILE = "positions.json"

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_positions(positions):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(positions, f, indent=2)

async def send_telegram(text):
    if TELEGRAM_ENABLED:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=text[:4000])
        except Exception as e:
            print(f"Telegram error: {e}")

async def scan_and_trade():
    print(f"\n[SCAN] {datetime.now().strftime('%H:%M:%S')}")
    
    try:
        positions = load_positions()
        
        # Check existing positions
        for symbol, pos in list(positions.items()):
            try:
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                buy_price = pos['buy_price']
                pnl = ((current_price - buy_price) / buy_price) * 100
                
                if current_price <= pos['stop_loss']:
                    qty = exchange.amount_to_precision(symbol, pos['quantity'])
                    exchange.create_market_sell_order(symbol, qty)
                    del positions[symbol]
                    save_positions(positions)
                    await send_telegram(f"SELL {symbol}\nP&L: {pnl:.1f}% (Stop Loss)")
                    print(f"SELL {symbol} by stop loss")
                elif current_price >= pos['take_profit']:
                    qty = exchange.amount_to_precision(symbol, pos['quantity'])
                    exchange.create_market_sell_order(symbol, qty)
                    del positions[symbol]
                    save_positions(positions)
                    await send_telegram(f"SELL {symbol}\nP&L: {pnl:.1f}% (Take Profit)")
                    print(f"SELL {symbol} by take profit")
            except Exception as e:
                print(f"Check error {symbol}: {e}")
        
        # Scan new pairs
        markets = exchange.load_markets()
        pairs = [s for s in markets if markets[s]['spot'] and s.endswith('/USDT')]
        pairs = pairs[:100]
        
        for symbol in pairs:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=2)
                if len(ohlcv) < 2:
                    continue
                
                old_price = ohlcv[0][1]
                current_price = ohlcv[1][4]
                change = ((current_price - old_price) / old_price) * 100
                
                if change > 10:
                    volume_5m = ohlcv[1][5] * current_price
                    print(f"SIGNAL: {symbol} | +{change:.1f}% | Vol: ${volume_5m:,.0f}")
                    
                    await send_telegram(
                        f"SIGNAL!\n{symbol}\nGain: +{change:.1f}%\nVolume: ${volume_5m:,.0f}"
                    )
                    
                    if symbol not in positions:
                        quantity = MAX_TRADE_AMOUNT_USDT / current_price
                        qty = exchange.amount_to_precision(symbol, quantity)
                        exchange.create_market_buy_order(symbol, qty)
                        
                        positions[symbol] = {
                            'symbol': symbol,
                            'buy_price': current_price,
                            'quantity': float(qty),
                            'amount_usdt': MAX_TRADE_AMOUNT_USDT,
                            'stop_loss': current_price * (1 - STOP_LOSS_PERCENT / 100),
                            'take_profit': current_price * (1 + TAKE_PROFIT_PERCENT / 100),
                            'buy_time': datetime.now().isoformat()
                        }
                        save_positions(positions)
                        await send_telegram(
                            f"BUY {symbol}\nPrice: ${current_price:.8f}\nTP: +{TAKE_PROFIT_PERCENT}% | SL: -{STOP_LOSS_PERCENT}%"
                        )
                        print(f"BUY {symbol} at ${current_price:.8f}")
            except Exception as e:
                continue
    except Exception as e:
        print(f"Scan error: {e}")

async def main():
    print("\n" + "="*40)
    print("   CRYPTO TRADING BOT")
    print("="*40 + "\n")
    
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"Balance: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"Connection error: {e}")
        return
    
    print(f"Bot started! Interval: {SCAN_INTERVAL} sec\n")
    
    while True:
        try:
            await scan_and_trade()
            await asyncio.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
