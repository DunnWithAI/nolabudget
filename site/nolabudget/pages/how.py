# ABOUTME: /how/ : how this site was built with Claude Code, prompt by prompt.
# ABOUTME: The words are in site/content/how.yml; the book count comes from the data.
from ..evidence import Evidence


def build(site):
    text = site.content["how"]
    n_prompts = sum(len(p["steps"]) for p in text["parts"])
    site.page("/how/", "how.html", "How this site was built", text=text,
              n_prompts=n_prompts, n_books=len(Evidence().books))
