#!/usr/bin/env python3
"""
qr_lookup.py -- quick QR-code list for a tour+program, in the language that
specific departure actually runs in.

The problem: RWGPS doesn't support one route serving multiple languages --
each day/option of a tour exists as a SEPARATE RWGPS route per language
(see tours_resolved.csv's `language` column, produced by resolve_tours.py
from the "... ENG 2026" / "... DEU 2026" / unmarked-NL route-name suffixes).
When a tour departure is scheduled to run in Dutch or German, whoever preps
guest materials needs the *right* route_id for every day, not the English
default, and a QR code pointing at it.

IMPORTANT: `tour` alone is NOT a unique key. 20/44 tours in tours_resolved.csv
share one tour name across multiple `program` values -- almost always a
forward/reverse direction pair (e.g. "Aschaffenburg-Bamberg or v.v." covers
both "Program: Aschaffenburg-Bamberg" and "Program: Bamberg - Aschaffenburg"),
each with its own day numbering. Grouping by tour name alone conflates their
day sequences and produces false gaps -- everything here groups by
(tour, program) instead.

This is NOT guest-facing yet -- it's an internal lookup, in the same shape
as the rest of this toolset (a script + a data file), so the correct QR
set for a given tour+program+language can be pulled up fast instead of
hunting through tours_resolved.csv by hand.

    python qr_lookup.py --list-tours                                  # tour -> programs -> languages resolved
    python qr_lookup.py --tour "Netherlands: Hansa Highlights" --lang DE          # --program auto-picked if only one
    python qr_lookup.py --tour "Aschaffenburg-Bamberg or v.v." --program "Program: Bamberg - Aschaffenburg" --lang DE
    python qr_lookup.py --gap-report                                  # sweep ALL tour+program+languages, quietly

Each per-tour run also writes qr_manifest.json into --out -- day ->
route_id/url/png, so this becomes a real small database, not just a
one-off print.

--gap-report does NOT dump every gap to the console -- it sweeps every
tour+program+language combo already in tours_resolved.csv and writes the
full detail to a CSV report file (default: tour_language_gaps.csv),
printing only a one-line count. This is existing data debt in
tours_resolved.csv, not something today's run created -- no need to see
or fix it all right now, it's just logged somewhere so it isn't lost.

Needs: pip install qrcode[pil] --break-system-packages
"""
import argparse, csv, json, os, re
from collections import defaultdict

DEFAULT_CSV = "tours_resolved.csv"
RWGPS_URL = "https://ridewithgps.com/routes/{}"


def load_rows(csv_path):
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tour_programs(rows):
    """tour -> program -> set of languages present. (tour, program) is the
    real unique key -- see module docstring."""
    out = defaultdict(lambda: defaultdict(set))
    for r in rows:
        out[r["tour"]][r["program"]].add(r["language"] or "NL")
    return out


def cmd_list_tours(a):
    rows = load_rows(a.csv)
    programs = tour_programs(rows)
    n_programs = sum(len(p) for p in programs.values())
    print(f"{len(programs)} tours, {n_programs} tour+program combos in {a.csv}\n")
    for tour in sorted(programs):
        progs = programs[tour]
        if len(progs) == 1:
            (prog, langs), = progs.items()
            print(f"  {tour}  [{', '.join(sorted(langs))}]")
        else:
            print(f"  {tour}  ({len(progs)} programs -- --program required)")
            for prog in sorted(progs):
                print(f"      {prog}  [{', '.join(sorted(progs[prog]))}]")


def resolve_days(matches, want):
    """For one (tour, program)'s rows + a target language, return
    (all_days, resolved, gaps) -- shared by the single lookup and the
    all-combos gap sweep so the two never disagree about what's a gap.

    A day can legitimately have MORE THAN ONE applied route in the same
    language -- these are rider-choice distance/routing alternates (the
    original "Optie 1"/"Optie 2" columns), not an error. 44% of day+language
    slots in tours_resolved.csv have exactly this shape. So `resolved[day]`
    is a LIST of one or more matched rows, not a single row -- a real gap
    is only a day with ZERO applied routes in the requested language."""
    all_days = sorted({int(r["day"]) for r in matches if r["day"]})
    resolved, gaps = {}, []
    for day in all_days:
        day_rows = [r for r in matches if r["day"] and int(r["day"]) == day]
        lang_match = [r for r in day_rows
                      if (r["language"] or "NL") == want and r["applied"] == "True"]
        if not lang_match:
            have = sorted({(r["language"] or "NL") for r in day_rows})
            gaps.append((day, have))
            continue
        resolved[day] = lang_match
    return all_days, resolved, gaps


