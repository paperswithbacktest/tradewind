# Reproducibility: the run fingerprint

The engine is deterministic: the same data, the same strategy and the same parameters give
the same values, to the last bit. After every `run()`, `engine.fingerprint` records what
went in, hashed, so a result can be tied to its inputs and two results compared.

## Deterministic by construction

- One synchronous, single-threaded event loop; the sixteen phases of a slice run in a fixed
  order ([How a backtest runs](concepts.md#the-sixteen-phases-of-a-slice)).
- No wall-clock time, no thread, no network inside a backtest.
- Two things in a strategy break determinism: drawing from `np.random` or `random` without a
  seed, and iterating over a `set` of strings (its order changes from one Python process to
  the next unless `PYTHONHASHSEED` is fixed). Seed the first, sort the second.

```python
engine.set_seed(42)          # or engine.run(seed=42): seeds Python's and numpy's global
                             # generators before the strategies are built
```

## What the fingerprint records

```python
engine.run()
fp = engine.fingerprint      # a RunFingerprint, built on first read (None before run())
fp.digest                    # SHA-256 of everything below, in canonical JSON
fp.short                     # its first 12 characters, for a log line
```

| section | contents |
|---|---|
| `engine` | Tradewind's version and a hash of its own source |
| `environment` | Python, numpy, pandas, platform, `PYTHONHASHSEED` |
| `data` | per feed: name, rows, first and last timestamps, a SHA-256 of its content, its parameters and declared provenance |
| `provenance` | data the strategy reads by itself, declared with `engine.add_provenance(...)` |
| `strategies` | per strategy: the class, a hash of the source of every module it comes from, its parameters |
| `broker` | cash, commission schemes, the reality models, the broker's flags |
| `clock` | the clock and the exchange hours, when set |
| `rng` | the engine's seed, and the state of Python's and numpy's global generators at the start when the run drew from them |

Universes, corporate-action providers and the price normalisation of each feed are recorded
too, with a digest of their tables ([Universes and corporate actions](universe-and-corporate-actions.md)).

## Declaring where the data came from

A content hash proves two runs read the same numbers; a provenance says where the numbers
came from, so a result can be rebuilt later:

```python
engine.adddata(feed, name="SPY", provenance={"dataset": "ETFs-Daily-Price", "revision": "a1b2c3d"})
engine.add_provenance("fundamentals", dataset="Stocks-Quarterly-Earnings", revision="r1")
```

The second form is for data the strategy loads by itself (a file, a dataset at a revision):
the engine cannot see it otherwise.

## Storing and comparing

```python
open("run.fingerprint.json", "w").write(fp.to_json())

old = tw.RunFingerprint.from_json(open("run.fingerprint.json").read())   # checks the digest
old.verify()                 # True: the content was not edited by hand
fp == old                    # same digest
fp.diff(old)                 # e.g. ['strategies[0].params.n']: the dotted paths that differ
```

## What it guarantees, and what it does not

- **Same digest, same NAV.** Two runs with the same digest read the same inputs, so they
  produce the same values (`tests/test_fingerprint.py`).
- **The converse does not hold.** A new numpy or a new platform changes the digest without
  moving a deterministic NAV; `diff` tells you that only `environment` changed.
- **Blind spots.** Data a strategy loads itself is invisible unless declared with
  `add_provenance`; a generator created without a seed (`np.random.default_rng()`) is not
  reproducible, and nothing outside the engine's inputs is seen.

Live sessions go one step further: their journal holds every bar and every broker message
in the order they were applied, and replaying it reproduces the session
([Live trading](live-trading.md#journal-replay-and-resume)).

Reference: [Fingerprint](reference/fingerprint.md).
