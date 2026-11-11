"""Swap a model for one security only: a fixed per-share slippage on the illiquid name.

The compatible slippage model leaves fills at the open untouched unless ``slip_open`` is set,
and a market order fills at the next bar's open: hence ``slip_open=True`` here.
"""

from _data import bars

import tradewind as tw
from tradewind.models import CompatSlippage


class Hold(tw.Strategy):
    def next(self):
        # rebalance every 21 bars, so the slippage is paid many times
        if len(self) % 21 == 1:
            for data in self.datas:
                self.order_target_percent(data, target=0.45)


def run(slipped: bool) -> float:
    cerebro = tw.Cerebro()
    liquid = cerebro.adddata(tw.feeds.PandasData(dataname=bars("LIQ", seed=21)), name="LIQ")
    thin = cerebro.adddata(tw.feeds.PandasData(dataname=bars("THIN", seed=22)), name="THIN")
    if slipped:
        # 5 cents a share against the trader on THIN; LIQ keeps the broker's default models
        cerebro.broker.set_models(thin, slippage=CompatSlippage(fixed=0.05, slip_open=True))
    cerebro.addstrategy(Hold)
    cerebro.broker.setcash(100_000)
    cerebro.run()
    assert liquid.security.models is not thin.security.models or not slipped
    return cerebro.broker.getvalue()


print(f"without slippage {run(False):,.2f}, with slippage on THIN {run(True):,.2f}")
