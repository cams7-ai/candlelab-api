"""Causal Decimal indicators; Wilder seeds use arithmetic means."""

from collections import deque
from decimal import Decimal


class Wilder:
    def __init__(self, period):
        self.period, self.count, self.total, self.value = period, 0, Decimal(0), None

    def update(self, value):
        self.count += 1
        if self.value is None:
            self.total += value
            if self.count == self.period:
                self.value = self.total / self.period
        else:
            self.value = (self.value * (self.period - 1) + value) / self.period
        return self.value


class Volatility:
    def __init__(self, period):
        self.previous = None
        self.tr, self.plus, self.minus, self.adx = [Wilder(period) for _ in range(4)]

    def update(self, candle):
        old, self.previous = self.previous, candle
        if old is None:
            return None, None
        tr = max(
            candle.high - candle.low, abs(candle.high - old.close), abs(candle.low - old.close)
        )
        up, down = candle.high - old.high, old.low - candle.low
        atr = self.tr.update(tr)
        plus = self.plus.update(max(up, Decimal(0)) if up > down else Decimal(0))
        minus = self.minus.update(max(down, Decimal(0)) if down > up else Decimal(0))
        if atr is None:
            return None, None
        # Common TR denominator cancels in DX, including flat ranges.
        dx = 100 * abs(plus - minus) / (plus + minus) if plus + minus else Decimal(0)
        return atr, self.adx.update(dx)


class Bands:
    def __init__(self, period, deviations):
        self.period, self.deviations = period, Decimal(deviations)
        self.history = deque(maxlen=period)

    def update(self, close):
        self.history.append(close)
        if len(self.history) < self.period:
            return None, None, None
        mean = sum(self.history) / self.period
        sigma = (sum((x - mean) ** 2 for x in self.history) / self.period).sqrt()
        return mean, mean - self.deviations * sigma, mean + self.deviations * sigma


class MACD:
    def __init__(self):
        self.fast, self.slow, self.signal = (
            MovingAverage(12, True),
            MovingAverage(26, True),
            MovingAverage(9, True),
        )

    def update(self, close):
        fast, slow = self.fast.update(close), self.slow.update(close)
        if slow is None:
            return None, None
        macd = fast - slow
        return macd, self.signal.update(macd)


class MovingAverage:
    def __init__(self, window, exponential=False):
        self.window = window
        self.exponential = exponential
        self.history = deque()
        self.total = Decimal(0)
        self.value = None

    def update(self, close):
        if self.exponential and self.value is not None:
            alpha = Decimal(2) / (self.window + 1)
            self.value = alpha * close + (1 - alpha) * self.value
        else:
            self.history.append(close)
            self.total += close
            if len(self.history) > self.window:
                self.total -= self.history.popleft()
            if len(self.history) == self.window:
                self.value = self.total / self.window
        return self.value


class RSI:
    def __init__(self, period):
        self.period = period
        self.previous = None
        self.count = 0
        self.gain = Decimal(0)
        self.loss = Decimal(0)

    def update(self, close):
        if self.previous is None:
            self.previous = close
            return None
        delta = close - self.previous
        self.previous = close
        self.count += 1
        gain, loss = max(delta, Decimal(0)), max(-delta, Decimal(0))
        if self.count <= self.period:
            self.gain += gain
            self.loss += loss
            if self.count < self.period:
                return None
            self.gain /= self.period
            self.loss /= self.period
        else:
            self.gain = (self.gain * (self.period - 1) + gain) / self.period
            self.loss = (self.loss * (self.period - 1) + loss) / self.period
        return (
            Decimal(50) if self.gain + self.loss == 0 else 100 * self.gain / (self.gain + self.loss)
        )
