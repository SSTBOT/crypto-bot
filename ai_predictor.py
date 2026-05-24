#!/usr/bin/env python3
"""
LightGBM AI Predictor for Crypto Trading
Обучается на исторических данных и предсказывает вероятность роста токена
"""

import lightgbm as lgb
import pandas as pd
import numpy as np
import joblib
import os
from datetime import datetime, timedelta
from sklearn.model_selection import TimeSeriesSplit

class LightGBMPredictor:
    """AI-предсказатель для криптотрейдинга"""
    
    def __init__(self, model_path="models/lgb_model.pkl"):
        self.model = None
        self.model_path = model_path
        self.is_trained = False
        self.feature_names = [
            'price_change_5m',      # Изменение цены за 5 минут
            'price_change_15m',     # Изменение цены за 15 минут
            'volume_ratio',         # Отношение текущего объема к среднему
            'volatility',           # Волатильность за 5 минут
            'rsi_14',               # RSI индикатор
            'macd',                 # MACD индикатор
            'bid_ask_spread',       # Спред между ценой покупки и продажи
            'volume_trend',         # Тренд объема (5 мин vs 15 мин)
            'price_position',       # Позиция цены относительно минимума/максимума
        ]
        
        # Загружаем модель если есть
        if os.path.exists(model_path):
            try:
                self.model = joblib.load(model_path)
                self.is_trained = True
                print("✅ LightGBM model loaded from", model_path)
            except Exception as e:
                print(f"⚠️ Failed to load model: {e}")
        else:
            print("📋 No existing model found. Will train on new data.")
    
    def create_features(self, ohlcv, ticker_data):
        """
        Создаёт признаки из рыночных данных для предсказания
        
        Args:
            ohlcv: list of candles [[timestamp, open, high, low, close, volume], ...]
            ticker_data: dict with ticker info (bid, ask, last, etc.)
        
        Returns:
            dict: features for prediction
        """
        if len(ohlcv) < 20:
            return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # Price changes
        price_change_5m = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2] * 100 if len(df) >= 2 else 0
        price_change_15m = (df['close'].iloc[-1] - df['close'].iloc[-4]) / df['close'].iloc[-4] * 100 if len(df) >= 4 else 0
        
        # Volume analysis
        avg_volume_30m = df['volume'].tail(6).mean()  # 6 * 5min = 30min
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / avg_volume_30m if avg_volume_30m > 0 else 1
        
        volume_trend = (df['volume'].iloc[-1] - df['volume'].iloc[-4]) / df['volume'].iloc[-4] * 100 if len(df) >= 4 else 0
        
        # Volatility (standard deviation of returns)
        returns = df['close'].pct_change()
        volatility = returns.tail(5).std() * 100  # 5 periods = 25 minutes
        
        # Technical indicators
        rsi_14 = self._calculate_rsi(df['close'])
        macd = self._calculate_macd(df['close'])
        
        # Order book features (if available)
        bid = ticker_data.get('bid', df['close'].iloc[-1])
        ask = ticker_data.get('ask', df['close'].iloc[-1])
        bid_ask_spread = (ask - bid) / bid * 100 if bid > 0 else 0
        
        # Price position within recent range
        high_30m = df['high'].tail(6).max()
        low_30m = df['low'].tail(6).min()
        current_price = df['close'].iloc[-1]
        if high_30m != low_30m:
            price_position = (current_price - low_30m) / (high_30m - low_30m) * 100
        else:
            price_position = 50
        
        features = {
            'price_change_5m': price_change_5m,
            'price_change_15m': price_change_15m,
            'volume_ratio': volume_ratio,
            'volatility': volatility,
            'rsi_14': rsi_14,
            'macd': macd,
            'bid_ask_spread': bid_ask_spread,
            'volume_trend': volume_trend,
            'price_position': price_position,
        }
        
        return features
    
    def _calculate_rsi(self, prices, period=14):
        """Calculate RSI indicator"""
        if len(prices) < period + 1:
            return 50
        
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi.iloc[-1] if not rsi.empty else 50
    
    def _calculate_macd(self, prices, fast=12, slow=26, signal=9):
        """Calculate MACD indicator"""
        if len(prices) < slow + signal:
            return 0
        
        ema_fast = prices.ewm(span=fast, adjust=False).mean()
        ema_slow = prices.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
        macd_histogram = macd_line - macd_signal
        
        return macd_histogram.iloc[-1] if not macd_histogram.empty else 0
    
    def predict(self, features):
        """
        Predict probability of price increase
        
        Returns:
            proba (float): Probability of increase (0-1)
            signal (bool): True if probability > threshold
            confidence (str): Confidence level
        """
        if not self.is_trained or self.model is None:
            return 0.5, False, "Model not trained"
        
        try:
            # Create feature vector
            X = pd.DataFrame([features])[self.feature_names]
            
            # Predict
            proba = self.model.predict_proba(X)[0][1]
            
            # Determine signal based on probability
            if proba > 0.7:
                signal = True
                confidence = "HIGH"
            elif proba > 0.55:
                signal = True
                confidence = "MEDIUM"
            else:
                signal = False
                confidence = "LOW"
            
            return proba, signal, confidence
            
        except Exception as e:
            print(f"Prediction error: {e}")
            return 0.5, False, "Error"
    
    def predict_batch(self, features_list):
        """Predict for multiple samples"""
        if not self.is_trained or self.model is None:
            return [0.5] * len(features_list)
        
        X = pd.DataFrame(features_list)[self.feature_names]
        return self.model.predict_proba(X)[:, 1]
    
    def train(self, historical_data, target_column='future_return'):
        """
        Train the model on historical data
        
        Args:
            historical_data: DataFrame with features and target
            target_column: Name of target column (positive return = 1)
        
        Returns:
            model: Trained model
        """
        print("🔄 Starting LightGBM training...")
        
        # Prepare data
        feature_cols = [col for col in historical_data.columns if col != target_column]
        X = historical_data[feature_cols]
        y = (historical_data[target_column] > 0).astype(int)  # Binary classification
        
        print(f"📊 Training data shape: {X.shape}")
        print(f"📊 Positive samples: {y.sum()} ({y.mean()*100:.1f}%)")
        
        # Time series cross-validation
        tscv = TimeSeriesSplit(n_splits=5)
        
        # Model parameters (optimized for crypto)
        params = {
            'n_estimators': 200,
            'learning_rate': 0.05,
            'max_depth': 5,
            'min_child_samples': 20,
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'reg_alpha': 0.1,
            'reg_lambda': 0.1,
            'random_state': 42,
            'verbose': -1
        }
        
        # Train model
        self.model = lgb.LGBMClassifier(**params)
        self.model.fit(X, y)
        
        # Feature importance
        importance = pd.DataFrame({
            'feature': feature_cols,
            'importance': self.model.feature_importances_
        }).sort_values('importance', ascending=False)
        
        print("\n📊 Feature importance:")
        for _, row in importance.head(10).iterrows():
            print(f"   • {row['feature']}: {row['importance']:.0f}")
        
        # Save model
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        joblib.dump(self.model, self.model_path)
        self.is_trained = True
        
        print(f"\n✅ Model saved to {self.model_path}")
        
        return self.model
    
    def collect_training_data(self, exchange, symbol, days=30):
        """
        Collect historical data for training
        
        Args:
            exchange: CCXT exchange instance
            symbol: Trading pair (e.g., 'BTC/USDT')
            days: Number of days to collect
        
        Returns:
            DataFrame with features and target
        """
        print(f"📊 Collecting training data for {symbol}...")
        
        # Calculate number of candles needed (5 minute candles)
        limit = int(days * 24 * 60 / 5)  # ~288 candles per day
        
        # Fetch historical OHLCV
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=limit)
        
        features_list = []
        
        for i in range(20, len(ohlcv) - 1):  # Need at least 20 candles for features
            # Current window
            window = ohlcv[i-19:i+1]  # 20 candles for features
            current_price = window[-1][4]
            next_price = ohlcv[i+1][4] if i+1 < len(ohlcv) else current_price
            
            # Calculate future return (10 minutes ahead)
            future_return = (next_price - current_price) / current_price * 100
            
            # Create dummy ticker data (using last price)
            ticker_data = {'bid': current_price, 'ask': current_price, 'last': current_price}
            
            # Create features
            features = self.create_features(window, ticker_data)
            
            if features:
                features['future_return'] = future_return
                features_list.append(features)
        
        df = pd.DataFrame(features_list)
        
        print(f"✅ Collected {len(df)} samples")
        print(f"📊 Future return distribution:")
        print(f"   Positive: {(df['future_return'] > 0).sum()} ({((df['future_return'] > 0).sum()/len(df))*100:.1f}%)")
        print(f"   Negative: {(df['future_return'] <= 0).sum()}")
        
        return df

