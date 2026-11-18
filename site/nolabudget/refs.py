# ABOUTME: Per-page numbered list of sources and notes. Macros register each citation or
# ABOUTME: note as they render; base.html prints the list as "Notes and sources" at the end.
"""One numbered system per page, Wikipedia-style.

`m.cite`, `m.source`, `m.refs` and `m.footnotes` (templates/partials/macros.html) call
`pagerefs.source(c)` or `pagerefs.note(n)` and print a small "[n]" marker that links to
`#src-n`. The same book page (same text and link), or the same note, gets the same
number everywhere on the page; numbers follow first appearance. `base.html` renders the
list after the page body (Jinja renders the child's blocks before base.html reaches the
end of <main>, so the list is complete by then). `Site.page()` clears it after each page.
"""
from markupsafe import Markup


class PageRefs(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.items = []    # dicts: n, kind ("source" or "note"), text, href, html, computed, uses
        self._by_key = {}

    def _add(self, key, item):
        """Register an entry; return it with `first` set when this is its first marker."""
        it = self._by_key.get(key)
        if it is None:
            it = dict(item, n=len(self.items) + 1, uses=0)
            self.items.append(it)
            self._by_key[key] = it
        it["uses"] += 1
        return {"n": it["n"], "first": it["uses"] == 1, "item": it}

    def source(self, c):
        """A citation dict from Budget.cite(): {text, href, computed, ...}."""
        return self._add(("source", c["text"], c.get("href")), {
            "kind": "source", "text": c["text"], "href": c.get("href"),
            "computed": bool(c.get("computed")),
        })

    def note(self, n):
        """A comparability note from Site.notes (content/notes.yml): {id, html, ...}."""
        return self._add(("note", n["id"]), {
            "kind": "note", "id": n["id"], "html": n["html"],
            "text": Markup(n["html"]).striptags(),
        })
