# Project website

- English: https://lucky-goals.github.io/goals-iterm-mcp/
- 简体中文: https://lucky-goals.github.io/goals-iterm-mcp/zh/
- Sitemap: https://lucky-goals.github.io/goals-iterm-mcp/sitemap.xml

## Edit and publish

The website is plain HTML and CSS in `site/`; no build tools or JavaScript are required.
Edit both `site/index.html` and `site/zh/index.html` when changing project instructions.
Keep installation commands aligned with README versions. Shared styles are in
`site/assets/style.css`. Preserve the Google verification meta tag in both pages;
it is a public ownership proof, not a private credential.

Run `python3 scripts/check_site.py` before pushing. The Pages workflow checks PRs
and deploys `site/` after changes merge into `main`. Configure Settings → Pages →
Source as GitHub Actions. This workflow does not release the software package.

## Search Console

Use a URL-prefix property: `https://lucky-goals.github.io/goals-iterm-mcp/`. Verify using the HTML tag on the homepage,
submit `https://lucky-goals.github.io/goals-iterm-mcp/sitemap.xml`, and inspect/request indexing for the English and
Chinese homepages. Google determines if and when to index them. A successful
submission or deployment is not proof of indexing.

A project-level `robots.txt` is intentionally omitted: crawlers read robots.txt
only at the host root, `https://lucky-goals.github.io/robots.txt`. Control that
file in the user-site repository if one is created later; keep both projects crawlable.