def resolve_tour_program(rows, tour, program):
    """Find the exact (tour, program) match, auto-picking the program if
    there's only one for that tour. Returns (matches, resolved_program) or
    (None, error_message) if it can't be resolved unambiguously."""
    programs = tour_programs(rows)
    if tour not in programs:
        close = [t for t in programs if tour.lower() in t.lower()]
        msg = f"no tour named exactly '{tour}' found."
        if close:
            msg += " did you mean:\n" + "\n".join(f"  {t}" for t in close)
        else:
            msg += " run --list-tours to see exact names."
        return None, msg

    progs = programs[tour]
    if program:
        if program not in progs:
            msg = f"'{tour}' has no program '{program}'. Programs available:\n" + \
                  "\n".join(f"  {p}" for p in sorted(progs))
            return None, msg
        chosen = program
    elif len(progs) == 1:
        (chosen, _), = progs.items()
    else:
        msg = f"'{tour}' has {len(progs)} programs -- pass --program to pick one:\n" + \
              "\n".join(f"  {p}" for p in sorted(progs))
        return None, msg

    matches = [r for r in rows if r["tour"] == tour and r["program"] == chosen]
    return (matches, chosen), None


def cmd_lookup(a):
    rows = load_rows(a.csv)
    result, err = resolve_tour_program(rows, a.tour, a.program)
    if err:
        print(err)
        return
    matches, program = result

    want = a.lang.upper()
    all_days, resolved, gaps = resolve_days(matches, want)

    os.makedirs(a.out, exist_ok=True)
    try:
        import qrcode
    except ImportError:
        print("qrcode not installed -- run: pip install qrcode[pil] --break-system-packages")
        return

    manifest = {"tour": a.tour, "program": program, "language": want, "days": {}}
    print(f"{a.tour}  |  {program}  [{want}]\n")
    for day in all_days:
        if day not in resolved:
            have = gaps[[g[0] for g in gaps].index(day)][1]
            print(f"  day {day}: MISSING -- no applied {want} route (have: {', '.join(have)})")
            continue
        options = resolved[day]
        entries = []
        multi = len(options) > 1
        for r in options:
            url = RWGPS_URL.format(r["route_id"])
            opt_tag = f"_opt{r['option']}" if multi else ""
            png_name = f"day{day:02d}{opt_tag}_{r['route_id']}.png"
            png_path = os.path.join(a.out, png_name)
            qrcode.make(url).save(png_path)
            label = f"option {r['option']}: " if multi else ""
            print(f"  day {day}: {label}{r['name']}")
            print(f"          {url}  ->  {png_path}")
            entries.append({
                "route_id": r["route_id"], "name": r["name"], "url": url,
                "png": png_path, "option": r["option"], "confidence": r["confidence"],
            })
        manifest["days"][day] = entries if multi else entries[0]

    manifest_path = os.path.join(a.out, "qr_manifest.json")
    json.dump(manifest, open(manifest_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n_multi = sum(1 for d in resolved.values() if len(d) > 1)
    print(f"\n{len(resolved)}/{len(all_days)} days resolved -- wrote {manifest_path}")
    if n_multi:
        print(f"({n_multi} day(s) had more than one rider-choice option -- both QR'd, see opt1/opt2 filenames)")
    if gaps:
        print(f"{len(gaps)} day(s) missing a {want} route -- see MISSING lines above")


def cmd_gap_report(a):
    """Sweep every tour+program+language combo already present in
    tours_resolved.csv for missing applied routes. Existing data debt, not
    something to fix right now -- so this stays quiet: full detail goes to
    a CSV file, only a one-line count hits the console."""
    rows = load_rows(a.csv)
    programs = tour_programs(rows)
    report_rows, combos_checked, missing_slots = [], 0, 0

    for tour in sorted(programs):
        for program in sorted(programs[tour]):
            matches = [r for r in rows if r["tour"] == tour and r["program"] == program]
            for lang in sorted(programs[tour][program]):
                combos_checked += 1
                all_days, resolved, gaps = resolve_days(matches, lang)
                if not gaps:
                    continue
                missing_slots += len(gaps)
                report_rows.append({
                    "tour": tour,
                    "program": program,
                    "language": lang,
                    "total_days": len(all_days),
                    "missing_days": ";".join(str(d) for d, _ in gaps),
                    "missing_count": len(gaps),
                })

    with open(a.gap_report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tour", "program", "language", "total_days",
                                          "missing_days", "missing_count"])
        w.writeheader()
        w.writerows(report_rows)

    print(f"wrote {a.gap_report}: {len(report_rows)}/{combos_checked} tour+program+language combos "
          f"have a gap ({missing_slots} missing day-slots total). Full detail is in the file -- "
          f"nothing to act on now.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV, help=f"default: {DEFAULT_CSV}")
    ap.add_argument("--list-tours", action="store_true")
    ap.add_argument("--tour", help="exact tour name (see --list-tours)")
    ap.add_argument("--program", help="exact program name -- required only if the tour has more than one")
    ap.add_argument("--lang", choices=["NL", "EN", "DE"], help="language this departure runs in")
    ap.add_argument("--out", default="qr_out", help="output folder for PNGs + manifest (default: qr_out/)")
    ap.add_argument("--gap-report", nargs="?", const="tour_language_gaps.csv", default=None,
                     help="sweep all tour+program+languages for gaps and write a CSV report "
                          "(default filename: tour_language_gaps.csv) instead of a single lookup")
    a = ap.parse_args()

    if a.list_tours:
        cmd_list_tours(a)
        return
    if a.gap_report:
        cmd_gap_report(a)
        return
    if not a.tour or not a.lang:
        ap.error("--tour and --lang are required (or use --list-tours / --gap-report)")
    cmd_lookup(a)


if __name__ == "__main__":
    main()
