"""Compute demand scores for every (hospital, specialty) and write dashboard/data.json.

Inputs (see README section in specialist-demand-dashboard-prompt.md):
  data/hospitals_sirs.json        <- sirs_scrape.py
  data/population_kabkota.csv     <- scripts/fetch_population.py
  data/kmk1277_kjsu_kia.csv       (+ data/kmk_matches_confirmed.csv from the review screen)
"""
import csv
import json
import math
import os
import statistics
import time
from collections import Counter, defaultdict

import dreams_match
import kmk_match
from regions import kab_key, kab_match, province_key
from scoring import (AREA_METHODS, BANDS, BASIC7, CATCHMENT_KM, EITHER, FLOOR, GLOBAL_RATIO, NO_POPULATION, SAO,
                     area_catchment, area_custom, area_global, area_national, band_index, required_for, score_pair,
                     two_step_catchment)
from specialties import NAMES, NAMES_ID, is_doctor_specialist, normalize

HERE = os.path.dirname(os.path.abspath(__file__))
SIRS = os.path.join(HERE, "data", "hospitals_sirs.json")
POP = os.path.join(HERE, "data", "population_kabkota.csv")
OUT = os.path.join(HERE, "dashboard", "data.json")
SPECIALTIES = list(NAMES)
LABELS = ["Low", "Moderate", "High"]
PLACEHOLDER = (-2.4185588, 108.4919086)  # SIRS default point used when a hospital never set its location
MIN_PEERS = 10
DEFAULT_AREA = "custom"  # area method stored in the score rows; the dashboard can switch it


def ownership(owner):
    if owner in ("Pemkab", "Pemkot", "Pemprop", "Kemkes", "Kementerian Lain"):
        return "Government"
    if owner.startswith("TNI") or owner == "POLRI":
        return "TNI/Polri"
    if owner == "BUMN":
        return "BUMN"
    return "Private"


def in_indonesia(lat, lng):
    return lat is not None and lng is not None and -11.5 < lat < 6.5 and 94 < lng < 141.5


def fix_coords(hospitals):
    """Repair swapped coordinates; place missing/implausible ones at the kab/kota center (flagged approx)."""
    for h in hospitals:
        h["approx"] = False
        lat, lng = h.get("lat"), h.get("lng")
        if (lat, lng) == PLACEHOLDER:
            h["lat"] = h["lng"] = None
        elif not in_indonesia(lat, lng) and in_indonesia(lng, lat):
            h["lat"], h["lng"] = lng, lat
        elif not in_indonesia(lat, lng):
            h["lat"] = h["lng"] = None
    groups = defaultdict(list)
    for h in hospitals:
        if h["lat"] is not None:
            groups[h["kab_id"]].append(h)
    centers = {k: (statistics.median(x["lat"] for x in v), statistics.median(x["lng"] for x in v)) for k, v in groups.items()}
    for i, h in enumerate(hospitals):
        c = centers.get(h["kab_id"])
        far = h["lat"] is not None and c and len(groups[h["kab_id"]]) >= 3 and (abs(h["lat"] - c[0]) + abs(h["lng"] - c[1])) > 1.5
        if (h["lat"] is None or far) and c:
            # Spread approximate pins slightly so they don't stack on one point.
            h["lat"] = c[0] + ((i * 37) % 11 - 5) * 0.004
            h["lng"] = c[1] + ((i * 53) % 11 - 5) * 0.004
            h["approx"] = True


def peer_groups(hospitals):
    """Peer = same kind and bed band, merged with neighbouring bands until it has MIN_PEERS hospitals."""
    by_kind = defaultdict(lambda: defaultdict(list))
    for h in hospitals:
        by_kind[h["kind"]][band_index(h["beds"])].append(h)
    groups = {}
    for kind, bands in by_kind.items():
        for b in range(len(BANDS)):
            if not bands.get(b):
                continue
            lo = hi = b
            members = list(bands[b])
            while len(members) < MIN_PEERS and (lo > 0 or hi < len(BANDS) - 1):
                # Grow toward whichever neighbour is closer to the original band, alternating.
                if lo > 0 and (b - lo <= hi - b or hi == len(BANDS) - 1):
                    lo -= 1
                    members += bands.get(lo, [])
                else:
                    hi += 1
                    members += bands.get(hi, [])
            label = BANDS[lo][2] if lo == hi else f"{BANDS[lo][2].split('–')[0].rstrip('≥<')}–{BANDS[hi][2].split('–')[-1].lstrip('≥<')}"
            if lo != hi and hi == len(BANDS) - 1:
                label = f"≥{BANDS[lo][0]}"
            if lo == 0 and lo != hi:
                label = f"<{BANDS[hi][1]}"
            medians = {s: statistics.median(m["counts"].get(s, 0) for m in members) for s in SPECIALTIES}
            groups[(kind, b)] = {"kind": kind, "band": label, "size": len(members), "medians": medians}
    return groups


