# The website

The source of https://0xdaniell.github.io/macbox/, built by `build.py` (Python's standard library,
nothing to install) and published to GitHub Pages by `.github/workflows/pages.yml` on every push
to main that touches it.

```sh
python3 docs/site/build.py           # build into docs/site/_site and check it
python3 docs/site/build.py --serve   # the same, then http://127.0.0.1:8000/macbox/
```

| | |
|---|---|
| `pages/` | one HTML fragment per page (the home page and the 404). A header of `key: value` lines, then `---`. The file's path is the URL |
| `layout.html`, `style.css` | the frame around every page; the CSS is inlined |
| `static/` | copied as is: the favicon, the IndexNow key, search engines' verification files, and the home page's pictures in `static/images/`: `desk-*` is a real, uncropped capture of the macOS VM's whole screen (1600x1200: Simulator.app running Nimbus, a demo weather app made for these pictures, next to its website in Safari), saved as `-quality 84 desk-1600.webp`, `-resize 800x -quality 82 desk-800.webp` and `-quality 84 -interlace JPEG desk-1600.jpg`; the favicon is `docs/images/logo.svg`. The logos on the home page are inline SVG: Claude's from Simple Icons, OpenAI's and T3 Code's from T3 Code's repository) |

The site is one page on purpose: what macbox is, the picture, and how to set it up. Everything
else lives in the repo's docs. The logo, the social preview and `llms.txt` are copied from the repo
at build time (`docs/images`, `docs/site/llms.txt`), not kept twice.

**The build fails on:** a broken internal link or `#anchor`, a missing or duplicate title or
description, a page without exactly one `<h1>`, and a `macbox <command>` or `mac-sim <command>` in
a code sample that the CLI doesn't have.

Every page also gets a Markdown copy (`index.md` beside `index.html`) for language models, as
llmstxt.org suggests. robots.txt can't be set here: crawlers read it only at the host's root
(`0xdaniell.github.io/robots.txt`), which this repo doesn't control. Pages carry robots meta tags.
