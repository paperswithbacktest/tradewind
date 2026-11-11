"""``Engine`` is the engine's name; ``Cerebro`` stays as backtrader's name for it."""

import tradewind as tw
from tradewind.cerebro import Cerebro as CerebroFromOldPath


def test_cerebro_is_the_engine_under_backtraders_name():
    assert tw.Cerebro is tw.Engine
    assert CerebroFromOldPath is tw.Engine
    assert "Engine" in tw.__all__ and "Cerebro" in tw.__all__


def test_a_strategy_reaches_its_engine_under_all_three_names():
    seen = {}

    class Probe(tw.Strategy):
        def start(self):
            seen["ids"] = {id(self.engine), id(self.cerebro), id(self.env)}

    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=idx)
    engine = tw.Engine()
    engine.adddata(tw.feeds.PandasData(dataname=df))
    engine.addstrategy(Probe)
    engine.run()
    assert seen["ids"] == {id(engine)}