def kab_centers(hospitals):
    """Median location of each kab/kota's hospitals, preferring exact pins over approximate ones."""
    groups = defaultdict(list)
    for h in hospitals:
        if h["lat"] is not None:
            groups[h["kab_id"]].append(h)
    out = {}
    for k, hs in groups.items():
        exact = [h for h in hs if not h["approx"]] or hs
        out[k] = (statistics.median(h["lat"] for h in exact), statistics.median(h["lng"] for h in exact))
    return out


def km(a, b):
    """Straight-line distance in km between (lat, lng) points (equirectangular; fine at catchment scale)."""
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371 * math.hypot(x, math.radians(b[0] - a[0]))


def catchment_links(hospitals, centers, radius):
    """Kab/kota each hospital serves: its own, plus every kab/kota whose center is within radius km."""
    links = {}
    for h in hospitals:
        reach = {h["kab_id"]}
        if h["lat"] is not None:
            p = (h["lat"], h["lng"])
            reach.update(k for k, c in centers.items() if abs(c[0] - p[0]) < 1 and km(p, c) <= radius)
        links[h["kode"]] = reach
    return links


def area_tables(hospitals, kabs, density, national_median, tiers_by_hospital):
    """Kab-level (points, (en, id) reason) per area method and specialty, plus what the Regions screen shows.
    Reasons carry a {kab} placeholder that the dashboard fills in, so the reason table stays small."""
    # The three new methods count the 7 basic specialists at public hospitals from Kemenkes DREAMS where it
    # covers them (base specialists, plus SIRS subspecialists): SIRS often reports none where DREAMS lists one.
    best = {}
    for h in hospitals:
        c = Counter(h["counts"])
        for code, d in (h.get("dreams") or {}).items():
            c[code] = d[3] + h["sub"].get(code, 0)
        best[h["kode"]] = c
    by_kab = defaultdict(list)
    for h in hospitals:
        by_kab[h["kab_id"]].append(h)
    pops = {kid: k["population"] for kid, k in kabs.items()}
    totals = {kid: sum((best[h["kode"]] for h in by_kab[kid]), Counter()) for kid in kabs}
    gov = {kid: sum((best[h["kode"]] for h in by_kab[kid] if ownership(h["pemilik"]) == "Government"), Counter()) for kid in kabs}
    national_ratio = {s: sum(t[s] for t in totals.values()) / sum(pops.values()) * 1e5 for s in SPECIALTIES}
    links = catchment_links(hospitals, kab_centers(hospitals), CATCHMENT_KM)
    access = {s: two_step_catchment(pops, links, {h["kode"]: best[h["kode"]][s] for h in hospitals}) for s in SPECIALTIES}

    def per100k(kid, n):
        return n / pops[kid] * 1e5 if pops[kid] else None

    out = {}
    for kid, k in kabs.items():
        name, hs = "{kab}", by_kab[kid]
        needs = {h["kode"]: required_for(tiers_by_hospital.get(h["kode"], {})) for h in hs}
        sao = per100k(kid, sum(totals[kid][s] for s in SAO))
        rows = {m: [] for m in AREA_METHODS}
        for s in SPECIALTIES:
            dens = density.get(kid, {}).get(s)
            rows["custom"].append(NO_POPULATION if dens is None else area_custom(s, k["totals"].get(s, 0), dens, national_median[s], name))
            # National: public target hospitals are the DREAMS-tracked ones (7 basics) and KMK hospitals needing s.
            kmk = [(h, svc, tier, either) for h in hs for svc, tier, either in needs[h["kode"]].get(s, [])]
            either_only = s not in BASIC7 and kmk and all(e for *_, e in kmk)
            group = EITHER if either_only else (s,)
            has = lambda c: any(c[g] > 0 for g in group)  # noqa: E731
            targets = {h["kode"]: h for h in hs if s in BASIC7 and h.get("dreams")}
            targets.update({h["kode"]: h for h, *_ in kmk})
            lacking = sum(1 for h in targets.values() if not has(best[h["kode"]]))
            rows["national"].append(area_national(s, sum(totals[kid][g] for g in group), sum(gov[kid][g] for g in group),
                                                  len(targets), lacking, [(svc, tier) for _, svc, tier, _ in kmk], name))
            d = per100k(kid, totals[kid][s])
            rows["global"].append(NO_POPULATION if d is None else area_global(s, totals[kid][s], d, sao, name))
            rows["catchment"].append(NO_POPULATION if not pops[kid] else area_catchment(s, access[s][kid], national_ratio[s], name))
        out[kid] = {"rows": rows, "best": [totals[kid][s] for s in SPECIALTIES],
                    "access": [round(access[s][kid], 3) for s in SPECIALTIES]}
    bench = {"custom": [round(national_median[s], 4) for s in SPECIALTIES],
             "global": [GLOBAL_RATIO[s][0] if s in GLOBAL_RATIO else None for s in SPECIALTIES],
             "global_group": [bool(GLOBAL_RATIO.get(s, (0, 0, False))[2]) for s in SPECIALTIES],
             "catchment": [round(national_ratio[s], 4) for s in SPECIALTIES], "national": None}
    return out, bench


