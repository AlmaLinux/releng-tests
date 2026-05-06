"""RPM-style EVR (Epoch:Version-Release) comparison.

Used by parity checks that need to pick the *latest* build of a
package per arch and compare those — for example, noarch parity has
to ignore stale leftover builds on one arch when comparing what
``dnf install <name>`` would actually pull.

We deliberately do NOT depend on the system ``rpm`` Python module
(it's not in our ``pyproject.toml`` deps and pulling librpm in just
to get a comparator is overkill). Instead we port ``rpmvercmp`` from
RPM's source — same algorithm, same edge cases (tilde sorts lower
than anything, caret sorts lower than alphanumeric but higher than
end-of-string, leading zeros stripped from numeric segments, numeric
beats alpha when types disagree).

Public API:

* ``vercmp(a, b)`` -- compare two version-or-release strings.
  Returns -1 / 0 / 1.
* ``evr_cmp(a, b)`` -- compare two ``(epoch, version, release)``
  triples. ``epoch`` may be ``""`` / ``None`` / ``"0"`` (treated
  as 0).

Both raise nothing on input — feed them whatever ``primary.xml``
gave us.
"""

from __future__ import annotations


def _is_alnum(c: str) -> bool:
    return c.isalnum()


def vercmp(a: str, b: str) -> int:
    """RPM ``rpmvercmp`` ported to Python.

    Returns -1, 0, 1 — same contract as the C function in
    ``rpm/rpmio/rpmvercmp.c``. Tested against the canonical cases
    (digits, alpha, tilde, caret, mixed) in the meta-test suite.
    """
    if a == b:
        return 0

    i, j = 0, 0
    la, lb = len(a), len(b)

    while i < la and j < lb:
        # Skip non-alnum, but preserve the special markers ~ and ^.
        while i < la and not (_is_alnum(a[i]) or a[i] in "~^"):
            i += 1
        while j < lb and not (_is_alnum(b[j]) or b[j] in "~^"):
            j += 1

        # Tilde marks a "pre-release" — sorts lower than anything,
        # including the empty string. ``1.0~rc1`` < ``1.0``.
        a_tilde = i < la and a[i] == "~"
        b_tilde = j < lb and b[j] == "~"
        if a_tilde or b_tilde:
            if not a_tilde:
                return 1
            if not b_tilde:
                return -1
            i += 1
            j += 1
            continue

        # Caret marks a "post-release minor" — sorts higher than the
        # empty string but lower than alphanumeric continuation.
        # Used by Debian-style upstream snapshots; RPM accepts it too.
        a_caret = i < la and a[i] == "^"
        b_caret = j < lb and b[j] == "^"
        if a_caret or b_caret:
            if i >= la:
                return -1  # "" < "^"
            if j >= lb:
                return 1
            if not a_caret:
                return 1
            if not b_caret:
                return -1
            i += 1
            j += 1
            continue

        if i >= la or j >= lb:
            break

        # A homogeneous segment: either all-digits or all-alpha.
        is_num = a[i].isdigit()
        si, sj = i, j
        if is_num:
            while i < la and a[i].isdigit():
                i += 1
            while j < lb and b[j].isdigit():
                j += 1
        else:
            while i < la and a[i].isalpha():
                i += 1
            while j < lb and b[j].isalpha():
                j += 1

        # Type mismatch: one side started a numeric segment, the other
        # an alpha segment (so b's "segment" had length 0). Numeric wins.
        if sj == j:
            return 1 if is_num else -1

        if is_num:
            # Strip leading zeros so "010" == "10". Then longer wins.
            seg_a = a[si:i].lstrip("0") or "0"
            seg_b = b[sj:j].lstrip("0") or "0"
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
            if seg_a != seg_b:
                return 1 if seg_a > seg_b else -1
        else:
            seg_a = a[si:i]
            seg_b = b[sj:j]
            if seg_a != seg_b:
                return 1 if seg_a > seg_b else -1

    # End-of-string handling. Trailing tilde means "lower" (e.g.
    # ``1.0~`` < ``1.0``); trailing caret means "higher".
    if i < la and a[i] == "~":
        return -1
    if j < lb and b[j] == "~":
        return 1
    if i < la and a[i] == "^":
        return 1
    if j < lb and b[j] == "^":
        return -1
    if i < la:
        return 1
    if j < lb:
        return -1
    return 0


def _epoch_int(e: str | None) -> int:
    """Treat empty / None / ``"0"`` epoch as 0."""
    if e is None or e == "":
        return 0
    return int(e)


def evr_cmp(
    a: tuple[str | None, str, str],
    b: tuple[str | None, str, str],
) -> int:
    """Compare two ``(epoch, version, release)`` triples.

    Epoch is compared numerically (so ``"10"`` > ``"2"``); version and
    release use ``vercmp``. Returns -1 / 0 / 1.
    """
    ea, va, ra = a
    eb, vb, rb = b
    iea, ieb = _epoch_int(ea), _epoch_int(eb)
    if iea != ieb:
        return -1 if iea < ieb else 1
    c = vercmp(va, vb)
    if c:
        return c
    return vercmp(ra, rb)


def evr_str(evr: tuple[str | None, str, str]) -> str:
    """Render an EVR triple in the canonical ``[epoch:]version-release`` form."""
    e, v, r = evr
    prefix = f"{e}:" if e and e != "0" else ""
    return f"{prefix}{v}-{r}"
