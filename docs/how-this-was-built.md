# How nolabudget.dunn.us was built

A plain account of how this site went from one prompt to a live, public site, for anyone
who wants to do the same thing. Andrew Dunn built it with Claude Code in two sessions
between 14 and 17 November 2026:

- **Part 1, the first build** (14 to 17 November): find every budget book, pull the
  tables out, check them, and build a working site.
- **Part 2, making it clear and public** (17 November, about 8:40 to 11:40 a.m.): plainer
  words and pictures, more depth and context, a source on every figure, then going public.

Live site: https://nolabudget.dunn.us/

A shorter version is on the site at `/how/`; its text is in `site/content/how.yml`. Keep
the two in step.

## The short version

1. One long opening prompt set the goal, the source and the working style.
2. Claude found and downloaded every budget it could, then came back with a plan.
3. Short go-aheads moved it through the stages: "Run the pilot", "Extract the rest",
   "Build it", "Yes, polish it".
4. Andrew reviewed the site, sent screenshots of what looked wrong, and asked questions.
5. A second session set a writing standard, raised the design, added depth (police
   programs, cost per resident, comparisons with other cities) and made every figure show
   its source.
6. It went public, with visit counts and search tools set up.

Andrew typed about 25 messages in all. Most of the steering was in a few longer prompts;
the rest were short go-aheads and questions.

The prompts below are quoted word for word, typos included, with two exceptions. Prompt 1
is reworded so it stands on its own (the original pointed to another site as the model to
follow). Text in square brackets replaces a personal detail.

---

## Part 1: The first build

### 1. The opening prompt

> I'd like to build a website that makes the City of New Orleans budget easy to understand. The goal is something clear that works as a civic resource: simple to understand, easy to drill down into, and easy to see changes over time.
>
> To start, we need to pull down every budget we can find. The city's site goes back to 2011, but if we can go back further, great!
>
> Then we need to think of the most compelling ways to see the budget: how it's shifted over time, how resources are being used, etc.
>
> We should spin up right-sized subagents for everything. The main session can plan and verify things, but we should use smaller models wherever we can.
>
> https://nola.gov/next/mayors-office/topics/budget/

What this prompt does well:

- **States the purpose, not the features.** "A civic resource", "clear and simple",
  "drill down", "changes over time". Claude chose the pages; Andrew chose what they are for.
- **Orders the work.** Get the data first, then think about how to show it.
- **Names the source.** One URL to start from.
- **Sets the working style.** The main session plans and checks; smaller, cheaper models
  do the bulk work.

### 2. "Run the pilot"

After the downloads, Claude proposed testing the extraction on three books before doing
all of them. This was the go-ahead.

### 3. "Extract the rest"

The pilot worked, so the remaining books were extracted in parallel.

### 4. "Build it"

Go-ahead to turn the combined data into a website.

### 5. The stack file

> Here's my stack.md. We'll put this up on cloudflare maybe nolabudget.dunn.us
>
> Let's build and review locally first though.

Andrew keeps a `stack.md` file listing his preferred hosting, naming and tools, so those
choices are not made again for every project. Sharing it settled hosting in one message.

### 6. "Yes, polish it"

Go-ahead for a pass on plain wording and phone layout.

### 7. A bug report, with screenshots

> Seeing a lot of pages like this. What are so many setions showing $0 like this

Two screenshots showed two separate problems: a list printing one letter per bullet (a
bug), and tables full of $0 (real data: some budget books print empty form pages for
small bodies).

### 8. Getting it published

> Where can I view the current site?
> Should we just get it published to nolabudget.dunn.us for me to review and such?

Then a few messages sorting out deployment, ending with:

> Yes, install Node

### What happened at each stage

**Find and download everything.** Four subagents ran at once:

| Job | Model |
|---|---|
| Download every budget file from nola.gov | Haiku (the smallest) |
| Explore the city's online budget viewer (Questica OpenBook) | Sonnet |
| Look for budgets from before 2011 | Sonnet |
| Look for structured data (open data portal, etc.) | Sonnet |