def load_spot_check():
    path = os.path.join(HERE, "data", "spot_check.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf8") as f:
        return list(csv.DictReader(f))


def main():
    started = time.time()
    data = json.load(open(SIRS, encoding="utf8"))
    hospitals = data["hospitals"]

    # Population, keyed by (province, kab) with the same normalization as SIRS
    pops = {}
    with open(POP, encoding="utf8") as f:
        for r in csv.DictReader(f):
            pops.setdefault(province_key(r["provinsi"]), {})[kab_key(r["kab_kota"])] = r
    kabs = {}
    for prov_rows in pops.values():
        for r in prov_rows.values():
            kabs[r["kab_code"]] = {"id": r["kab_code"], "provinsi": r["provinsi"], "kab_kota": r["kab_kota"],
                                   "population": int(r["population"]), "totals": Counter(), "hospitals": 0}

    unmapped = Counter()
    for h in hospitals:
        prov_rows = pops.get(province_key(h["provinsi"]), {})
        k = kab_match(kab_key(h["kab_kota"]), prov_rows)
        h["kab_id"] = prov_rows[k]["kab_code"] if k else f"x:{h['provinsi']}|{h['kab_kota']}"
        counts, sub = Counter(), Counter()
        for s in h["staff"]:
            if not is_doctor_specialist(s["group"], s["type"]) or s["count"] <= 0:
                continue
            code, is_sub = normalize(s["group"], s["type"])
            if code is None:
                unmapped[s["type"]] += s["count"]
                continue
            counts[code] += s["count"]
            if is_sub:
                sub[code] += s["count"]
        h["counts"], h["sub"] = counts, sub
        h["beds"] = h.get("beds_total") or 0
        h["general"] = h["jenis"] == "RSU"
        h["kind"] = "RSU" if h["general"] else h["jenis"]
        if h["kab_id"] in kabs:
            kabs[h["kab_id"]]["totals"].update(counts)
            kabs[h["kab_id"]]["hospitals"] += 1
    fix_coords(hospitals)

    density = {kid: {s: k["totals"].get(s, 0) / k["population"] * 1e5 for s in SPECIALTIES}
               for kid, k in kabs.items() if k["population"] > 0}
    national_median = {s: statistics.median(d[s] for d in density.values()) for s in SPECIALTIES}

    dreams_total, dreams_unmatched = dreams_match.match(hospitals)
    # SIRS vs DREAMS: compare SIRS specialists without subspecialists, as DREAMS counts base specialties.
    disagreements = []
    for h in hospitals:
        for code, (_, _, _, total) in h.get("dreams", {}).items():
            sirs = h["counts"].get(code, 0) - h["sub"].get(code, 0)
            if sirs != total:
                disagreements.append([h["kode"], code, sirs, total])

    matches = kmk_match.match_all(hospitals)
    tiers_by_hospital, kmk_by_hospital = {}, {}
    for m in matches:
        if m["sirs_kode"]:
            tiers_by_hospital[m["sirs_kode"]] = m["tiers"]
            kmk_by_hospital[m["sirs_kode"]] = {"tiers": m["tiers"], "page": m["halaman_pdf"], "nama": m["nama_rs"]}

    groups = peer_groups(hospitals)
    areas, bench = area_tables(hospitals, kabs, density, national_median, tiers_by_hospital)
    reason_table, reason_table_id, reason_index = [], [], {}

    def rid(en, id_):
        if en not in reason_index:
            reason_index[en] = len(reason_table)
            reason_table.append(en)
            reason_table_id.append(id_)
        return reason_index[en]

    out_h = []
    label_counts = Counter()
    for h in hospitals:
        g = groups[(h["kind"], band_index(h["beds"]))]
        tiers = tiers_by_hospital.get(h["kode"], {})
        area = areas.get(h["kab_id"])
        scores = []
        for si, s in enumerate(SPECIALTIES):
            r = score_pair(specialty=s, general=h["general"], tiers=tiers, counts=h["counts"], peer_median=g["medians"][s],
                           area=area["rows"][DEFAULT_AREA][si] if area else NO_POPULATION,
                           band_label=g["band"], peer_label=h["kind"], peer_count=g["size"])
            label_counts[r["label"]] += 1
            # Compact row: specialty, n, peer median, A, B, C, D, total, label, reason ids without the area
            # reason, KMK service, relevant (area gate), boost. D/total/label are for DEFAULT_AREA; the
            # dashboard recomputes them for the area method picked in Settings.
            scores.append([si, r["n"], g["medians"][s], r["A"], r["B"], r["C"], r["D"], r["total"],
                           LABELS.index(r["label"]), [rid(en, id_) for en, id_ in r["base_reasons"]],
                           f"{r['b_service'][0]}|{r['b_service'][1]}" if r["b_service"] else None,
                           int(r["relevant"]), r["boost"]])
        out_h.append({
            "id": h["kode"], "name": h["nama"], "prov": h["provinsi"], "kab": h["kab_kota"], "kab_id": h["kab_id"],
            "owner": h["pemilik"], "own": ownership(h["pemilik"]), "kelas": h["kelas"], "type": h["jenis"],
            "general": h["general"], "beds": h["beds"], "band": g["band"], "peers": g["size"],
            "lat": round(h["lat"], 5) if h["lat"] is not None else None,
            "lng": round(h["lng"], 5) if h["lng"] is not None else None, "approx": h["approx"],
            "address": h["alamat"], "phone": h["telepon"], "kmk": kmk_by_hospital.get(h["kode"]),
            "sub": dict(h["sub"]), "scores": scores, "dreams": h.get("dreams"),
        })

    quality = {
        "kmk_total": len(matches),
        "kmk_matched": sum(1 for m in matches if m["sirs_kode"]),
        "kmk_status": dict(Counter(m["status"] for m in matches)),
        "review": [m for m in matches if m["status"] in ("review",)],
        "no_match": [m for m in matches if m["status"] == "no_match"],
        "missing_beds": [h["kode"] for h in hospitals if not h["beds"]],
        "missing_population": sorted({h["kab_kota"] for h in hospitals if h["kab_id"] not in kabs}),
        "approx_location": sum(1 for h in hospitals if h["approx"]),
        "no_location": sum(1 for h in hospitals if h["lat"] is None),
        "unmapped_labels": unmapped.most_common(),
        "dreams_total": dreams_total,
        "dreams_matched": sum(1 for h in hospitals if h.get("dreams")),
        "dreams_unmatched": dreams_unmatched,
        "dreams_pairs": sum(len(h["dreams"]) for h in hospitals if h.get("dreams")),
        "dreams_disagree": disagreements,
        "spot_check": load_spot_check(),
        "operational_since": False,
        "vacancies": False,
    }
    payload = {
        "meta": {"built": time.strftime("%Y-%m-%d %H:%M"), "sirs_downloaded": data["meta"]["downloaded"],
                 "population_source": "Dukcapil Kemendagri (AGR_VISUAL_KAB_FIX), total "
                                      f"{sum(k['population'] for k in kabs.values()):,}"},
        "specialties": [{"code": s, "name": NAMES[s], "name_id": NAMES_ID.get(s, NAMES[s]), "floor": s in FLOOR} for s in SPECIALTIES],
        "national_median": [round(national_median[s], 4) for s in SPECIALTIES],
        # Area check per method: kab area[method][si] = points (null = skipped), area_r = reason id (-1 = none).
        "area": {"default": DEFAULT_AREA, "methods": list(AREA_METHODS), "km": CATCHMENT_KM, "bench": bench,
                 "missing": rid(*NO_POPULATION[1])},
        "kabs": [{"id": k["id"], "prov": k["provinsi"], "kab": k["kab_kota"], "pop": k["population"],
                  "hospitals": k["hospitals"], "totals": [k["totals"].get(s, 0) for s in SPECIALTIES],
                  "best": areas[k["id"]]["best"], "access": areas[k["id"]]["access"],
                  "area": {m: [p for p, _ in areas[k["id"]]["rows"][m]] for m in AREA_METHODS},
                  "area_r": {m: [rid(*r) if r else -1 for _, r in areas[k["id"]]["rows"][m]] for m in AREA_METHODS}}
                 for k in kabs.values()],
        "reasons": reason_table,
        "reasons_id": reason_table_id,
        "hospitals": out_h,
        "quality": quality,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out_h)} hospitals × {len(SPECIALTIES)} specialties; labels {dict(label_counts)}; "
          f"KMK matched {quality['kmk_matched']}/{quality['kmk_total']}; "
          f"{os.path.getsize(OUT) // 1024} KB in {time.time() - started:.1f}s")
    return payload


if __name__ == "__main__":
    main()
