# Sample outputs

Generated from the fixture payload in `tests/conftest.py` (a three-question
analyst RFI with two source documents), so engineering can see the shape of each
format without an API key.

| File | Format |
|---|---|
| `sample-dashboard.html` | Interactive HTML dashboard — open in a browser, click the tabs |
| `sample-qa.html` | Q&A response document — linear, printable |
| `sample-docx.docx` | Word |
| `sample-pptx.pptx` | PowerPoint |
| `sample-xlsx.xlsx` | Excel — `Summary` and `Responses` sheets |

These were built with `EMBED_FONTS_IN_HTML=false` to keep them small. In normal
operation the HTML outputs base64-embed the Neue Haas Grotesk fonts (~600 KB) so
a downloaded file stays on-brand anywhere.