Result: adopted budget books back to 2008, plus recent proposed budgets. The 2025 to 2027
budgets exist only in the city's online viewer, not as PDFs.

**The pilot.** Three books chosen because they differ: 2012 (older PDF layout), 2019
(newer PDF layout) and 2026 (online viewer, no PDF). One Sonnet subagent per book wrote an
extraction script and produced tables in a shared format. If the approach fails, it fails
on three books, not twenty.

**Extract the rest.** Seven Sonnet subagents ran in parallel, each taking two or three
books, each writing its own script because the layouts change from year to year. The 2008
book is a scan and needed text recognition. The main session then checked every book
itself: do the department rows add up to the printed totals, and does a random sample of
amounts appear on the page it cites? (`scripts/verify_extracted.py`.) Result: 21 books in
`data/extracted/`, one folder per book.

**Make the years comparable.** Department and income names change between books
("Criminal Sheriff" and "Orleans Parish Sheriff" are the same body). Three Sonnet
subagents built lookup tables (`data/crosswalk/`) mapping every printed name to one
stable name. `scripts/build_combined.py` merges all books and records whether books
agree when several print a figure for the same year.

**Build the site.**

| Job | Model |
|---|---|
| Site foundation: build script, templates, charts, department pages | Opus |
| Revenue pages | Sonnet |
| Year pages and "what changed" pages | Sonnet |
| "Not explained" page | Sonnet |

The foundation went to a larger model because every other page depends on it.

**Polish and review.** Budget jargon replaced with plain words, with the book's own term
underneath ("Personal Services" becomes "Pay and benefits"). Tables reworked for phones.
Bugs fixed from Andrew's screenshots.

**Deploy.** `wrangler deploy` uploads the built `dist` folder to Cloudflare and attaches
the domain, configured in `wrangler.jsonc`. The first deploy was blocked from search
engines so the site could be reviewed privately.

---

## Part 2: Making it clear, deep and public

This session started from a correct but plain site. Most of the work was making it
easier to read, adding context, and making every claim checkable.

### 9. Documentation, and say it simply

*8:37 a.m., with Andrew's writing guide (`VOICE.md`) attached*

> Great first pass!
>
> First make sure everything in this repo/folder is well documented this and future claude sessions.
>
> Second, I think it would be good it would explain things more visually and simply.
>
> For example:
>
> The proposed 2027 budget is $X billion. That is split between the general fund and [whatever the other thing is]. The general fund is proposed by the mayor and approved and adopted by the city council (or however it works).
>
> Doesn't have to be that exactl language, just make it clearer. Here's  my voice file. Make sure we're using simple, helpful langauge. We want this to be accessible. Show when you can, charges, graphs, other visiuals are super useful.

- Wrote a README, a guide for future Claude sessions (`CLAUDE.md`) and a data-pipeline
  guide, and copied the writing guide into the project.
- Rebuilt the home page around the example: "$1.47 billion", a bar splitting the General
  Fund from other funds, the four steps of the budget year with "We are here", and "out of
  every $100" bars.

**Why it worked:** one example sentence showed the tone better than any instruction, and
the writing guide made "simple" specific.

### 10. Push for better design

*8:47 a.m.*

> Is this your best work? Look at https://www.usehallmark.com/ and such (I think it's installed) and research the best UI/UX for this kind of site. Let's see if we can take it up a notce. Think nice, delightful animations, mouseovers, etc.

Claude studied Hallmark's pages and added a "where every $100 goes" receipt, a count-up
headline, bars that grow in as you scroll, hover tooltips and charts that draw themselves.
Animation switches off for readers who ask for less motion, and every page still reads
without JavaScript.

**Why it worked:** "Is this your best work?" plus a reference to study gave permission
and a direction to raise the bar.

### 11. "Yes, do those next"

*8:55 a.m.* The same pictures went onto every department and revenue page: this year
against last year, "$22 of every $100", and split bars.

### 12. Ask for depth and context

*9:03 a.m.*

> Looks great.
>
> Do we get more detail for example into the police department's budget? Like how they spend the money? Any way we can go a level deeper?
>
> How can we contextualize the data better? I think showing changes overtime is good. But would also like to see maybe $ per citizen and maybe how that compares to other cities in Louisiana and throughout the world. We don't need line by line, but it would be helpful to have that context.

