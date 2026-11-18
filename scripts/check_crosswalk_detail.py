#!/usr/bin/env python3
"""Check data/crosswalk/department_aliases_detail.csv.

1. COVERAGE (fatal): every (book_id, source_file, dept_name) that occurs in
   data/extracted/<book>/department_detail.csv and positions.csv has exactly one alias
   row, and no alias row names a pair that does not occur.  Exit status 1 if not.
2. ROLL-UP EVIDENCE (reported): for each book and each budget-year column, the section
   figures mapped to a dept_id are summed and compared with that department's
   citywide-statement all-funds figure (and general-fund figure where the section data
   has a by_fund split).

   Section figure, per dept_id and column, is tried under each of these (never added
   together, so a dept_summary_* page is never double counted with its sub-sections):
     by_type | by_program | by_fund | by_program_type |
     dept_summary_by_type | dept_summary_by_fund
   The method closest to the statement is reported.  A statement figure that the book
   itself prints inconsistently (general fund + other funds != all funds) is also tried
   as an alternative reference.

   Misses are explained, in this order, by
     * a mismatch recorded in the book's own checks.json (known book inconsistency),
     * a statement line whose all-funds figure disagrees with its own GF+OF,
     * a section total in the same book equal to the size of the miss (suspect mapping),
   else reported UNRESOLVED.

The statement-name -> dept_id mapping used here is local to this check and exists only
to line the two sides up.  If data/crosswalk/department_aliases_statement.csv exists it
is used instead (columns book_id, printed_name|dept_name, dept_id).

Usage:  .venv/bin/python scripts/check_crosswalk_detail.py [--book ID ...] [-v] [--tree]
"""
import argparse
import collections
import csv
import itertools
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, 'data', 'extracted')
XW = os.path.join(ROOT, 'data', 'crosswalk')
ALIASES = os.path.join(XW, 'department_aliases_detail.csv')
STMT_ALIASES = os.path.join(XW, 'department_aliases_statement.csv')
DEPTS = os.path.join(XW, 'departments.csv')
FILES = ('department_detail', 'positions')
METHODS = ('by_type', 'by_program', 'by_fund', 'by_program_type',
           'dept_summary_by_type', 'dept_summary_by_fund')
GF_RX = re.compile(r'^(\d+\s*-?\s*)?general fund$', re.I)


def norm(s):
    s = s.lower().replace('’', "'").replace('‘', "'").replace('�', 'e')
    s = re.sub(r'\s*-\s*', ' - ', s)
    return re.sub(r'\s+', ' ', s).strip().rstrip('.')


# ---------------------------------------------------------------- statement side
STMT_RULES = [
    (r'payroll error', 'other'),
    (r'secondary employment', 'police-secondary-employment'),
    (r'independent police|ind\. police|indep police', 'independent-police-monitor'),
    (r'ethics', 'ethics-review-board'),
    (r'inspector general|^oig', 'inspector-general'),
    (r'alcoholic', 'alcoholic-beverage-control'),
    (r'code enforc', 'code-enforcement'),
    (r'chief administrative|^cao', 'cao'),
    (r'planning', 'city-planning'),
    (r'^civil court', 'civil-court'),
    (r'civil service', 'civil-service'),
    (r'clerk|clrk', 'clerk-criminal-court'),
    (r'coroner', 'coroner'),
    (r'council', 'council'),
    (r'criminal district court', 'criminal-district-court'),
    (r'sheriff', 'sheriff'),
    (r'district attorney', 'district-attorney'),
    (r'economic dev', 'economic-development-fund'),
    (r"finance|^bureau of|director's office", 'finance'),
    (r'fire', 'fire'),
    (r'first city', 'first-city-court'),
    (r'general services', 'general-services'),
    (r'hdlc|historic', 'hdlc'),
    (r'human services|jjic|juvenile justice', 'human-services'),
    (r'health|^ems$', 'health'),
    (r'intergovernmental', 'intergovernmental'),
    (r'judicial retirement', 'judicial-retirement'),
    (r'juvenile court', 'juvenile-court'),
    (r'^law|city attorney', 'law'),
    (r'library|pubic library', 'library'),
    (r'miscellaneous', 'miscellaneous'),
    (r'mayor', 'mayor'),
    (r'mosquito', 'mosquito-control'),
    (r'municipal court|traffic court|municipal and traffic|municipal traffic', 'municipal-traffic-court'),
    (r'yacht', 'yacht-harbor'),
    (r'museum|art museum', 'museum-of-art'),
    (r'neighborhood 1', 'neighborhood-1'),
    (r'n.hood hsg|neighborhood housing', 'neighborhood-housing-fund'),
    (r'training', 'office-of-training'),
    (r'parks|parkway', 'parks-parkways'),
    (r'police', 'police'),
    (r'property management', 'property-management'),
    (r'public works', 'public-works'),
    (r'recreation', 'recreation'),
    (r'registrar', 'registrar-of-voters'),
    (r'safety', 'safety-permits'),
    (r'sanitation', 'sanitation'),
    (r'vcc|vieux', 'vcc'),
    (r'workforce', 'workforce-investment'),
    (r'aviation', 'aviation-board'),
    (r'french market', 'french-market'),
    (r'canal st', 'canal-street-development'),
    (r'rivergate', 'rivergate'),
    (r'communication', 'communication-district'),
    (r'building corp', 'building-corp'),
    (r'comm\. dev|comm dev|community dev|housing and community', 'community-development'),
]
# (book_id, normalised statement name) -> dept_id, where the printed name does not
# describe the money in that book (found by roll-up, see report)
STMT_BOOK_OVERRIDES = {}

