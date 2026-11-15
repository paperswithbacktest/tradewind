"""tools/codemod_tradewind.py: the rewrite, its idempotence, and what it reports."""

import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

import codemod_tradewind as cm

SOURCE = textwrap.dedent(
    '''\
    """Uses backtrader; the docstring says so and stays as it is."""
    import backtrader as bt  # engine
    import backtrader.indicators as btind
    from backtrader.utils.date import num2date as _num2date
    from backtrader.feeds import PandasData
    import pwb_toolbox.backtesting as pwb_bt
    import pwb_toolbox.datasets
    from pwb_toolbox.datasets.pit_universe import (
        load_pit_universe,
    )

    def run():
        try:
            import backtrader as _bt  # type: ignore
        except ImportError:
            _bt = None
        return pwb_toolbox.datasets.get_pricing(["SPY"]), "pwb_toolbox is a word here"
    '''
)


def test_tradewind_form_and_idempotence():
    after, hits, unsupported = cm.transform(SOURCE, cm.ENGINE_PATTERNS, "pwb")
    assert "import tradewind as bt  # engine" in after
    assert "import tradewind.indicators as btind" in after
    assert "from tradewind import num2date as _num2date" in after
    assert "from tradewind.feeds import PandasData" in after
    assert "    import tradewind as _bt  # type: ignore" in after
    assert "import pwb.backtesting as pwb_bt" in after
    assert "from pwb.datasets.pit_universe import (" in after
    # the package named by the code follows its unaliased import
    assert "return pwb.datasets.get_pricing" in after
    # prose is not code: the docstring and the message keep their words
    assert '"""Uses backtrader; the docstring' in after
    assert '"pwb_toolbox is a word here"' in after
    assert unsupported == []
    assert hits["toolbox-code-name"] == 1
    again, hits2, _ = cm.transform(after, cm.ENGINE_PATTERNS, "pwb")
    assert again == after and not hits2


def test_toolbox_form_names_no_engine():
    rules = cm.toolbox_engine_patterns("pwb")
    after, _, unsupported = cm.transform(SOURCE, rules, "pwb")
    assert "from pwb.backtesting import bt  # engine" in after
    assert "from pwb.backtesting.bt import indicators as btind" in after
    assert "from pwb.backtesting.bt import num2date as _num2date" in after
    assert "from pwb.backtesting.bt.feeds import PandasData" in after
    assert "    from pwb.backtesting import bt as _bt  # type: ignore" in after
    assert "import backtrader" not in after and "tradewind" not in after
    assert unsupported == []
    assert cm.transform(after, rules, "pwb")[0] == after


def test_what_no_rule_handles_is_reported_not_rewritten():
    src = textwrap.dedent(
        """\
        import importlib
        import backtrader.observers as obs
        import pwb_toolbox.backtesting as pwb
        engine = importlib.import_module("backtrader")
        TARGET = "pwb_toolbox.datasets.get_pricing"
        """
    )
    after, _, unsupported = cm.transform(src, cm.ENGINE_PATTERNS, "pwb")
    assert "import backtrader.observers as obs" in after
    kinds = " | ".join(unsupported)
    assert "import line: import backtrader.observers as obs" in kinds
    assert "dynamic import: importlib.import_module(\"backtrader" in kinds
    assert "module name in a string: \"pwb_toolbox.datasets.get_pricing\"" in kinds
    # ``import pwb.backtesting as pwb`` binds the name itself: no clash
    assert "name clash" not in kinds


def test_a_rewrite_that_would_shadow_a_name_is_reported():
    src = "import pwb_toolbox.datasets\npwb = 3\nprint(pwb_toolbox.datasets, pwb)\n"
    _, _, unsupported = cm.transform(src, [], "pwb")
    assert any(u.startswith("name clash") for u in unsupported)


def test_surface_flags_a_missing_name_and_sees_its_guard(tmp_path):
    surface = cm.Surface()
    src = textwrap.dedent(
        """\
        import backtrader as bt

        class S(bt.Strategy):
            def __init__(self):
                self.x = bt.indicators.NoSuchIndicator(self.data.close)
                self.sma = bt.indicators.SMA(self.data.close, period=3)

            def next(self):
                try:
                    self.no_such_method()
                except AttributeError:
                    pass
                self.order_target_percent(self.data, target=0.5)
        """
    )
    missing = dict(surface.missing(src))
    assert missing == {
        "bt:indicators.NoSuchIndicator": False,
        "bt_call:indicators.NoSuchIndicator": False,
        "self.call:no_such_method": True,
    }


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True,
                                   check=False).returncode,
                    reason="git not available")
def test_git_mode_reads_blobs_without_a_checkout(tmp_path):
    repo = tmp_path / "papers"
    (repo / "published" / "a").mkdir(parents=True)
    (repo / "held" / "b").mkdir(parents=True)
    (repo / "published" / "a" / "strategy.py").write_text("import backtrader as bt\n")
    (repo / "held" / "b" / "strategy.py").write_text("import pwb_toolbox.datasets as pwb_ds\n")
    (repo / "published" / "a" / "README.md").write_text("x\n")

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "corpus")
    files = dict(cm.read_git(str(repo), "HEAD", ["published", "held"]))
    assert files == {
        "published/a/strategy.py": "import backtrader as bt\n",
        "held/b/strategy.py": "import pwb_toolbox.datasets as pwb_ds\n",
    }
    report = tmp_path / "report.json"
    out = subprocess.run(
        [sys.executable, cm.__file__, "--git", str(repo), "--ref", "HEAD", "--report", str(report)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "2 would change" in out
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run([sys.executable, cm.__file__, "--git", str(repo), "--write"],
                       capture_output=True, check=True)
