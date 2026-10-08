"""C18 (backlog B55, B56): what the joint own-entry accounting costs, measured against the old per-step work.

    python scripts/c18_measure.py --bench [--entries 150] [--scenarios 20000] [--candidates 350] [--repeats 3]
    python scripts/c18_measure.py --bench --mem-only old|new         # one fill in a fresh process: its extra traced peak (tracemalloc)

`--bench` replays ONE contest's selection fill step by step on synthetic scores of the real shape (a 5,000-entry
large GPP, 20,000 selection scenarios, about 350 screened candidates), twice: `old` is the arithmetic the fill used before C18
(the candidate's own payout and utility from `PayCurve.pay_util` on field-plus-own ranks, then `pay_total += pay_k`), `new` is
`objectives.OwnContest` (marginal over every candidate, then `add`). Both take the SAME fixed pick order and the SAME scenario
chunking (`objectives.memory_cap_mb` split across the five knobs, as `select` does), so only the accounting differs. It is a
timing harness: the scores are synthetic (shared slate effect plus noise, integers, so ties are as common as in real runs). It
reads nothing of Ben's and writes nothing. Timings are interleaved old, new, old, new and the minimum is reported.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import numpy as np  # noqa: E402

from nhl_dfs.build import objectives as ob  # noqa: E402
from nhl_dfs.contracts.statuses import PayoutSource  # noqa: E402


def peak_mb() -> float:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1e6
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3


def synth(S: int, K: int, F: int, L: int, n_entries: int, seed: int):
    """Scores, field ranks and a contest of the real shape. Scores are integers around 300 with a shared slate effect."""
    rng = np.random.default_rng(seed)
    slate = rng.normal(0, 1, (S, 1))
    cand = (300 + 25 * slate + rng.normal(0, 40, (S, K))).astype(np.int32)
    fs = (300 + 25 * slate + rng.normal(0, 40, (S, F))).astype(np.int32)
    w = np.full(F, (L - n_entries) // F, np.int64)
    w[: (L - n_entries) - int(w.sum())] += 1
    paid = L // 5
    k = np.arange(1, paid + 1)
    prizes = (2_000_000 * k ** -1.1).astype(np.int64) + 700
    ct = ob.Contest("B", "bench", "large_gpp", L, 500, prizes, np.zeros(paid, bool), None, PayoutSource.PRIOR)
    dt = np.int16 if L < 32000 else np.int32
    G, E = ob.ranks(cand, fs, w)
    return ct, cand, G.astype(dt), E.astype(dt)


def step_rows(S: int, K: int, cap_mb: float, knobs: int) -> int:
    return ob._chunk_rows(S, K * 160, cap_mb / knobs)  # the fill's own chunking (select: mem_share = 1 / knobs)


def fill(which: str, ct, cand, G, E, picks, cap_mb: float, knobs: int, kappa: float = 0.5):
    """The per-step work of the selection fill for one contest; returns per-step seconds and the final pay_total."""
    S, K = cand.shape
    cv = ob.PayCurve(ct, 0.01)
    step = step_rows(S, K, cap_mb, knobs)
    n = len(picks)
    total_fees = n * ct.fee_cents
    thr = 0.2 * total_fees
    pay_total = np.zeros(S, np.int64)
    used = np.zeros(K, bool)
    if which == "new":
        oc = ob.OwnContest(cv.pay_util, G, E, cand, capacity=n, chunk=step, live_below=ct.paid)
    else:
        gi, ei = np.zeros((S, K), G.dtype), np.zeros((S, K), G.dtype)
    times = []
    for k_next in picks:
        t0 = time.perf_counter()
        su, sq, n80 = np.zeros(K), np.zeros(K), np.zeros(K)
        for a in range(0, S, step):
            b = min(S, a + step)
            if which == "new":
                pay, util = oc.marginal(a, b)
            else:
                pay, util = cv.pay_util(G[a:b] + gi[a:b], E[a:b] + ei[a:b])
            u = util / 100.0
            su += u.sum(axis=0)
            sq += (u * u).sum(axis=0)
            n80 += ((pay_total[a:b, None] + pay) <= thr).sum(axis=0)
        mean_u = su / S
        score = mean_u - kappa * total_fees / 100.0 * (n80 / S)
        score[used] = -np.inf
        _ = int(np.argmax(score))  # the real fill picks here; the harness replays a fixed order so both do equal work
        k = int(k_next)
        used[k] = True
        if which == "new":
            pay_total += oc.add(k)[0]
        else:
            pay_k, _u = cv.pay_util(G[:, [k]] + gi[:, [k]], E[:, [k]] + ei[:, [k]])
            pay_total += pay_k[:, 0]
            for a in range(0, S, step):
                b = min(S, a + step)
                own = cand[a:b, [k]]
                gi[a:b] += own > cand[a:b]
                ei[a:b] += own == cand[a:b]
        times.append(time.perf_counter() - t0)
    return times, pay_total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--entries", type=int, default=150)
    ap.add_argument("--scenarios", type=int, default=20000)
    ap.add_argument("--candidates", type=int, default=350)
    ap.add_argument("--field", type=int, default=400, help="distinct field lineups (cost does not depend on it)")
    ap.add_argument("--contest-size", type=int, default=5000)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--knobs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=18)
    ap.add_argument("--mem-only", choices=["old", "new"])
    a = ap.parse_args()
    if not a.bench:
        ap.error("nothing to do: pass --bench")
    cap = float(ob.load_risk_config()["objectives"]["memory_cap_mb"])
    ct, cand, G, E = synth(a.scenarios, a.candidates, a.field, a.contest_size, a.entries, a.seed)
    picks = np.random.default_rng(a.seed + 1).permutation(a.candidates)[: a.entries]
    if a.mem_only:  # building the synthetic data peaks higher than the fill, so measure the fill's own extra peak
        import gc
        import tracemalloc

        gc.collect()
        tracemalloc.start()
        resident, _ = tracemalloc.get_traced_memory()
        t, _ = fill(a.mem_only, ct, cand, G, E, picks, cap, a.knobs)
        _, peak = tracemalloc.get_traced_memory()
        print(f"{a.mem_only}: fill {sum(t):.1f} s (traced), extra peak {(peak - resident) / 1e6:.0f} MB on top of "
              f"{resident / 1e6:.0f} MB resident (process peak working set {peak_mb():.0f} MB, set by the data build)")
        return 0
    print(f"shape: S={a.scenarios} K={a.candidates} entries={a.entries} contest={a.contest_size} chunk rows="
          f"{step_rows(a.scenarios, a.candidates, cap, a.knobs)} (memory_cap_mb {cap:g} / {a.knobs} knobs)")
    best = {"old": None, "new": None}
    for r in range(a.repeats):
        for which in ("old", "new"):
            t, tot = fill(which, ct, cand, G, E, picks, cap, a.knobs)
            t = np.asarray(t)
            if best[which] is None or t.sum() < best[which][0].sum():
                best[which] = (t, tot)
            print(f"repeat {r + 1} {which}: {t.sum():.1f} s")
    (to, po), (tn, pn) = best["old"], best["new"]
    print(f"minimum of {a.repeats}: old {to.sum():.2f} s, new {tn.sum():.2f} s, ratio {tn.sum() / to.sum():.2f}x")
    for lo, hi in ((0, 3), (3, 20), (20, 75), (75, a.entries)):
        if lo < len(to):
            hi = min(hi, len(to))
            print(f"  steps {lo + 1:>3} to {hi:>3} (m = {lo} to {hi - 1}): old {to[lo:hi].sum():.2f} s, new {tn[lo:hi].sum():.2f} s, "
                  f"ratio {tn[lo:hi].sum() / to[lo:hi].sum():.2f}x")
    print(f"first three steps (m <= 2) ratio: {tn[:3].sum() / to[:3].sum():.2f}x")
    over = int(np.max(pn - po))
    print(f"final running payout, new minus old, largest over scenarios: {over / 100:.2f} dollars (old counted prizes twice; "
          f"new never exceeds the prize pool {int(ct.prizes_cents.sum()) / 100:.0f}: max {int(pn.max()) / 100:.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
