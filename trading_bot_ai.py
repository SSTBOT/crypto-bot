#!/usr/bin/env python3
"""
Crypto Trading Bot with LightGBM AI
Управление из Telegram + AI-предсказания
"""

import ccxt
import asyncio
import json
import os
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ai_predictor import LightGBMPredictor
from dotenv import load_dotenv

load_dotenv()

# ========== CONFIG ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Trading settings
MAX_TRADE_AMOUNT_USDT = float(os.getenv("MAX_TRADE_AMOUNT", "10"))
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS", "10"))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT", "25"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "8"))
AI_CONFIDENCE_THRESHOLD = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.6"))

# ========== INIT ==========
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# Initialize AI Predictor
ai_predictor = LightGBMPredictor()

POSITIONS_FILE = "positions.json"
SCANNER_RUNNING = True

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_positions(positions):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(positions, f, indent=2)

async def send_telegram(text):
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=CHAT_ID, text=text[:4000])
    except Exception as e:
        print(f"Telegram error: {e}")

async def buy_token(symbol, amount_usdt, source="auto", ai_confidence=None):
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
            'source': source,
            'ai_confidence': ai_confidence
        }
        save_positions(positions)
        
        ai_text = f" | AI confidence: {ai_confidence*100:.0f}%" if ai_confidence else ""
        await send_telegram(f"🟢 BUY {symbol}\nPrice: ${price:.8f}\nAmount: ${amount_usdt}{ai_text}")
        return True, f"Bought {symbol} at ${price:.8f}"
    except Exception as e:
        return False, str(e)

async def sell_token(symbol):
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
        
        await send_telegram(f"🔴 SELL {symbol}\nPrice: ${price:.8f}\nP&L: {pnl_percent:+.1f}% (${profit_usdt:+.2f})")
        return True, f"Sold {symbol}, P&L: {pnl_percent:+.1f}%"
    except Exception as e:
        return False, str(e)

async def scan_and_trade():
    global SCANNER_RUNNING
    
    while SCANNER_RUNNING:
        try:
            # Check existing positions
            positions = load_positions()
            for symbol, pos in list(positions.items()):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    current = ticker['last']
                    buy = pos['buy_price']
                    pnl = ((current - buy) / buy) * 100
                    
                    if current <= pos['stop_loss']:
                        await sell_token(symbol)
                    elif current >= pos['take_profit']:
                        await sell_token(symbol)
                except:
                    continue
            
            # Scan for new signals
            markets = exchange.load_markets()
            pairs = [s for s in markets if markets[s]['spot'] and s.endswith('/USDT')]
            pairs = pairs[:150]
            
            for symbol in pairs:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=25)
                    if len(ohlcv) < 20:
                        continue
                    
                    old_price = ohlcv[-2][1]
                    current_price = ohlcv[-1][4]
                    change = ((current_price - old_price) / old_price) * 100
                    
                    if change > MIN_PRICE_CHANGE:
                        ticker = exchange.fetch_ticker(symbol)
                        
                        # AI Prediction
                        features = ai_predictor.create_features(ohlcv, ticker)
                        ai_proba = None
                        ai_signal = False
                        ai_confidence_str = ""
                        
                        if features:
                            ai_proba, ai_signal, ai_conf = ai_predictor.predict(features)
                            ai_confidence_str = f" | AI: {ai_proba*100:.0f}%"
                            
                            # Only buy if AI also sees potential (or override)
                            if not ai_signal and change < 15:
                                continue
                        
                        positions = load_positions()
                        if symbol not in positions:
                            volume_5m = ohlcv[-1][5] * current_price
                            print(f"🎯 SIGNAL: {symbol} +{change:.1f}%{ai_confidence_str}")
                            
                            await send_telegram(
                                f"🚨 SIGNAL\n{symbol}\n"
                                f"Gain: +{change:.1f}%\n"
                                f"Volume: ${volume_5m:,.0f}{ai_confidence_str}"
                            )
                            
                            await buy_token(symbol, MAX_TRADE_AMOUNT_USDT, "auto+ai", ai_proba)
                            
                except Exception as e:
                    continue
            
            await asyncio.sleep(SCAN_INTERVAL)
            
        except Exception as e:
            print(f"Scanner error: {e}")
            await asyncio.sleep(10)

