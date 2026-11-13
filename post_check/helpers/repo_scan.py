"""Parallel collection of every ``(arch, repo)`` cell of one source.

Walking a whole release means fetching one ``primary.xml`` per
``(arch, repo)`` pair — roughly 36-60 of them on the full matrix, each
carrying thousands of packages. Sequentially that blows past any
sensible wall-clock budget, so the fetch is fanned out; the original
per-arch scripts under ``releng-almalinux/tools/`` solved it the same
way.

Lives here rather than inside a test module because two release tests
now need the identical traversal — ``test_srpm_version_consistency``
(one source, looking for split-brain SRPMs) and
``test_cross_source_parity`` (two sources, looking for a NEVRA whose
bits disagree). Duplicating it would let the two drift on exactly the
question that matters to both: which cells count as part of "the
release".
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from post_check.helpers import repodata
from post_check.helpers.url_builder import RepoURL, pulp_internal_beta_repo_base

# 8 is a comfortable upper bound for the AlmaLinux mirror network —
# higher and the CDN starts handing back connection resets, which look
# like release failures and are not.
FETCH_WORKERS = 8


def repo_bases(
    *,
    source: str,
    version: str,
    arches: Iterable[str],
    repos: Iterable[str],
) -> list[tuple[str, str]]:
    """``[(arch, repo_base), …]`` for every cell of ``source``.

    Pure: config lookups only, no I/O.

    ``pulp`` is special-cased because its layered internal-beta is a
    **flat** repository — there is no ``BaseOS``/``AppStream`` split to
    iterate, so the per-component ``repos`` list collapses to a single
    URL per arch and is ignored.
    """
    tasks: list[tuple[str, str]] = []
    for arch in arches:
        if source == "pulp":
            tasks.append((arch, pulp_internal_beta_repo_base(version=version, arch=arch)))
            continue
        for repo in repos:
            tasks.append(
                (
                    arch,
                    RepoURL.from_config(
                        source=source, version=version, arch=arch, repo=repo
                    ).repo_base(),
                )
            )
    return tasks


def fetch_one_cell(
    session: requests.Session,
    repo_base: str,
) -> list[repodata.Package]:
    """Fetch + parse one cell's ``primary.xml``.

    Returns ``[]`` on 403/404. A missing repo is not a release defect:
    RT/NFV do not exist on every arch, AL10 dropped ResilientStorage,
    and an arch may not have been pushed to the internal-beta yet.
    Other HTTP errors propagate — those are infrastructure problems
    rather than "this repo was never published", and the operator wants
    the raw exception (same contract as
    ``tests.release.test_release_parity._find_pkg``).
    """
    try:
        primary = repodata.find_primary_xml_url(session, repo_base)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            return []
        raise
    return list(repodata.iter_packages(session, primary))


def collect_packages_by_arch(
    session: requests.Session,
    *,
    source: str,
    version: str,
    arches: Iterable[str],
    repos: Iterable[str],
) -> dict[str, list[repodata.Package]]:
    """Fetch every cell of ``source`` in parallel, aggregated per arch.

    Parallelism is per ``(arch, repo)`` — the actual unit of network
    I/O — so one arch's biggest repo doesn't gate the rest and total
    wall-clock is bounded by the slowest single cell rather than their
    sum.

    The :class:`requests.Session` from ``http.session()`` (a
    ``requests_cache.CachedSession``) is safe to share across threads:
    each ``get()`` takes its own urllib3 connection from the pool.
    """
    arches = list(arches)
    out: dict[str, list[repodata.Package]] = {a: [] for a in arches}
    tasks = repo_bases(source=source, version=version, arches=arches, repos=repos)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        # Keyed by arch so a fetch failure surfaces against the right one.
        futures = {
            pool.submit(fetch_one_cell, session, base): arch for arch, base in tasks
        }
        for fut in as_completed(futures):
            out[futures[fut]].extend(fut.result())
    return out
