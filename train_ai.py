#!/usr/bin/env python3
"""
Train LightGBM AI on real market data from Bybit
Запусти один раз для обучения модели
"""

import ccxt
import pandas as pd
from ai_predictor import LightGBMPredictor
from datetime import datetime

def main():
    print("="*50)
    print("Training LightGBM AI on Bybit Data")
    print("="*50)
    
    # Initialize exchange
    exchange = ccxt.bybit({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })
    
    # Initialize predictor
    predictor = LightGBMPredictor()
    
    # Collect data for multiple symbols
    symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT', 'XRP/USDT']
    all_data = []
    
    for symbol in symbols:
        print(f"\n📊 Collecting data for {symbol}...")
        df = predictor.collect_training_data(exchange, symbol, days=14)
        all_data.append(df)
    
    # Combine all data
    combined_data = pd.concat(all_data, ignore_index=True)
    print(f"\n📊 Total training samples: {len(combined_data)}")
    
    # Train model
    predictor.train(combined_data)
    
    print("\n✅ AI Training Complete!")
    print("🚀 You can now start the bot with: python trading_bot_ai.py")

if __name__ == "__main__":
    main()