# ========== TELEGRAM COMMANDS ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SCANNER_RUNNING
    SCANNER_RUNNING = True
    await update.message.reply_text(
        "✅ Bot started with AI!\n\n"
        "Commands:\n"
        "/status - Balance & positions\n"
        "/sellall - Close all positions\n"
        "/buy SYMBOL AMOUNT - Manual buy\n"
        "/sell SYMBOL - Manual sell\n"
        "/ai_status - AI model info\n"
        "/stop - Stop scanner"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SCANNER_RUNNING
    SCANNER_RUNNING = False
    await update.message.reply_text("⏹️ Scanner stopped")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        
        ai_status = "✅ Trained" if ai_predictor.is_trained else "⚠️ Not trained"
        
        await update.message.reply_text(
            f"💰 Balance: ${usdt:.2f}\n"
            f"📊 Positions: {len(positions)}{pos_text}\n"
            f"📈 Total P&L: ${total_pnl:+.2f}\n"
            f"🧠 AI: {ai_status}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def sellall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = load_positions()
    sold = []
    for symbol in list(positions.keys()):
        success, _ = await sell_token(symbol)
        if success:
            sold.append(symbol)
    await update.message.reply_text(f"Sold {len(sold)} positions: {', '.join(sold)}")

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /buy SYMBOL [amount]")
        return
    
    symbol = context.args[0].upper()
    if not symbol.endswith('/USDT'):
        symbol = f"{symbol}/USDT"
    
    amount = MAX_TRADE_AMOUNT_USDT
    if len(context.args) > 1:
        try:
            amount = float(context.args[1])
        except:
            pass
    
    success, msg = await buy_token(symbol, amount, "manual")
    await update.message.reply_text(msg)

async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /sell SYMBOL")
        return
    
    symbol = context.args[0].upper()
    if not symbol.endswith('/USDT'):
        symbol = f"{symbol}/USDT"
    
    success, msg = await sell_token(symbol)
    await update.message.reply_text(msg)

async def ai_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ai_predictor.is_trained:
        await update.message.reply_text(
            "🧠 **AI Model Status**\n\n"
            "✅ Model is trained and ready\n"
            f"📊 Features: {', '.join(ai_predictor.feature_names[:5])}...\n\n"
            "The AI analyzes each signal and adds confidence score.\n"
            "Signals with AI confidence >60% are prioritized.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "⚠️ **AI Model Not Trained**\n\n"
            "To train the AI:\n"
            "1. Collect historical data\n"
            "2. Run: python train_ai.py\n\n"
            "Currently trading without AI predictions.",
            parse_mode='Markdown'
        )

# ========== MAIN ==========
async def main():
    print("""
    ╔════════════════════════════════════════════╗
    ║   🤖 CRYPTO BOT with LightGBM AI          ║
    ║   Telegram Control + AI Predictions       ║
    ╚════════════════════════════════════════════╝
    """)
    
    # Check connection
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free']
        print(f"💰 Balance: ${usdt:.2f} USDT")
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return
    
    # AI Status
    if ai_predictor.is_trained:
        print("✅ LightGBM AI: TRAINED and ACTIVE")
    else:
        print("⚠️ LightGBM AI: NOT TRAINED (trading without AI)")
        print("   To train: run 'python train_ai.py'")
    
    # Start scanner in background
    asyncio.create_task(scan_and_trade())
    
    # Start Telegram bot
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("sellall", sellall_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("sell", sell_command))
    application.add_handler(CommandHandler("ai_status", ai_status_command))
    
    print("\n✅ Bot started! Send /start in Telegram\n")
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        global SCANNER_RUNNING
        SCANNER_RUNNING = False
        await application.updater.stop()
        await application.stop()
        print("\n🛑 Bot stopped")

if __name__ == "__main__":
    asyncio.run(main())