# ========== DEMO ==========
if __name__ == "__main__":
    print("="*50)
    print("LightGBM AI Predictor - Test Mode")
    print("="*50)
    
    # Initialize predictor
    predictor = LightGBMPredictor()
    
    # If no model exists, create sample training data
    if not predictor.is_trained:
        print("\n📋 Creating sample training data for demonstration...")
        
        # Generate synthetic data (in real scenario, use real exchange data)
        np.random.seed(42)
        n_samples = 1000
        
        sample_data = pd.DataFrame({
            'price_change_5m': np.random.randn(n_samples) * 2,
            'price_change_15m': np.random.randn(n_samples) * 3,
            'volume_ratio': np.random.exponential(2, n_samples),
            'volatility': np.random.exponential(1, n_samples),
            'rsi_14': np.random.uniform(20, 80, n_samples),
            'macd': np.random.randn(n_samples) * 0.5,
            'bid_ask_spread': np.random.exponential(0.1, n_samples),
            'volume_trend': np.random.randn(n_samples) * 5,
            'price_position': np.random.uniform(0, 100, n_samples),
            'future_return': np.random.randn(n_samples) * 2
        })
        
        # Train on sample data
        predictor.train(sample_data)
    
    # Test prediction
    print("\n📋 Testing prediction on sample features...")
    test_features = {
        'price_change_5m': 2.5,
        'price_change_15m': 5.0,
        'volume_ratio': 3.2,
        'volatility': 1.5,
        'rsi_14': 65,
        'macd': 0.3,
        'bid_ask_spread': 0.05,
        'volume_trend': 10,
        'price_position': 70,
    }
    
    proba, signal, confidence = predictor.predict(test_features)
    print(f"\n📊 Prediction result:")
    print(f"   Probability of increase: {proba*100:.1f}%")
    print(f"   Signal: {'BUY' if signal else 'NO ACTION'}")
    print(f"   Confidence: {confidence}")
    
    print("\n✅ LightGBM AI Predictor is ready!")
