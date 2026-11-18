"""Page types. Each module has build(site) and writes its pages with site.page().

To add a page type: write pages/<name>.py with build(site), its templates, and add the
module to PAGES below. Order matters only if a page reads something another sets
(about sets site.downloads, which build.py copies).
"""
from . import about, budgets, changes, departments, home, revenue

from . import compare, how, method, not_explained

PAGES = [home, departments, budgets, changes, revenue, compare, about, not_explained, method, how]
