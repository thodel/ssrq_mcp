#!/usr/bin/env python3
"""
ingest_editio.py — load the SSRQ editions into the documents table.

Source: https://github.com/SSRQ-SDS-FDS/editio-data, the TEI-XML of the Swiss
Law Sources, CC BY-NC-SA 4.0. Attribution belongs in every answer that quotes
them; the licence also forbids commercial use, which is why the corpus is
served from our own infrastructure rather than redistributed.

    python ingest_editio.py --src /path/to/editio-data --db /data/ssrq.db

WHAT COUNTS AS A DOCUMENT. One TEI file. Of 5,596 files, 968 carry no <body> —
indices and front matter — and are skipped, leaving 4,628 with a transcription.

DATING. <origDate> is when the document was issued and is what a historian
means by its date. The same files also carry <date type="electronic">, the
date the *edition* was published. Reading that one would date a 1446 charter
to 2022, so only origDate is used, and a document without one is stored
undated rather than guessed at.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterator, Optional

import db as db_module

VIEW_URL = "https://www.ssrq-sds-fds.ch/persons-db-edit/?query={id}"

# <origDate when="1446-01-19"/>, or from=/to= for a range, or notBefore=.
#
# A file can carry several <origDate> elements, and they date different
# things. When the physical witness is a later copy, msDesc/history/origin
# dates the COPY — SSRQ-SG-III_4-202-1 is a 20th-century photocopy of a 1691
# Ordnung, and its origin says from="1901-01-01" to="2000-12-31" — while the
# document's own date sits in <filiation>. Two independent whole-file regexes
# paired a start from one element with an end from another, and seven
# documents came out as 1691–2000, 1438–1900 and the like (#13).
#
# So: attributes are paired WITHIN one element, and the FIRST dated
# origDate in the file wins. That is the header's date — msDesc precedes the
# body, and within msDesc the filiation (the original's date, for a copy)
# precedes history/origin (the copy's). The origDates further down sit in the
# transcription itself and date other things: SSRQ-ZH-NF_I_2_1-170-1 is a
# 1497 vidimus whose text carries the inserted 1275 charter's origDate, and
# "earliest origDate wins" — the first attempt at this fix — would have
# redated the vidimus to the charter it confirms.
_ORIG_ELEM = re.compile(r"<origDate\b[^>]*>")
_ORIG_START = re.compile(r'(?:when|from|notBefore)="(-?\d{3,4})')
_ORIG_END = re.compile(r'(?:to|notAfter)="(-?\d{3,4})')


def origination(xml: str) -> tuple[Optional[int], Optional[int]]:
    """(origin_from, origin_to) of the document — not of a copy, not of a
    charter quoted inside it, not of a later hand's addition."""
    for tag in _ORIG_ELEM.findall(xml):
        start = _ORIG_START.search(tag)
        end = _ORIG_END.search(tag)
        if start or end:
            return (int(start.group(1)) if start else None,
                    int(end.group(1)) if end else None)
    return None, None


_IDNO = re.compile(r"<idno[^>]*>([^<]+)</idno>")
# The document's own title is the first <head>; <title> is the series
# ("IX. Abteilung: Die Rechtsquellen des Kantons Freiburg…"), which is the
# same string for every file in a volume and useless in a citation.
_HEAD = re.compile(r"<head[^>]*>(.*?)</head>", re.S)
_PLACE = re.compile(r"<placeName[^>]*>(.*?)</placeName>", re.S)
_BODY = re.compile(r"<body>(.*?)</body>", re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: str) -> str:
    return _WS.sub(" ", _TAGS.sub(" ", fragment)).strip()


def _title(xml: str) -> str:
    """The document's own heading.

    Editions carry parallel headings, German then French, so the first is
    taken. <title> would give the series title, identical across a whole
    volume — "IX. Abteilung: Die Rechtsquellen des Kantons Freiburg…" — which
    identifies nothing.
    """
    head = _HEAD.search(xml)
    return _text(head.group(1)) if head else ""


# The TEI declares no language for the transcription itself: xml:lang on <TEI>
# is the language of the *edition's* metadata, and every Fribourg file carries
# "de" there whether the charter is German or French. So it is detected, and
# the column is read as detected rather than declared. Function words are used
# because they survive the orthography: fifteenth-century Alemannic spells
# almost everything differently, but still writes "und", "der", "das".
_DE_MARKERS = (" und ", " der ", " die ", " das ", " den ", " ist ", " nicht ",
               " zu ", " von ", " mit ", " wir ", " sind ")
_FR_MARKERS = (" et ", " le ", " la ", " les ", " des ", " que ", " qui ",
               " pour ", " est ", " dans ", " aux ", " nous ")
# A third of the undated older material is Latin — episcopal and imperial
# charters. Without this they fall out of every language filter.
# Latin was tried and dropped. The transcriptions mix Latin source text with a
# German editorial apparatus, so German markers outscore Latin ones even in an
# episcopal charter — the detector found two documents out of roughly eight
# hundred that plainly are Latin. Leaving them unlabelled is the honest result;
# a wrong tag would drop them from a language filter silently.