# Misses diagnosed by hand (book, dept_id) -> explanation; shown instead of UNRESOLVED
MANUAL = {
    ('2025-operating-adopted', 'cao'): 'SECTION MISSING: the 2025 adopted book prints no CAO core page (only ITI, FEMA and the Miscellaneous-CAO page); gap = CAO core (cf. 2025 proposed)',
    ('2026-operating-adopted', 'miscellaneous'): 'IN-BOOK LAYOUT: the 2024-actual statement column has a finer layout (OPSE on its own line, 14,640,724; remaining 40,264,258 offset against CAO is recorded in checks.json); OPSE section is mapped to Miscellaneous for the 2025/2026 columns',
    ('2027-operating-proposed', 'mayor'): 'UNCERTAIN: all-funds off by 309,990 with JJIC mapped to Mayor (GF exact); CJC section is absent from this book',
}

_stmt_file = None


def _load_stmt_file():
    global _stmt_file
    if _stmt_file is not None:
        return _stmt_file
    _stmt_file = {}
    if os.path.exists(STMT_ALIASES):
        with open(STMT_ALIASES, newline='', encoding='utf-8') as f:
            rd = csv.DictReader(f)
            cols = rd.fieldnames or []
            ncol = next((c for c in ('printed_name', 'dept_name', 'statement_name', 'name') if c in cols), None)
            if ncol and 'dept_id' in cols:
                for r in rd:
                    _stmt_file[(r.get('book_id', ''), norm(r[ncol]))] = r['dept_id']
                    _stmt_file.setdefault(('', norm(r[ncol])), r['dept_id'])
    return _stmt_file


def stmt_dept(book, name):
    f = _load_stmt_file()
    n = norm(name)
    if (book, n) in f:
        return f[(book, n)]
    if (book, n) in STMT_BOOK_OVERRIDES:
        return STMT_BOOK_OVERRIDES[(book, n)]
    if f and ('', n) in f:
        return f[('', n)]
    for rx, d in STMT_RULES:
        if re.search(rx, n):
            return d
    return None


# ---------------------------------------------------------------- loaders
def books():
    return [b for b in sorted(os.listdir(EX)) if os.path.isdir(os.path.join(EX, b))]


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_names(book):
    """{file: {dept_name: row_count}}"""
    out = {}
    for fn in FILES:
        p = os.path.join(EX, book, fn + '.csv')
        if os.path.exists(p):
            c = collections.Counter(r['dept_name'] for r in read_csv(p))
            out[fn] = c
    return out


def load_sections(book):
    tot = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(int)))
    gf = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(int)))
    for r in read_csv(os.path.join(EX, book, 'department_detail.csv')):
        k = (r['fiscal_year'], r['basis'])
        a = int(r['amount'])
        tot[r['dept_name']][r['table']][k] += a
        if r['table'] in ('by_fund', 'dept_summary_by_fund') and GF_RX.match(r['dim1'].strip()):
            gf[r['dept_name']][r['table']][k] += a
    return tot, gf


def load_statement(book):
    """{(name, fund_group): {col: amount}}; all_funds synthesised from GF+OF only for
    the columns where the book prints an other-funds figure (2011 book)."""
    st = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in read_csv(os.path.join(EX, book, 'department_expenditures.csv')):
        st[(r['dept_name'], r['fund_group'])][(r['fiscal_year'], r['basis'])] += int(r['amount'])
    if not any(fg == 'all_funds' for (_, fg) in st):
        of_cols = {k for (n, fg), v in st.items() if fg == 'other_funds' for k in v}
        for (n, fg), v in list(st.items()):
            if fg in ('general_fund', 'other_funds'):
                for k, a in v.items():
                    if k in of_cols:
                        st[(n, 'all_funds')][k] += a
    return st