Claude checked the data before promising anything: the books break police spending into
programs (all eight districts, the crime lab, investigations) and job titles back to
2008. It recommended a standard dataset for comparing U.S. cities, and advised against
comparing with cities abroad, which run different services.

**Why it worked:** asking "any way we can…?" got an answer grounded in what the data can
support.

### 13. "Yes, in that order"

*9:05 a.m.* Cost per resident across the site, an "Inside the department" section for
police, and a "Who works there" breakdown of jobs by rank, each checked against the
totals. Then Claude stopped: the comparison dataset's terms did not allow republishing
its figures.

### 14. "Use Census data"

*9:47 a.m.* Claude used the Census Bureau's 2022 Census of Governments, which is public
domain, and compared New Orleans with every U.S. city of 250,000 to 750,000 people plus
Louisiana's larger cities, chosen by rule rather than by hand. Cities where another
government runs a service are left out, with the reason on the page.

### 15. Expand

*10:15 a.m.*

> Merge when ready and I think we should expand the comparison beyond police.

Seven services, each checked against New Orleans's own budget books first. Housing and
courts were left out because the Census figures did not match the books.

### 16. "Yes, deploy"

*10:38 a.m.*

### 17. Clean up, and hand off a side quest

*10:48 a.m.*

> Let's clean up the footer. link my name do dunn.us.
>
> Then I think we just do a simple "About this site"
>
> And then should we do anything else to the footer for SEO purposes?
>
> Also spin up a subagent on SEO/AEO. Just make sure we have good hygiene.

A simpler footer. A subagent working in parallel added canonical links, social tags,
structured data, a sitemap, `robots.txt` and an `llms.txt`, with build checks for all of
it.

### 18. Catch leftovers

*Sent while Claude was working, with a screenshot*

> Get rid of this on the about page.
>
> Also is there any other placeholder data?

The unfinished "Who made it" section came out, and a search found one more placeholder.

### 19. Question the claims

*10:53 a.m.*

> nolabudget@dunn.us
>
> also does every figure really link to its source? because it doesn't appear that way […]

The honest answer was no. Claude gave every section with a figure a source, then made the
build fail if any section shows a figure without one.

**Why it worked:** checking a promise the site made turned it into a rule the build
enforces.

### 20. "Yes, deploy"

*11:14 a.m.*

### 21. Go public

*Sent while Claude was working*

> Go ahead and do any social sharing stuff and go public.

A sharing image with no figures on it, so it never goes stale; the "don't index" settings
switched off; deployed and checked live.

### 22. Set up the accounts

*11:32 a.m.*

> can you just open my browser and get this setup in umami, GSC, and openseo. Make sure you're using my personal accounts. umami is [my own Umami server].
>
> Let me know if you need logging into anything

- **Umami:** added the site and its tracking script, and confirmed a visit was recorded.
- **Google Search Console:** noticed it had opened in a work account and switched to the
  personal one, added the site, submitted the sitemap (169 pages found) and requested
  indexing.
- **OpenSEO:** created a project linked to that Search Console property.

### 23. Share the process

> yo - can you like output all of my prompts i used to amke this? or the process? like to help other learns how I built it?

Which produced this document, combined with the write-up from Part 1.

---

## Decisions Andrew made

| Decision | Why it mattered |
|---|---|
| Data first, presentation second | The pages were designed around what the books contain |
| Go back as far as possible | The site covers 2007 to 2027, not 2011 onward |
| Small models for bulk work, a large one to plan and verify | 24 of 26 first-build subagent jobs ran on Sonnet, one on Haiku, one on Opus |
| Approve a pilot before the full extraction | Caught format problems on three books |
| Keep a `stack.md` | Hosting, domain and naming were decided in one message |
| Review before going public | Bugs and placeholders were found before anyone else saw the site |
| Report problems with screenshots | Faster and clearer than describing them |
| Write a voice guide and give one example sentence | "Simple" became specific |
| Use public Census data for comparisons | Figures can be republished freely |
| Ask whether the site keeps its promises | "Every figure shows its source" became a build rule |
| Name whose accounts to use | The wrong Google account was caught |

