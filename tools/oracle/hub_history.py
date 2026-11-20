"""Record the commit history of every dataset of the organisation (sha and time only).

    HF_TOKEN=... python tools/oracle/hub_history.py --out /tmp/oracle_v1/hub_commits.json

One ``list_repo_commits`` call per dataset, nothing downloaded. Commit titles are not kept:
the reference set's provenance needs the revision live at a given time, nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HUB_ORG, iso


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--org", default=HUB_ORG)
    args = p.parse_args()
    from huggingface_hub import HfApi

    token = os.environ.get("HF_TOKEN") or os.environ.get("HF_ACCESS_TOKEN")
    api = HfApi(token=token)
    repos = sorted(d.id for d in api.list_datasets(author=args.org))
    commits, failed = {}, {}
    for repo in repos:
        try:
            commits[repo] = [(iso(c.created_at), c.commit_id) for c in api.list_repo_commits(repo, repo_type="dataset")]
        except Exception as exc:  # noqa: BLE001 - one unreadable repository is reported, not fatal
            failed[repo] = type(exc).__name__
        time.sleep(0.05)
    out = {"org": args.org, "fetched_at": iso(__import__("datetime").datetime.now(__import__("datetime").timezone.utc)),
           "n_repos": len(commits), "failed": failed, "commits": commits}
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=0)
    n = sum(len(v) for v in commits.values())
    print(f"{len(commits)} datasets, {n} commits -> {args.out}" + (f"; unreadable: {failed}" if failed else ""))


if __name__ == "__main__":
    main()
