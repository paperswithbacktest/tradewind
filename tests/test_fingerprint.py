"""The reproducibility fingerprint: same digest, same NAV."""

import json

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.fingerprint import RunFingerprint


def frames(seed=5, n_bars=120, n_sym=3):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n_bars)
    out = {}
    for i in range(n_sym):
        close = 50 * np.exp(np.cumsum(rng.normal(0, 0.02, n_bars)))
        openp = close * (1 + rng.normal(0, 0.005, n_bars))
        out[f"S{i}"] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.01,
                                     "low": np.minimum(openp, close) * 0.99, "close": close, "volume": 1e5},
                                    index=index)
    return out


class Rotation(tw.Strategy):
    """Holds the feed with the best ``lookback`` return; ``noise`` draws from numpy's global
    generator (the kind of strategy spec/02 calls not reproducible without a seed)."""

    params = (("lookback", 10), ("noise", False))

    def __init__(self):
        self.nav = []

    def next(self):
        self.nav.append(self.broker.getvalue())
        if len(self) <= self.p.lookback or len(self) % 5:
            return
        scores = [d.close[0] / d.close[-self.p.lookback] for d in self.datas]
        if self.p.noise:
            scores = [s + np.random.normal(0, 0.05) for s in scores]
        best = int(np.argmax(scores))
        for i, d in enumerate(self.datas):
            self.order_target_percent(d, target=0.95 if i == best else 0.0)


def run(fr=None, seed=None, provenance=None, commission=None, **params):
    fr = fr or frames()
    engine = tw.Engine()
    engine.set_seed(seed)
    for name, frame in fr.items():
        engine.adddata(tw.feeds.PandasData(dataname=frame), name=name,
                       provenance=provenance.get(name) if provenance else None)
    engine.addstrategy(Rotation, **params)
    engine.broker.set_cash(100_000)
    if commission is not None:
        engine.broker.setcommission(commission=commission)
    strat = engine.run()[0]
    return engine.fingerprint, np.array(strat.nav)


def test_no_fingerprint_before_run():
    assert tw.Engine().fingerprint is None


def test_same_inputs_same_digest_same_nav():
    fp1, nav1 = run()
    fp2, nav2 = run()
    assert fp1 == fp2 and fp1.digest == fp2.digest and fp1.diff(fp2) == []
    assert np.array_equal(nav1, nav2)   # bit for bit
    assert fp1.rng["numpy"] == {"global_state_used": False}   # a deterministic run: no state recorded


@pytest.mark.parametrize("change, path", [
    ({"lookback": 20}, "strategies[0].params.lookback"),
    ({"commission": 0.001}, "broker.comminfo.None.params.commission"),
    ({"provenance": {"S0": {"dataset": "Stocks-Daily-Price", "revision": "abc123"}}}, "data[0].provenance"),
])
def test_an_input_change_moves_the_digest(change, path):
    base, _ = run()
    other, _ = run(**change)
    assert base != other and path in other.diff(base)


def test_data_change_moves_the_digest():
    fr = frames()
    base, _nav_a = run(fr)
    fr["S1"] = fr["S1"].copy()
    fr["S1"].iloc[60, fr["S1"].columns.get_loc("close")] *= 1.0000001
    other, _nav_b = run(fr)
    assert other.diff(base) == ["data[1].content_sha256"]
    assert other.data[1]["rows"] == 120 and other.data[1]["first"].startswith("2022-01-03")


def test_seed_makes_a_random_strategy_reproducible():
    groups: dict = {}
    for seed in (1, 2, 1, 2, 1):
        fp, nav = run(seed=seed, noise=True)
        assert fp.rng["seed"] == seed and fp.rng["numpy"]["global_state_used"]
        groups.setdefault(fp.digest, []).append(nav)
    assert len(groups) == 2
    for navs in groups.values():           # one digest, one NAV
        assert all(np.array_equal(navs[0], n) for n in navs[1:])
    a, b = (navs[0] for navs in groups.values())
    assert not np.array_equal(a, b)


def test_unseeded_random_runs_do_not_share_a_digest():
    fp1, _ = run(noise=True)
    fp2, _ = run(noise=True)   # the global generator moved on: another input
    assert fp1 != fp2 and fp1.diff(fp2) == ["rng.numpy.state_at_start"]


def test_strategy_seeding_itself_is_in_its_code():
    class Seeded(Rotation):
        def __init__(self):
            super().__init__()
            np.random.seed(7)

    def go():
        engine = tw.Engine()
        for name, frame in frames().items():
            engine.adddata(tw.feeds.PandasData(dataname=frame), name=name)
        engine.addstrategy(Seeded, noise=True)
        return engine.fingerprint if engine.run() else None, np.array(engine.runstrats[0][0].nav)

    (fp1, nav1), (fp2, nav2) = go(), go()
    assert np.array_equal(nav1, nav2)
    assert fp1.strategies[0]["code"][0]["class"].endswith("Seeded")
    # The global state is recorded when the run moved it: the first run started from an
    # unrelated state, so its digest differs (equal digests are sufficient, not necessary);
    # the second ended where it started (seed 7 and the same draws), so its start state is
    # not an input and a third run, from that same state, shares its digest.
    assert fp1.rng["numpy"]["global_state_used"] and not fp2.rng["numpy"]["global_state_used"]
    fp3, nav3 = go()
    assert fp3 == fp2 and np.array_equal(nav3, nav2)


def test_serialisation_round_trip():
    fp, _ = run(provenance={"S0": {"revision": "deadbeef"}})
    text = fp.to_json()
    back = RunFingerprint.from_json(text)
    assert back == fp and back.verify() and back.to_dict() == json.loads(text)
    tampered = json.loads(text)
    tampered["strategies"][0]["params"]["lookback"] = 99
    with pytest.raises(ValueError):
        RunFingerprint.from_dict(tampered)
    assert fp.engine["name"] == "tradewind" and fp.engine["version"] == tw.__version__
    assert len(fp.engine["source_sha256"]) == 64 and fp.environment["numpy"] == np.__version__
    assert fp.clock == {"clock": "UnionClock", "hours": "AlwaysOpen"}
    assert fp.data[0]["provenance"] == {"revision": "deadbeef"}


def test_declared_provenance_and_strategy_code():
    engine = tw.Engine()
    for name, frame in frames().items():
        engine.adddata(tw.feeds.PandasData(dataname=frame), name=name)
    engine.add_provenance("fundamentals", dataset="Stocks-Quarterly-Earnings", revision="r1")
    engine.addstrategy(Rotation)
    engine.run()
    fp = engine.fingerprint
    assert fp.provenance == {"fundamentals": {"dataset": "Stocks-Quarterly-Earnings", "revision": "r1"}}
    code = fp.strategies[0]["code"]
    assert [c["class"] for c in code] == [f"{__name__}.Rotation"]
    assert code[0]["kind"] == "module-source" and len(code[0]["sha256"]) == 64
    assert engine.fingerprint is fp     # built once
