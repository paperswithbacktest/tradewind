5 symbols x 5040 bars on Apple M4 (macOS-27.0.1-arm64-arm-64bit-Mach-O), Python 3.13.12; backtrader 1.9.78.125 (numpy 2.2.4, pandas 2.3.1, best of 3), Tradewind 0.0.2 (numpy 2.2.4, pandas 2.3.1, best of 9)

| scenario | backtrader µs | Tradewind µs | speed-up | same final value |
|---|---|---|---|---|
| `idle` | 32.79 | 0.408 | ×80.4 | yes |
| `reader` | 33.44 | 0.568 | ×58.8 | yes |
| `rebalance` | 37.42 | 3.032 | ×12.3 | yes |
| `notified` | 36.98 | 3.226 | ×11.5 | yes |
| `indicators` | 38.82 | 3.099 | ×12.5 | yes |
