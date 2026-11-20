"""The corpus oracle harness (tools/oracle/): comparison, data epochs, pinning, files.

No network, no strategy run: the replay itself is the oracle-corpus workflow.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "oracle"))

from common import HubHistory, compare, read_lines, started_at, to_utc


def read(path):
    with open(path) as fh:
        return fh.read()


def nav(*values, start=1):
    return [{"date": f"2020-01-{start + i:02d}", "value": v} for i, v in enumerate(values)]


def test_compare_identical_and_within_tolerance():
    assert compare(nav(100.0, 101.0), nav(100.0, 101.0)) == {"same": True, "n": 2, "max_rel": 0.0}
    c = compare(nav(1e6, 1e6), nav(1e6, 1e6 + 1e-4), tol=1e-9)
    assert c["same"] and c["max_rel"] == pytest.approx(1e-10)


def test_compare_reports_the_first_bar_beyond_tolerance():
    c = compare(nav(100.0, 101.0, 102.0, 103.0), nav(100.0, 101.0, 102.5, 103.0), tol=1e-9)
    assert not c["same"]
    assert (c["first_index"], c["first_date"], c["ref_value"], c["run_value"]) == (2, "2020-01-03", 102.0, 102.5)


def test_compare_values_below_one_are_absolute():
    # relative to max(1, |ref|): a NAV near zero does not blow the error up
    assert compare(nav(1e-12), nav(2e-12), tol=1e-9)["same"]


def test_compare_dates_and_nans():
    c = compare(nav(1.0, 2.0), nav(1.0, 2.0, start=2))
    assert c["dates_differ"] and c["first_index"] == 0
    assert compare(nav(1.0, 2.0), nav(1.0, 2.0, 3.0))["dates_differ"]
    assert compare(nav(float("nan")), nav(float("nan")))["same"]
    assert not compare(nav(1.0), nav(float("nan")))["same"]
    assert compare(None, nav(1.0)) == {"same": False, "missing": "reference"}


def test_measured_at_is_local_time_of_the_host():
    # the Mac writes Paris time (UTC+1 in November), the data host UTC
    assert to_utc("2026-11-12T10:00:00", "mac") == datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
    assert to_utc("2026-11-12T10:00:00", "ns3208333") == datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
    end = to_utc("2026-11-12T10:00:00", "ns3208333")
    assert started_at(end, 90.0) == datetime(2026, 11, 12, 9, 58, 30, tzinfo=timezone.utc)
    assert started_at(end, float("nan")) == end


def hub():
    return HubHistory({"org/A": [("2026-11-10T00:00:00Z", "a1"), ("2026-11-12T00:00:00Z", "a2")],
                       "org/B": [("2026-11-11T12:00:00Z", "b1")]})


def test_revision_at_takes_the_last_commit_before_the_epoch():
    h = hub()
    t = datetime(2026, 11, 11, tzinfo=timezone.utc)
    assert h.revision_at("org/A", t) == "a1"
    assert h.revision_at("org/B", t) is None                    # did not exist yet
    assert h.revision_at("org/A", datetime(2026, 11, 12, tzinfo=timezone.utc)) == "a2"   # inclusive
    assert h.revisions_at(datetime(2026, 11, 13, tzinfo=timezone.utc)) == {"org/A": "a2", "org/B": "b1"}


def test_moved_between_lists_datasets_committed_during_a_run():
    h = hub()
    t0, t1 = datetime(2026, 11, 11, tzinfo=timezone.utc), datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert h.moved_between(t0, t1) == ["org/A", "org/B"]
    assert h.moved_between(t1, datetime(2026, 11, 13, tzinfo=timezone.utc)) == []


def test_pinned_hub_injects_the_epoch_revision(tmp_path, monkeypatch):
    hf = pytest.importorskip("huggingface_hub")
    import pinned_hub

    seen = []
    monkeypatch.setattr(hf.HfApi, "dataset_info", lambda self, repo_id, *, revision=None, **kw: seen.append(
        ("info", repo_id, revision)))
    monkeypatch.setattr(hf.HfApi, "list_repo_files", lambda self, repo_id, *, revision=None, repo_type=None, **kw:
                        seen.append(("files", repo_id, revision)))
    monkeypatch.setattr(hf, "hf_hub_download", lambda repo_id, filename=None, *, revision=None, repo_type=None, **kw:
                        seen.append(("get", repo_id, revision)))
    monkeypatch.setattr(hf, "list_repo_files", hf.list_repo_files)
    import huggingface_hub.file_download as fd

    monkeypatch.setattr(fd, "hf_hub_download", fd.hf_hub_download)
    path = tmp_path / "commits.json"
    path.write_text(json.dumps({"commits": {"paperswithbacktest/A": [["2026-11-10T00:00:00Z", "a1"],
                                                                     ["2026-11-12T00:00:00Z", "a2"]]}}))
    monkeypatch.setattr(pinned_hub, "PINNED", {})
    pinned_hub.install("2026-11-11T00:00:00Z", str(path))
    hf.HfApi().dataset_info("paperswithbacktest/A")
    hf.list_repo_files("paperswithbacktest/A", repo_type="dataset")
    hf.hf_hub_download("paperswithbacktest/A", "data/x.parquet", repo_type="dataset")
    hf.HfApi().dataset_info("paperswithbacktest/A", revision="explicit")     # an explicit revision wins
    hf.HfApi().dataset_info("someone/else")                                 # other owners untouched
    hf.HfApi().dataset_info("paperswithbacktest/New")                       # absent at the epoch
    assert seen == [("info", "paperswithbacktest/A", "a1"), ("files", "paperswithbacktest/A", "a1"),
                    ("get", "paperswithbacktest/A", "a1"), ("info", "paperswithbacktest/A", "explicit"),
                    ("info", "someone/else", None), ("info", "paperswithbacktest/New", None)]
    assert pinned_hub.PINNED == {"paperswithbacktest/A": "a1"}
    assert "paperswithbacktest/New" in pinned_hub.UNPINNED


def test_lock_and_ci_subset_are_well_formed():
    with open(os.path.join(ROOT, "oracle", "v1.lock.json")) as fh:
        lock = json.load(fh)
    assert lock["version"] == "v1" and lock["repo"] == "paperswithbacktest/Tradewind-Oracle"
    assert len(lock["revision"]) == 40 and set(lock["files"]) >= {"nav.parquet", "manifest.parquet",
                                                                   "hub_commits.json"}
    assert all(len(f["sha256"]) == 64 for f in lock["files"].values())
    assert sum(lock["papers_trees"].values()) == lock["n_reference"]
    ci = read_lines(os.path.join(ROOT, "tools", "oracle", "subsets", "ci.txt"))
    assert 20 <= len(ci) <= 60 and len(set(ci)) == len(ci)


def test_reference_navs_read_one_strategy_per_row_group(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    import datetime as dt

    import run

    schema = pa.schema([("slug", pa.string()), ("date", pa.date32()), ("value", pa.float64())])
    with pq.ParquetWriter(tmp_path / "nav.parquet", schema) as w:
        for slug, vals in (("a", [1.0, 2.0]), ("b", [3.0])):
            w.write_table(pa.table({"slug": [slug] * len(vals), "date": [dt.date(2020, 1, i + 1) for i in range(len(vals))],
                                    "value": vals}, schema=schema))
    refs = run.ReferenceNavs(str(tmp_path))
    assert refs.get("a") == [{"date": "2020-01-01", "value": 1.0}, {"date": "2020-01-02", "value": 2.0}]
    assert refs.get("b") == [{"date": "2020-01-01", "value": 3.0}] and refs.get("c") is None


def test_materialize_takes_the_harness_and_the_strategies_from_git(tmp_path, monkeypatch):
    import run

    repo = tmp_path / "papers"
    (repo / "published" / "s1").mkdir(parents=True)
    (repo / "published" / "s2").mkdir(parents=True)
    (repo / "backtester.py").write_text("HARNESS = 1\n")
    (repo / "published" / "s1" / "strategy.py").write_text("S = 1\n")
    (repo / "published" / "s1" / "paper.pdf").write_bytes(b"%PDF")
    (repo / "published" / "s2" / "strategy.py").write_text("S = 2\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    tree = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (repo / "published" / "s1" / "strategy.py").write_text("S = 'changed after the commit'\n")
    monkeypatch.setattr(run, "CACHE", str(tmp_path / "cache"))
    dest = run.materialize(str(repo), tree, ["s1"])
    assert read(os.path.join(dest, "backtester.py")) == "HARNESS = 1\n"
    assert read(os.path.join(dest, "published", "s1", "strategy.py")) == "S = 1\n"
    assert not os.path.exists(os.path.join(dest, "published", "s1", "paper.pdf"))
    assert not os.path.exists(os.path.join(dest, "published", "s2"))
    run.materialize(str(repo), tree, ["s1", "s2"])
    assert read(os.path.join(dest, "published", "s2", "strategy.py")) == "S = 2\n"