## Decisions Claude made (and Andrew can overrule)

- **Static site, no database.** A Python script reads the data and writes plain HTML.
  Nothing runs on a server. Charts are drawn into the page; animation is extra and
  never needed to read a page.
- **Every figure shows its source.** Each number links to the budget book and page it
  came from, and the build fails if a section shows a figure without a source.
- **Say what is not known.** A "Not explained" page lists figures the books print without
  explanation, and places where two books disagree.
- **Never guess.** Where a label's meaning could not be confirmed (for example
  "Off-Budget"), it is shown as printed. No reason for a change is given unless the
  budget book states it.
- **Plain language.** Written for a general reader, with the book's own terms kept beside
  the plain ones so they can still be looked up.
- **Keep Census figures apart.** Comparison figures are a different year and use different
  definitions from the budget, so they are labelled every time.
- **Don't compare abroad.** Cities in other countries run different services, so the
  comparison stays within the U.S.

## What went wrong, and what caught it

- **A list printed one letter per bullet** on many department pages. Internal working
  notes were being treated as reader text. Found by Andrew from a screenshot.
- **Tables of $0.** Not an extraction bug: several books print empty form pages for small
  bodies. The pages now say so.
- **One book mislabels a column.** The 2018 book heads a column "Adopted" when the
  figures are actuals. Corrected, with the reason recorded in
  `data/crosswalk/corrections.csv`.
- **Deployment took several tries.** The Mac had no Node or Wrangler, and the Cloudflare
  login timed out once.
- **Dropbox** created conflicted copies of files during builds.
- **A comparison dataset's terms didn't allow republishing.** Claude stopped and asked;
  public Census data replaced it.
- **Census housing and courts figures didn't match the city's books**, so those were
  left out.
- **A state-code mix-up** in the Census script was caught by reading the file's
  documentation before running it.
- **One edit emptied a code file.** It was rewritten and the build checks confirmed it.
- **Browsers kept an old stylesheet.** File links now carry a fingerprint that forces a
  fresh copy.
- **Not every figure showed its source**, though the site said so. Caught by Andrew's
  question; now enforced by the build.

## How the project is laid out

```
data/raw/         the downloaded budget books and Census files
data/extracted/   one folder of tables per book (21 books)
data/crosswalk/   lookup tables that make names consistent across years
data/combined/    the merged tables the site is built from
scripts/          extraction, checking, merging and comparison scripts
site/             templates, page code, written content, styles
dist/             the built site (169 pages)
docs/             notes on how and why things work
VOICE.md          the writing guide
wrangler.jsonc    Cloudflare deploy settings
```

To rebuild and redeploy:

```bash
.venv/bin/python site/build.py
```

```bash
wrangler deploy
```

## Still open

- Department descriptions are drafts and have not been reviewed.
- A few statements rest on web search and not on the budget data.
- Capital budgets and audited financial reports were collected but are not on the site.
- "Inside the department" covers police only; fire and sanitation could follow.
- Not yet tested on a real phone or with a screen reader.
- The project is not in git.

## If you want to do this yourself

1. **Start with a goal and a purpose**, not a feature list.
2. **Ask for the data first.** Let the tool show you what exists before deciding what to
   build.
3. **Ask for a pilot** on a small, varied sample before the full run.
4. **Have the work checked** against the source, by something other than the thing that
   did the work.
5. **Move in stages** with a short go-ahead at each one. Long prompts are only needed at
   the turning points.
6. **Set standards in writing.** A voice guide and one example sentence beat "make it
   clearer".
7. **Ask for more.** "Is this your best work?" with a reference to study raises the bar.
8. **Ask questions, not only orders.** "Any way we can go a level deeper?" gets a plan
   based on what is possible.
9. **Check the claims.** Ask whether the thing really does what it says; then make the
   answer a rule.
10. **Review the real thing** and send screenshots of whatever looks wrong.
11. **Write down your defaults** (hosting, naming, tools) once, and share the file.
