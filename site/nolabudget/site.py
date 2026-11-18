"""The Site object: content, data, templates and the page writer.

Page modules (site/nolabudget/pages/*.py) receive a Site and call `site.page()` once per
URL. Everything else (copying static files, CSV downloads, checks) is done by build.py.
"""
import hashlib
import os

import jinja2
import yaml
from markupsafe import Markup

from . import fmt
from .data import BASIS_LABEL, FUND_LABEL, Budget
from .refs import PageRefs

SITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SITE_DIR)
CONTENT = os.path.join(SITE_DIR, "content")
TEMPLATES = os.path.join(SITE_DIR, "templates")


def load_content():
    out = {}
    for name in sorted(os.listdir(CONTENT)):
        base, ext = os.path.splitext(name)
        if ext in (".yml", ".yaml"):
            with open(os.path.join(CONTENT, name), encoding="utf-8") as f:
                out[base] = yaml.safe_load(f)
    return out


def crumbs_ld(items, base_url):
    """The visible breadcrumb trail [(href, label), ...] as schema.org ListItems."""
    return [{"@type": "ListItem", "position": i, "name": str(label), "item": base_url + href}
            for i, (href, label) in enumerate(items, 1)]


def faq_ld(questions):
    """[(question, answer_html), ...] as schema.org Questions, answers as plain text.
    The page prints the same answer_html, so the two cannot drift apart."""
    return [{"@type": "Question", "name": str(q),
             "acceptedAnswer": {"@type": "Answer", "text": Markup(a).striptags()}}
            for q, a in questions]


class Site(object):
    def __init__(self, out_dir):
        self.out = out_dir
        self.content = load_content()
        self.config = self.content["site"]
        self.budget = Budget(self.content)
        self.pages = []          # (url, title) of every page written
        self.written = set()     # every file written this build (anything else in dist/ is pruned)
        self.facts = {}          # numbers pages publish, for checks.py: {url: {name: value}}
        self.nav = [
            ("/", "Home"), ("/departments/", "Departments"), ("/revenue/", "Revenue"),
            ("/budgets/", "Budgets by year"), ("/compare/", "Compare"), ("/about/", "About the data"),
        ]
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES),
            autoescape=jinja2.select_autoescape(["html"]),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True, lstrip_blocks=True,
        )
        env.filters.update(fmt.FILTERS)
        self.pagerefs = PageRefs()   # the page's numbered notes and sources; see refs.py
        env.globals.update(
            pagerefs=self.pagerefs,
            site=self.config, nav=self.nav, FUND_LABEL=FUND_LABEL, BASIS_LABEL=BASIS_LABEL,
            budget=self.budget, fmt=fmt, asset=self.asset, crumbs_ld=crumbs_ld, faq_ld=faq_ld,
        )
        self.env = env
        self.notes = self._render_notes()

    # ----------------------------------------------------------------- notes
    def _render_notes(self):
        b = self.budget
        gf = lambda y, basis: b.total(y, basis, "general_fund")
        facts = {
            "pension_2012": 195.9e6,
            "gf_actual_2012": gf(2012, "actual"),
            "gf_actual_2012_ex": gf(2012, "actual") - 195.9e6,
            "gap_2020": b.total(2020, "adopted", "all_funds") - b.printed_all_funds[(2020, "adopted")],
            "gf_adopted_2023": gf(2023, "adopted"),
            "gf_proposed_2023": gf(2023, "proposed"),
            "gf_2023_gap": gf(2023, "adopted") - gf(2023, "proposed"),
            "gap_2025": b.total(2025, "adopted", "all_funds") - gf(2025, "adopted") - b.total(2025, "adopted", "other_funds"),
        }
        notes = []
        for n in self.content["notes"]:
            n = dict(n)
            n["html"] = Markup(self.env.from_string(n["text"]).render(facts=facts, money=fmt.money, money_full=fmt.full))
            notes.append(n)
        self.note_facts = facts
        return notes

    def notes_for(self, years, funds, bases, dept=None):
        """Notes that apply to any of these years / funds / bases (and department, if given)."""
        years = set(int(y) for y in years)
        funds = set(funds)
        if "other_funds" in funds:
            funds.add("all_funds")
        out = []
        for n in self.notes:
            if not years & set(n["years"]):
                continue
            if not funds & set(n["funds"]) or not set(bases) & set(n["bases"]):
                continue
            if dept and n.get("depts") and dept not in n["depts"]:
                continue
            out.append(n)
        return out

    # ----------------------------------------------------------------- pages
    def asset(self, name):
        """URL for a file in static/ with a fingerprint of its contents, so browsers and
        caches fetch the new file whenever it changes: /static/style.css?v=1a2b3c4d."""
        path = os.path.join(os.path.dirname(TEMPLATES), "static", name)
        with open(path, "rb") as f:
            return "/static/%s?v=%s" % (name, hashlib.sha1(f.read()).hexdigest()[:8])

    def page(self, url, template, title, **ctx):
        """Render `template` to dist/<url>/index.html. `url` starts and ends with '/'."""
        assert url.startswith("/") and url.endswith("/"), url
        try:
            html = self.env.get_template(template).render(page_url=url, title=title, **ctx)
        finally:
            self.pagerefs.reset()
        path = os.path.join(self.out, url.strip("/"), "index.html")
        self.write(path, html.encode("utf-8"))
        self.pages.append((url, title))
        return path

    def write(self, path, data):
        """Write a file into dist/, only if its content changed. dist/ lives in a synced
        (Dropbox) folder, where deleting and recreating files makes "conflicted copies"."""
        self.written.add(os.path.abspath(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, "rb") as f:
                if f.read() == data:
                    return
        with open(path, "wb") as f:
            f.write(data)

    def publish(self, url, **numbers):
        """Record numbers a page shows, so checks.py can compare them with spending.csv."""
        self.facts.setdefault(url, {}).update(numbers)
