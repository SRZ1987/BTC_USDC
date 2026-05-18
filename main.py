import asyncio
import csv
import os
from datetime import datetime
import ccxt
import pandas as pd
import ta
from aiogram import Bot
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================


SYMBOL = 'BTC/USDC:USDC'
TIMEFRAME = '5m'
CANDLE_LIMIT = 200

START_BALANCE = 1000
LEVERAGE = 3
POSITION_SIZE = 0.95

TAKE_PROFIT = 0.012
STOP_LOSS = 0.007
TRAILING_STOP = 0.006

RSI_LONG_MIN = 50
RSI_LONG_MAX = 70
RSI_SHORT_MAX = 50

CHECK_INTERVAL = 30

# =========================
# ENV
# =========================

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

bot = Bot(token=BOT_TOKEN)

# =========================
# BINANCE
# =========================

exchange = ccxt.binance({
    'options': {'defaultType': 'future'}
})

# =========================
# INDICATORS
# =========================

def add_indicators(df):
    df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
    df['ema25'] = ta.trend.ema_indicator(df['close'], window=25)
    df['ema99'] = ta.trend.ema_indicator(df['close'], window=99)

    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['signal'] = macd.macd_signal()

    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()

    # ===== KDJ =====
    low_min = df['low'].rolling(9).min()
    high_max = df['high'].rolling(9).max()

    rsv = (df['close'] - low_min) / (high_max - low_min) * 100

    k = []
    d = []

    k_val = 50
    d_val = 50

    for val in rsv.fillna(50):
        k_val = (2/3) * k_val + (1/3) * val
        d_val = (2/3) * d_val + (1/3) * k_val
        k.append(k_val)
        d.append(d_val)

    df['k'] = k
    df['d'] = d
    df['j'] = 3 * df['k'] - 2 * df['d']

    return df

# =========================
# STRATEGY
# =========================

def get_signal(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    long_signal = (
        last['ema7'] > last['ema25'] > last['ema99']
        and prev['macd'] < prev['signal']
        and last['macd'] > last['signal']
        and RSI_LONG_MIN < last['rsi'] < RSI_LONG_MAX
        and last['j'] > last['k'] > last['d']
    )

    short_signal = (
        last['ema7'] < last['ema25'] < last['ema99']
        and prev['macd'] > prev['signal']
        and last['macd'] < last['signal']
        and last['rsi'] < RSI_SHORT_MAX
        and last['j'] < last['k'] < last['d']
    )

    if long_signal:
        return 'LONG'

    if short_signal:
        return 'SHORT'

    return None

# =========================
# PAPER TRADER
# =========================

class PaperTrader:
    def __init__(self, balance):
        self.balance = balance
        self.position = None

    def open_position(self, side, price):
        amount = (self.balance * POSITION_SIZE * LEVERAGE) / price

        self.position = {
            'side': side,
            'entry': price,
            'amount': amount,
            'time': datetime.now(),
            'max_price': price,
            'min_price': price
        }

        return self.position

    def update_trailing(self, price):
        if not self.position:
            return

        if self.position['side'] == 'LONG':
            self.position['max_price'] = max(self.position['max_price'], price)
        else:
            self.position['min_price'] = min(self.position['min_price'], price)

    def should_close(self, price):
        entry = self.position['entry']
        side = self.position['side']

        if side == 'LONG':
            pnl = (price - entry) / entry
            trailing = (price - self.position['max_price']) / self.position['max_price']

            if pnl >= TAKE_PROFIT:
                return True
            if pnl <= -STOP_LOSS:
                return True
            if trailing <= -TRAILING_STOP:
                return True

        else:
            pnl = (entry - price) / entry
            trailing = (self.position['min_price'] - price) / self.position['min_price']

            if pnl >= TAKE_PROFIT:
                return True
            if pnl <= -STOP_LOSS:
                return True
            if trailing <= -TRAILING_STOP:
                return True

        return False

    def close_position(self, price):
        entry = self.position['entry']
        amount = self.position['amount']
        side = self.position['side']

        if side == 'LONG':
            pnl = (price - entry) * amount
        else:
            pnl = (entry - price) * amount

        self.balance += pnl

        trade = {
            'time': datetime.now(),
            'side': side,
            'entry': entry,
            'exit': price,
            'pnl': pnl,
            'balance': self.balance
        }

        with open('trades.csv', 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=trade.keys())
            if f.tell() == 0:
                writer.writeheader()
            writer.writerow(trade)

        self.position = None
        return trade

# =========================
# TELEGRAM
# =========================

async def send_message(text):
    await bot.send_message(CHAT_ID, text)

# =========================
# LOOP
# =========================

trader = PaperTrader(START_BALANCE)

async def run_bot():
    await send_message('🤖 Bot запущен (RSI + KDJ + Trailing)')

    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)

            df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
            df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)

            df = add_indicators(df)

            signal = get_signal(df)
            price = float(df.iloc[-1]['close'])

            print(f"PRICE {price} | RSI {df.iloc[-1]['rsi']:.2f}")

            # OPEN
            if signal and trader.position is None:
                trader.open_position(signal, price)

                await send_message(
                    f"🚀 {signal} BTC/USDC\n"
                    f"Price: {price}\n"
                    f"Balance: {trader.balance:.2f}"
                )

            # MANAGE
            if trader.position:
                trader.update_trailing(price)

                if trader.should_close(price):
                    trade = trader.close_position(price)

                    await send_message(
                        f"💰 CLOSED\n"
                        f"Side: {trade['side']}\n"
                        f"Entry: {trade['entry']}\n"
                        f"Exit: {trade['exit']}\n"
                        f"PNL: {trade['pnl']:.2f}\n"
                        f"Balance: {trade['balance']:.2f}"
                    )

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            await send_message(f"❌ Error: {e}")
            await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(run_bot())