def _detect_language(text: str) -> str:
    """'de', 'fr', or '' when neither is clear enough to claim.

    Function words rather than a model: they survive the orthography, which
    here is anything but standard — fifteenth-century Alemannic spells almost
    every content word differently but still writes "und", "der", "das".

    The decision needs a margin rather than a maximum: where neither language
    is clearly ahead the field stays empty. A wrong tag would silently drop
    documents from a language filter, which is worse than an absent one — and
    roughly eight hundred of these transcriptions are Latin under a German
    apparatus, which no word-count separates cleanly.
    """
    sample = f" {text[:4000].lower()} "
    scores = {
        "de": sum(sample.count(m) for m in _DE_MARKERS),
        "fr": sum(sample.count(m) for m in _FR_MARKERS),
    }
    best, count = max(scores.items(), key=lambda kv: kv[1])
    runner_up = max(v for k, v in scores.items() if k != best)
    return best if count >= 3 and count > runner_up * 1.5 else ""


def parse(path: Path, root: Path) -> Optional[dict]:
    """One TEI file as a row, or None when it carries no transcription."""
    xml = path.read_text(encoding="utf-8", errors="replace")
    body = _BODY.search(xml)
    if not body:
        return None
    text = _text(body.group(1))
    if not text:
        return None

    idno = _IDNO.search(xml)
    doc_id = idno.group(1).strip() if idno else path.stem
    relative = path.relative_to(root).parts
    origin_from, origin_to = origination(xml)

    return {
        "id": doc_id,
        # data/<canton>/<volume>/<file>.xml
        "canton": relative[1] if len(relative) > 2 else "",
        "volume": relative[2] if len(relative) > 3 else "",
        "title": _title(xml),
        "lang": _detect_language(text),
        "origin_from": origin_from,
        "origin_to": origin_to,
        "place": (_text(_PLACE.search(xml).group(1)) if _PLACE.search(xml) else ""),
        "text": text,
        "n_chars": len(text),
        "url": VIEW_URL.format(id=doc_id),
    }


def documents(src: Path) -> Iterator[dict]:
    for path in sorted((src / "data").rglob("*.xml")):
        row = parse(path, src)
        if row:
            yield row


def update_dates(src: Path, db_path: str, quiet: bool = False) -> int:
    """Recompute origin_from/origin_to for every ingested document — only that.

    A full re-ingest rewrites text and rebuilds the FTS index; a date fix
    should not touch either. Returns the number of rows that changed.
    """
    conn = sqlite3.connect(db_path)
    changed = 0
    try:
        conn.execute("BEGIN")
        for path in sorted((src / "data").rglob("*.xml")):
            xml = path.read_text(encoding="utf-8", errors="replace")
            idno = _IDNO.search(xml)
            doc_id = idno.group(1).strip() if idno else path.stem
            origin_from, origin_to = origination(xml)
            cursor = conn.execute(
                "UPDATE documents SET origin_from = ?, origin_to = ? "
                "WHERE id = ? AND (origin_from IS NOT ? OR origin_to IS NOT ?)",
                (origin_from, origin_to, doc_id, origin_from, origin_to))
            changed += cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if not quiet:
        lo, hi = conn.execute(
            "SELECT MIN(origin_from), MAX(COALESCE(origin_to, origin_from)) "
            "FROM documents").fetchone()
        print(f"{changed} Datierungen korrigiert; Dokumentspanne jetzt {lo}\u2013{hi}",
              file=sys.stderr)
    conn.close()
    return changed


def build(src: Path, db_path: str, quiet: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    conn.executescript(db_module.SCHEMA_SQL)
    conn.execute("DELETE FROM documents")
    # External-content FTS: deleting rows corrupts the index, so it is rebuilt
    # from the table afterwards rather than maintained by trigger.
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('delete-all')")

    cols = ("id", "canton", "volume", "title", "lang", "origin_from",
            "origin_to", "place", "text", "n_chars", "url")
    placeholders = ",".join("?" * len(cols))
    batch, total = [], 0
    for row in documents(src):
        batch.append(tuple(row[c] for c in cols))
        if len(batch) >= 500:
            conn.executemany(f"INSERT OR REPLACE INTO documents VALUES ({placeholders})", batch)
            total += len(batch); batch.clear()
            if not quiet:
                print(f"  {total} Dokumente …", file=sys.stderr)
    if batch:
        conn.executemany(f"INSERT OR REPLACE INTO documents VALUES ({placeholders})", batch)
        total += len(batch)

    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
    conn.commit()

    if not quiet:
        dated, lo, hi = conn.execute(
            "SELECT COUNT(origin_from), MIN(origin_from), MAX(origin_from) FROM documents"
        ).fetchone()
        chars = conn.execute("SELECT SUM(n_chars) FROM documents").fetchone()[0] or 0
        print(f"{total} Dokumente, {chars/1e6:.1f} Mio Zeichen, "
              f"{dated} datiert ({lo}–{hi})", file=sys.stderr)
    conn.close()
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="checkout of SSRQ-SDS-FDS/editio-data")
    ap.add_argument("--db", default=os.environ.get("SSRQ_DB", "/data/ssrq.db"))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--update-dates", action="store_true",
                    help="recompute origin_from/origin_to only; leave text, "
                         "FTS and everything else untouched")
    args = ap.parse_args(argv)

    src = Path(args.src)
    if not (src / "data").is_dir():
        raise SystemExit(f"{src}/data not found — is this a checkout of editio-data?")
    if args.update_dates:
        update_dates(src, args.db, quiet=args.quiet)
    else:
        build(src, args.db, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
