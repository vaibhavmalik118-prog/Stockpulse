// Technical indicators over OHLC candles. Every function returns an array aligned with the
// input; values are null until enough candles exist.

/** Group 1-minute candles into `minutes`-minute candles. */
export function resample(candles, minutes) {
  if (minutes <= 1) return candles;
  const size = minutes * 60;
  const out = [];
  for (const c of candles) {
    const start = c.start - (c.start % size);
    const last = out[out.length - 1];
    if (last && last.start === start) {
      last.high = Math.max(last.high, c.high);
      last.low = Math.min(last.low, c.low);
      last.close = c.close;
      last.volume = (last.volume || 0) + (c.volume || 0);
    } else {
      out.push({ ...c, start, volume: c.volume || 0 });
    }
  }
  return out;
}

export function sma(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

export function ema(values, period) {
  const out = new Array(values.length).fill(null);
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < values.length; i++) {
    if (i === period - 1) {
      prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period; // seed with the SMA
    } else if (i >= period) {
      prev = values[i] * k + prev * (1 - k);
    }
    out[i] = prev;
  }
  return out;
}

/** Bollinger Bands: SMA ± mult standard deviations. */
export function bollinger(values, period = 20, mult = 2) {
  const mid = sma(values, period);
  const upper = new Array(values.length).fill(null);
  const lower = new Array(values.length).fill(null);
  for (let i = period - 1; i < values.length; i++) {
    let v = 0;
    for (let j = i - period + 1; j <= i; j++) v += (values[j] - mid[i]) ** 2;
    const sd = Math.sqrt(v / period);
    upper[i] = mid[i] + mult * sd;
    lower[i] = mid[i] - mult * sd;
  }
  return { mid, upper, lower };
}

/** Volume-weighted average price, restarting each calendar day. */
export function vwap(candles) {
  const out = [];
  let pv = 0;
  let vol = 0;
  let day = null;
  for (const c of candles) {
    const d = new Date(c.start * 1000).toDateString();
    if (d !== day) {
      day = d;
      pv = 0;
      vol = 0;
    }
    const v = c.volume || 0;
    pv += ((c.high + c.low + c.close) / 3) * v;
    vol += v;
    out.push(vol > 0 ? pv / vol : null);
  }
  return out;
}

/** Wilder's Relative Strength Index. */
export function rsi(values, period = 14) {
  const out = new Array(values.length).fill(null);
  let gain = 0;
  let loss = 0;
  for (let i = 1; i < values.length; i++) {
    const ch = values[i] - values[i - 1];
    const g = Math.max(ch, 0);
    const l = Math.max(-ch, 0);
    if (i <= period) {
      gain += g / period;
      loss += l / period;
    } else {
      gain = (gain * (period - 1) + g) / period;
      loss = (loss * (period - 1) + l) / period;
    }
    if (i >= period) out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return out;
}