def load_known_issues(book):
    """Mismatch records the extraction itself wrote to checks.json, normalised."""
    p = os.path.join(EX, book, 'checks.json')
    out = []
    if not os.path.exists(p):
        return out
    d = json.load(open(p))
    recs = d.get('mismatches')
    if recs is None:
        recs = [c for c in d.get('cross_checks', []) if c.get('status') == 'mismatch']
    for m in recs:
        fy = m.get('fiscal_year')
        basis = m.get('basis')
        key = m.get('key', '')
        if fy is None and key:
            mm = re.search(r'(\d{4}) (actual|adopted|proposed|estimate|projected)', key)
            if mm:
                fy, basis = mm.group(1), mm.group(2)
        diff = m.get('diff', m.get('difference'))
        try:
            diff = float(diff)
        except (TypeError, ValueError):
            diff = None
        if fy is None or diff is None:
            continue
        out.append({'fy': str(fy), 'basis': basis, 'diff': diff,
                    'text': norm(json.dumps(m)), 'kind': str(m.get('kind') or m.get('check') or '')})
    return out


def load_aliases():
    rows = read_csv(ALIASES)
    return rows


# ---------------------------------------------------------------- scoring
def classify(diff):
    return 'exact' if diff == 0 else ('w1' if abs(diff) <= 1 else 'miss')


def roll_up(book, amap, sections, gfs, stmt):
    """amap: dept_name -> (dept_id, sub_unit) for department_detail names."""
    sd = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(int)))
    stmt_names = collections.defaultdict(set)
    for (n, fg), v in stmt.items():
        d = stmt_dept(book, n)
        for k, a in v.items():
            sd[d][fg][k] += a
        stmt_names[d].add(n)
    dd = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(int)))
    dg = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(int)))
    secs = collections.defaultdict(set)
    for name, tabs in sections.items():
        d = amap[name][0]
        nested = amap[name][2]
        secs[d].add(name)
        for t, ks in tabs.items():
            if nested and t in ('by_type', 'by_fund'):
                continue          # this section's by_type / by_fund pages are already inside its parent's
            for k, a in ks.items():
                dd[d][t][k] += a
        for t, ks in gfs.get(name, {}).items():
            if nested and t == 'by_fund':
                continue
            for k, a in ks.items():
                dg[d][t][k] += a
    return sd, stmt_names, dd, dg, secs


def explain(book, d, col, diff, secs, sections, amap, known, stmt_names, ref_note):
    fy, basis = col
    names = [norm(n) for n in secs[d]] + [norm(n) for n in stmt_names.get(d, [])]
    cands = [m for m in known if m['fy'] == fy and m['basis'] == basis
             and any(n and n in m['text'] for n in names)]
    for m in cands:
        if abs(abs(m['diff']) - abs(diff)) <= 1:
            return 'KNOWN checks.json: %s (diff %+.0f)' % (m['kind'][:60], m['diff'])
    if len(cands) > 1 and abs(abs(sum(m['diff'] for m in cands if 'all' in m['kind'] or 'statement' in m['kind'])) - abs(diff)) <= 1:
        return 'KNOWN checks.json (sum of %d records)' % len(cands)
    if (book, d) in MANUAL:
        return 'MANUAL ' + MANUAL[(book, d)]
    if ref_note:
        return ref_note
    # size-of-another-section test
    tots = {}
    for name, tabs in sections.items():
        for t in ('by_type', 'by_program', 'by_fund'):
            if col in tabs.get(t, {}):
                tots[name] = tabs[t][col]
                break
    cl = [(n, v) for n, v in tots.items() if v and amap[n][0] != d]
    for n, v in cl:
        if abs(abs(v) - abs(diff)) <= 1:
            return 'SUSPECT: |miss| = size of section %r (mapped to %s)' % (n, amap[n][0])
    for (n1, v1), (n2, v2) in itertools.combinations(cl, 2):
        if abs(abs(v1 + v2) - abs(diff)) <= 1:
            return 'SUSPECT: |miss| = size of %r + %r' % (n1, n2)
    return None


