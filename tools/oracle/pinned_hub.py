"""Pin every dataset of the organisation to the revision it had at the reference run's epoch.

Imported by ``measure_one.py`` before pwb-toolbox. pwb-toolbox (0.1.4x-0.1.5x) asks the Hub
for a dataset's *current* revision (``HfApi.dataset_info``) and the cost tables read
``list_repo_files`` / ``hf_hub_download`` without a revision; offline it takes the *newest*
local snapshot. Both drift as soon as a dataset is republished after the reference run. Here
the three entry points get ``revision=<sha at the epoch>`` when the caller gave none, so a
run reads, online, exactly what the reference read -- and downloads it if the cache lacks it.

    TW_ORACLE_EPOCH=2026-11-12T08:00:00Z TW_ORACLE_HUB_COMMITS=hub_commits.json \\
        python -c "import pinned_hub; pinned_hub.install()"

``PINNED`` is the set of ``(repo, sha)`` actually resolved, printed at exit by the caller:
it is the run's data fingerprint.
"""

from __future__ import annotations

import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HUB_ORG, HubHistory, _aware

PINNED: dict = {}
UNPINNED: set = set()


def install(epoch: str | None = None, commits_path: str | None = None) -> dict:
    """Patch ``huggingface_hub`` in this process; returns the revision map used."""
    epoch = epoch or os.environ["TW_ORACLE_EPOCH"]
    commits_path = commits_path or os.environ["TW_ORACLE_HUB_COMMITS"]
    revisions = HubHistory.load(commits_path).revisions_at(_aware(epoch))

    import huggingface_hub
    from huggingface_hub import HfApi

    def pin(repo_id, repo_type, revision):
        if revision is not None or not isinstance(repo_id, str) or not repo_id.startswith(HUB_ORG + "/"):
            return revision
        if repo_type not in (None, "dataset"):
            return revision
        sha = revisions.get(repo_id)
        if sha is None:
            UNPINNED.add(repo_id)   # did not exist at the epoch: left to the Hub
            return None
        PINNED[repo_id] = sha
        return sha

    def wrap_method(name):
        orig = getattr(HfApi, name)

        @functools.wraps(orig)
        def method(self, repo_id, *args, revision=None, repo_type=None, **kw):
            kind = repo_type if repo_type is not None else ("dataset" if name == "dataset_info" else None)
            rev = pin(repo_id, kind, revision)
            if name != "dataset_info":
                kw["repo_type"] = repo_type
            return orig(self, repo_id, *args, revision=rev, **kw)

        setattr(HfApi, name, method)

    wrap_method("dataset_info")
    wrap_method("list_repo_files")

    # the module-level shortcut is bound to a default HfApi built at import: rebind it
    huggingface_hub.list_repo_files = lambda repo_id, **kw: HfApi(token=kw.pop("token", None)).list_repo_files(repo_id, **kw)

    orig_download = huggingface_hub.hf_hub_download

    @functools.wraps(orig_download)
    def hf_hub_download(repo_id, filename=None, *args, revision=None, repo_type=None, **kw):
        rev = pin(repo_id, repo_type, revision)
        return orig_download(repo_id, filename, *args, revision=rev, repo_type=repo_type, **kw)

    huggingface_hub.hf_hub_download = hf_hub_download
    import huggingface_hub.file_download as fd

    fd.hf_hub_download = hf_hub_download
    return revisions
