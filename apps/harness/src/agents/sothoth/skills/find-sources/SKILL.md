---
name: find-sources
description: Find the web pages that answer a research question. Read this when you were given no URL, or the pages you have don't answer the question.
metadata:
  codename: Divination
  include_tools: web_search exa_search
---

# Find sources

## With a URL

Open the page with `browser_navigate` first. Search only when it doesn't answer the question, or to find a primary source it mentions without linking.

## Which search

Use `web_search` when the question names something specific:

- an exact product, version, error message, person or company
- news or recent events, such as "latest", "this week" or a date
- a fact or a number, such as when something was released or what it costs
- the official documentation of something named

Use `exa_search` when the question describes something without naming it, and set its category:

- papers on a topic: publication
- companies building something: company
- people who work on something: people
- an overview of a field, or pages like one you already have: no category

For broad research, use both: `exa_search` to map the field, then `web_search` for recent news on what you found.

When a search finds little, try a few differently worded searches. When a search fails, try the other one.

## Using the results

Each result has its title, its URL, and either its page text or only a snippet.

- Cite page text directly.
- Never cite a snippet. Open the page with `browser_navigate` first.
- When page text was cut and the missing part could matter, open the page.
