"""Volume-only check for the ORB amendment (no outcomes: fullday.pkl is never opened).
Prints: days failing coverage (e), the last failing day, and the scorable in-play sessions from the
proposed start, with the date that splits them in half."""
import os, pickle, sys, statistics, json
sys.path.insert(0, os.path.join(os.environ["EEH_ROOT"], "tools", "studies"))
import orb_in_play as O

daily = pickle.load(open(os.path.join(O.OUT_DIR, "daily.pkl"), "rb"))
opens = pickle.load(open(os.path.join(O.OUT_DIR, "open.pkl"), "rb"))
univ = O.universe(daily)
scored = sorted(d for d in univ if O.half_of(d))
fail = [d for d in scored
        if O.universe.cutoff_dv.get(d) is None or O.universe.cutoff_dv[d] < O.MIN_CUT_DV
        or O.universe.size.get(d, 0) < O.TOP_UNIV]
short = [d for d in fail if O.universe.size.get(d, 0) < O.TOP_UNIV]
cut = [O.universe.cutoff_dv[d] for d in fail if O.universe.cutoff_dv.get(d)]
START = "2018-01-11"
ip = O.in_play(daily, opens)
cand = [d for d in scored if d >= START and ip.get(d)]
mid = len(cand) // 2
print(json.dumps({
    "sessions_in_halves": len(scored),
    "e_fail_days": len(fail), "e_fail_first": fail[:1], "e_fail_last": fail[-1:],
    "e_fail_short_universe": len(short),
    "e_fail_cutoff_min_median_max_musd": [round(min(cut) / 1e6, 2), round(statistics.median(cut) / 1e6, 2),
                                          round(max(cut) / 1e6, 2)] if cut else None,
    "e_fail_after_start": [d for d in fail if d >= START],
    "scorable_inplay_sessions_from_start": len(cand),
    "first_last": [cand[0], cand[-1]] if cand else None,
    "split": {"last_of_first_half": cand[mid - 1], "first_of_second_half": cand[mid],
              "n_first": mid, "n_second": len(cand) - mid} if cand else None,
    "old_split_counts": {"before_2021": sum(d < "2021-01-04" for d in cand), "from_2021": sum(d >= "2021-01-04" for d in cand)},
    "segment_warmup_no_inplay_from_start": sum(1 for d in scored if d >= START and not ip.get(d)),
}, indent=1))