def score_book(book, amap, verbose=False, tree=False):
    sections, gfs = load_sections(book)
    stmt = load_statement(book)
    known = load_known_issues(book)
    sd, stmt_names, dd, dg, secs = roll_up(book, amap, sections, gfs, stmt)
    result = {}
    unresolved = []
    for d in sorted(set(sd) | set(dd), key=str):
        cols = sorted({k for t in dd[d].values() for k in t})
        if not cols:
            amt = sd[d].get('all_funds', {})
            nz = {k: v for k, v in amt.items() if v}
            result[d] = {'status': 'missing-section' if nz else 'zero-no-section', 'amt': nz}
            continue
        if 'all_funds' not in sd[d]:
            result[d] = {'status': 'no-statement-line'}
            continue
        af = sd[d]['all_funds']
        gfv = sd[d].get('general_fund', {})
        ofv = sd[d].get('other_funds', {})
        cells, worst = [], 'exact'
        for col in cols:
            if col not in af:
                continue
            m = {t: dd[d][t][col] for t in METHODS if col in dd[d].get(t, {})}
            t, v = min(m.items(), key=lambda kv: abs(kv[1] - af[col]))
            diff = v - af[col]
            st, ref_note, how = classify(diff), None, t
            if st == 'miss' and col in gfv and col in ofv:
                alt = gfv[col] + ofv[col]
                tt, vv = min(m.items(), key=lambda kv: abs(kv[1] - alt))
                if abs(vv - alt) <= 1:
                    st = classify(vv - alt)
                    ref_note = ('STATEMENT-INCONSISTENT: book prints all-funds %d but GF+OF %d; sections match GF+OF'
                                % (af[col], alt))
                    how = tt + '~GF+OF'
                    diff = vv - alt
            if st == 'miss' or ref_note:
                ex = explain(book, d, col, diff, secs, sections, amap, known, stmt_names, ref_note)
            else:
                ex = None
            cells.append({'col': col, 'st': st, 'diff': diff, 'method': how, 'stmt': af[col], 'expl': ex,
                          'all': m})
            if st == 'miss':
                worst = 'miss'
            elif st == 'w1' and worst == 'exact':
                worst = 'w1'
        gcells = []
        for col in cols:
            if col in gfv:
                cand = {t: dg[d][t][col] for t in ('by_fund', 'dept_summary_by_fund') if col in dg[d].get(t, {})}
                if cand:
                    t, v = min(cand.items(), key=lambda kv: abs(kv[1] - gfv[col]))
                    gd = v - gfv[col]
                    gst = classify(gd)
                    gex = explain(book, d, col, gd, secs, sections, amap, known, stmt_names, None) if gst == 'miss' else None
                    if gst == 'miss' and gex and gex.startswith(('KNOWN', 'MANUAL')):
                        gst = 'miss-explained'
                    elif gst == 'miss' and col in af and abs(af[col] - dd[d]['by_type'].get(col, af[col])) > 1 and False:
                        pass
                    gcells.append({'col': col, 'st': gst, 'diff': gd, 'method': t, 'expl': gex})
        result[d] = {'status': worst, 'cells': cells, 'gf': gcells}
        for c in cells:
            if c['st'] == 'miss' and not (c['expl'] or '').startswith(('KNOWN', 'STATEMENT', 'MANUAL')):
                unresolved.append((d, c))
    return result, unresolved


