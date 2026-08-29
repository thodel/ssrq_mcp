#!/usr/bin/env python3
"""ssrq_parse_ttl.py — build the person/org authority tables from the register TTL.

The README has named this script since the first release; until #11 it lived
outside the repo, in a scratch workspace on the server. That is how its bugs
went invisible: the deployed database carried editorial timestamps as
attestation years (Leo Jud, 1482–1542, "attested 2019") and HLS reference ids
as years (his HLS id 12013 became the year 1201), and nothing in this repo
could show where they came from.

This version parses the Turtle properly (rdflib) instead of scanning lines,
which also ends the cross-contamination of blank nodes that gave one person
another person's forename.

What counts as an attestation year, and what never does:

  read   births/birth, deaths/death, first_mentions/first_mention,
         last_mentions/last_mention, occupation/date, marital_statuses/date,
         and the nested format_date(s) blocks: when, from, to,
         notBefore, notAfter
  never  hist (the editor's save timestamp — "last touched by a researcher
         at SSRQ", explicitly out of scope), refs (GND/HLS identifiers),
         page/line/vol/article (citation locators)

Years outside the plausibility window are excluded AND reported, never
silently dropped: the warning is what shows the next artefact.

Usage:
    python ssrq_parse_ttl.py --input ssrq__fuseki_*.ttl --db register.db
    python ssrq_parse_ttl.py --input ... --db ssrq.db --replace
        (--replace rewrites persons/orgs/name_index inside an existing db,
         in one transaction, leaving documents/chunks/embeddings untouched)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter

PERS = "http://ssrq-sds-fds.ch/Register/schemas/persons/"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Blocks whose literals may carry attestation dates, and the keys inside them
# that hold the date text. Everything else is never read for years — most
# importantly pers:hist (editorial timestamps) and pers:refs (GND/HLS ids).
DATE_BLOCKS = {
    "births", "deaths", "first_mentions", "last_mentions",
    "frist_mentions",           # one record in the data spells it like this
    "occupation", "marital_statuses",
}
DATE_KEYS = {
    "birth", "death", "first_mention", "last_mention", "date",
    "when", "from", "to", "notBefore", "notAfter",
}
NESTED_DATE_BLOCKS = {"format_date", "format_dates"}

# Attestation years outside this window are artefacts until shown otherwise.
# The SSRQ editions run 1050-1846; births reach a little earlier, nothing in
# this register is attested after the 19th century. Out-of-window years are
# counted and printed, because the warning is what shows the next artefact.
YEAR_MIN, YEAR_MAX = 900, 1900

_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def years_in(text: str) -> list[int]:
    """Every 4-digit token: "1523-1542" gives both, "vor 17.05.1751" the year."""
    return [int(tok) for tok in _YEAR_RE.findall(text or "")]


def local(uri) -> str:
    """Predicate local name: .../persons/first_mention -> first_mention."""
    text = str(uri)
    return text.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def build(ttl_path: str, verbose: bool = True):
    """Parse the register and return (persons, orgs, warnings)."""
    import rdflib

    t0 = time.time()
    graph = rdflib.Graph()
    graph.parse(ttl_path, format="turtle")
    if verbose:
        print(f"parsed {len(graph):,} triples in {time.time() - t0:.0f}s",
              file=sys.stderr)

    # Index triples by subject once; walking blank nodes via graph.objects()
    # per node is quadratic in disguise on 3M triples.
    by_subject: dict = {}
    for s, p, o in graph:
        by_subject.setdefault(s, []).append((local(p), o))

    def date_literals(node, inside_dates: bool) -> list[str]:
        """Literal date texts under a block, descending only where dates live."""
        out = []
        for key, obj in by_subject.get(node, ()):
            if isinstance(obj, rdflib.Literal):
                if key in DATE_KEYS:
                    out.append(str(obj))
            elif key in NESTED_DATE_BLOCKS:
                out.extend(date_literals(obj, True))
        return out

    persons, orgs = {}, {}
    dropped_years: Counter = Counter()

    for subj, props in by_subject.items():
        if not isinstance(subj, rdflib.URIRef):
            continue
        ssrq_id = str(subj).rsplit("#", 1)[-1]
        etype = next((local(o) for k, o in props if k == "type"), "")
        if etype not in ("person", "family"):
            continue

        label = label_lang = None
        std_names, orig_names = [], []
        forename = surname = ""
        sex = org_type = ""
        years: set[int] = set()
        rel: dict[str, list[str]] = {k: [] for k in
                                     ("org", "spouse", "mother", "father", "loc")}

        for key, obj in props:
            if key == "label" or str(obj) == RDFS_LABEL:
                pass
            if isinstance(obj, rdflib.Literal):
                if key == "label":
                    label, label_lang = str(obj), obj.language or ""
                elif key == "sex":
                    sex = str(obj)
                elif key == "org_type":
                    org_type = str(obj)
                continue
            if key == "label":
                continue
            if key in DATE_BLOCKS:
                for text in date_literals(obj, True):
                    for year in years_in(text):
                        if YEAR_MIN <= year <= YEAR_MAX:
                            years.add(year)
                        else:
                            dropped_years[year] += 1
            elif key == "name":
                fields = dict((k, str(o)) for k, o in by_subject.get(obj, ())
                              if isinstance(o, rdflib.Literal))
                full = " ".join(filter(None, [fields.get("forename", ""),
                                              fields.get("surname", "")])).strip()
                if fields.get("type") == "orig":
                    if full:
                        orig_names.append(full)
                else:
                    if full:
                        std_names.append(full)
                    if not forename:
                        forename = fields.get("forename", "")
                    if not surname:
                        surname = fields.get("surname", "")
            elif key == "org_id" and isinstance(obj, rdflib.URIRef):
                rel["org"].append(str(obj).rsplit("#", 1)[-1])
            elif key in ("spouseOf", "motherOf", "fatherOf", "residence"):
                target = obj
                if not isinstance(obj, rdflib.URIRef):
                    target = next((o for k2, o in by_subject.get(obj, ())
                                   if k2 in ("id", "org_id", "location",
                                             "property_type")
                                   and isinstance(o, rdflib.URIRef)), None)
                if target is not None:
                    bucket = {"spouseOf": "spouse", "motherOf": "mother",
                              "fatherOf": "father", "residence": "loc"}[key]
                    rel[bucket].append(str(target).rsplit("#", 1)[-1])

        plain_label = label or (std_names[0] if std_names else "")
        record = {
            "id": ssrq_id, "uri": str(subj), "etype": etype,
            "label": plain_label, "label_lang": label_lang or "",
            # The curated label first: a std name variant is often the bare
            # forename ("Leo"), while the label is the name a historian would
            # search for ("Leo Jud").
            "std_name": plain_label or (std_names[0] if std_names else ""),
            "forename": forename, "surname": surname, "sex": sex,
            "years": sorted(years),
            "first_year": min(years) if years else None,
            "last_year": max(years) if years else None,
            "org_ids": sorted(set(rel["org"])),
            "spouse_ids": sorted(set(rel["spouse"])),
            "mother_ids": sorted(set(rel["mother"])),
            "father_ids": sorted(set(rel["father"])),
            "loc_ids": sorted(set(rel["loc"])),
            "std_names": std_names, "orig_names": orig_names,
            "org_type": org_type,
        }
        (persons if etype == "person" else orgs)[ssrq_id] = record

    warnings = [f"excluded implausible year {year} ({count}x) — outside "
                f"{YEAR_MIN}-{YEAR_MAX}; an artefact until shown otherwise"
                for year, count in sorted(dropped_years.items())]
    return persons, orgs, warnings


def write(db_path: str, persons: dict, orgs: dict, replace: bool) -> None:
    """Write the three register tables. --replace leaves every other table be."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        if replace:
            for table in ("persons", "orgs", "name_index"):
                conn.execute(f"DELETE FROM {table}")
        else:
            import db as db_module
            conn.executescript(db_module.SCHEMA_SQL)
        for rec in persons.values():
            conn.execute(
                "INSERT INTO persons (id, uri, etype, label, label_lang, "
                "std_name, forename, surname, sex, first_year, last_year, "
                "years, org_ids, spouse_ids, mother_ids, father_ids, loc_ids, "
                "orig_names, std_names) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["uri"], rec["etype"], rec["label"],
                 rec["label_lang"], rec["std_name"], rec["forename"],
                 rec["surname"], rec["sex"], rec["first_year"],
                 rec["last_year"], json.dumps(rec["years"]),
                 json.dumps(rec["org_ids"]), json.dumps(rec["spouse_ids"]),
                 json.dumps(rec["mother_ids"]), json.dumps(rec["father_ids"]),
                 json.dumps(rec["loc_ids"]), json.dumps(rec["orig_names"]),
                 json.dumps(rec["std_names"])))
            for name in rec["std_names"]:
                conn.execute("INSERT INTO name_index VALUES (?,?,0)",
                             (name, rec["id"]))
            for name in rec["orig_names"]:
                conn.execute("INSERT INTO name_index VALUES (?,?,1)",
                             (name, rec["id"]))
        for rec in orgs.values():
            conn.execute(
                "INSERT INTO orgs (id, uri, etype, label, std_name, surname, "
                "alias_of, org_type) VALUES (?,?,?,?,?,?,?,?)",
                (rec["id"], rec["uri"], rec["etype"], rec["label"],
                 rec["std_name"], rec["surname"], "", rec["org_type"]))
            for name in rec["std_names"] + rec["orig_names"]:
                conn.execute("INSERT INTO name_index VALUES (?,?,0)",
                             (name, rec["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="the register TTL export")
    parser.add_argument("--db", required=True, help="SQLite database to write")
    parser.add_argument("--replace", action="store_true",
                        help="rewrite persons/orgs/name_index inside an "
                             "existing db, leaving all other tables untouched")
    args = parser.parse_args(argv)

    persons, orgs, warnings = build(args.input)
    for line in warnings:
        print(f"WARNING: {line}", file=sys.stderr)

    write(args.db, persons, orgs, replace=args.replace)
    dated = sum(1 for p in persons.values() if p["first_year"])
    lo = min((p["first_year"] for p in persons.values() if p["first_year"]),
             default=None)
    hi = max((p["last_year"] for p in persons.values() if p["last_year"]),
             default=None)
    print(f"{len(persons):,} persons ({dated:,} with attestation years, "
          f"{lo}-{hi}), {len(orgs):,} orgs -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