def summarise(book, result):
    c = collections.Counter()
    for d, r in result.items():
        s = r['status']
        if s == 'miss':
            allexp = all(x['expl'] and x['expl'].startswith(('KNOWN', 'STATEMENT', 'MANUAL')) for x in r['cells'] if x['st'] == 'miss')
            s = 'miss-explained' if allexp else 'miss-unresolved'
        c[s] += 1
    gc = collections.Counter(x['st'] for r in result.values() for x in r.get('gf', []))
    return c, gc


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', nargs='*')
    ap.add_argument('-v', '--verbose', action='store_true', help='list every department, not only misses')
    args = ap.parse_args()
    fatal = []

    dept_ids = {r['dept_id'] for r in read_csv(DEPTS)}
    if not os.path.exists(ALIASES):
        print('FATAL: %s does not exist' % ALIASES)
        sys.exit(1)
    rows = load_aliases()
    need = ['book_id', 'source_file', 'printed_name', 'dept_id', 'sub_unit', 'note']
    if not rows or list(rows[0].keys()) != need:
        print('FATAL: columns must be', need)
        sys.exit(1)

    # ---- coverage
    keyed = collections.defaultdict(list)
    for r in rows:
        keyed[(r['book_id'], r['source_file'], r['printed_name'])].append(r)
    cov = collections.Counter()
    present = set()
    all_books = books()
    for b in all_books:
        for fn, names in load_names(b).items():
            for n in names:
                present.add((b, fn, n))
    dup = [k for k, v in keyed.items() if len(v) > 1]
    missing = sorted(present - set(keyed))
    extra = sorted(set(keyed) - present)
    for k in dup:
        fatal.append('duplicate alias row: %r (%d rows)' % (k, len(keyed[k])))
    for k in missing:
        fatal.append('UNCOVERED: %r' % (k,))
    for k in extra:
        fatal.append('alias row for a name that does not occur: %r' % (k,))
    blank = [r for r in rows if not r['dept_id'] and r['sub_unit'] != 'citywide']
    for r in blank:
        fatal.append('blank dept_id: %r' % ((r['book_id'], r['source_file'], r['printed_name']),))
    bad_file = [r for r in rows if r['source_file'] not in FILES]
    for r in bad_file:
        fatal.append('bad source_file: %r' % (r,))
    unknown_ids = collections.defaultdict(list)
    for r in rows:
        if r['dept_id'] and r['dept_id'] not in dept_ids:
            unknown_ids[r['dept_id']].append(r['book_id'])

    print('== ALIAS ROWS ==')
    byfile = collections.Counter(r['source_file'] for r in rows)
    print('rows by file:', dict(byfile), ' total', len(rows))
    print('(book, file, name) pairs in data: %d   covered: %d   uncovered: %d   extra: %d   duplicate: %d'
          % (len(present), len(present & set(keyed)), len(missing), len(extra), len(dup)))
    if unknown_ids:
        print('dept_ids NOT in data/crosswalk/departments.csv (proposed additions):')
        for k, v in sorted(unknown_ids.items()):
            print('   %-28s %d rows, books %s..%s' % (k, len(v), min(v)[:4], max(v)[:4]))
    subs = sorted({r['sub_unit'] for r in rows if r['sub_unit']})
    print('sub_unit labels (%d):' % len(subs), ', '.join(subs))

    # ---- roll-up
    print('\n== ROLL-UP vs CITYWIDE STATEMENT (department_detail sections -> dept_id) ==')
    tot_c = collections.Counter()
    all_unres = []
    for b in (args.book or all_books):
        amap = {r['printed_name']: (r['dept_id'], r['sub_unit'], r['note'].startswith('NESTED'))
                for r in rows if r['book_id'] == b and r['source_file'] == 'department_detail'}
        try:
            result, unres = score_book(b, amap)
        except KeyError as e:
            print(b, 'cannot score (unmapped section %s)' % e)
            continue
        c, gc = summarise(b, result)
        tot_c.update(c)
        print('%-24s exact %2d | within$1 %d | miss: %d explained, %d unresolved | no-statement-line %d | '
              'statement-line-without-section %d | GF cells %d exact/%d w1/%d miss-explained/%d miss-unexplained'
              % (b, c['exact'], c['w1'], c['miss-explained'], c['miss-unresolved'], c['no-statement-line'],
                 c['missing-section'], gc['exact'], gc['w1'], gc['miss-explained'], gc['miss']))
        for d, r in sorted(result.items(), key=lambda x: str(x[0])):
            s = r['status']
            show = args.verbose or s in ('miss', 'w1', 'missing-section') or any(x['st'] in ('miss', 'miss-explained') for x in r.get('gf', []))
            if not show:
                continue
            print('    %-28s %s' % (d, s))
            if s == 'missing-section':
                print('        statement all-funds non-zero, no section in book:',
                      {'%s %s' % k: v for k, v in r['amt'].items()})
            for x in r.get('cells', []):
                if x['st'] in ('miss', 'w1') or (args.verbose and x['expl']):
                    print('        %s %s: %s diff %+d (%s) stmt %d  %s'
                          % (x['col'][0], x['col'][1], x['st'], x['diff'], x['method'], x['stmt'], x['expl'] or ''))
            for x in r.get('gf', []):
                if x['st'] in ('miss', 'miss-explained'):
                    print('        GF %s %s: %s diff %+d (%s) %s' % (x['col'][0], x['col'][1], x['st'], x['diff'], x['method'], x.get('expl') or ''))
        all_unres += [(b, d, c_) for d, c_ in unres]
    print('\nTOTAL departments-books:', dict(tot_c))
    print('UNRESOLVED misses (cells):', len(all_unres))
    for b, d, c_ in all_unres:
        print('   %s %s %s %s diff %+d' % (b, d, c_['col'][0], c_['col'][1], c_['diff']))

    if fatal:
        print('\nFATAL coverage problems (%d):' % len(fatal))
        for f in fatal[:200]:
            print('   ', f)
        sys.exit(1)
    print('\ncoverage OK')


if __name__ == '__main__':
    main()
