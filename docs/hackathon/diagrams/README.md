# The submission's diagrams

Two Mermaid diagrams, checked in as source and as SVG:

| Source | SVG | What it shows | Embedded in |
|---|---|---|---|
| [architecture.mmd](architecture.mmd) | [architecture.svg](architecture.svg) | the system: both Regions, the worker beside the imagery, Sentinel-2, Overpass, the agent and its news search with its fallback, the rotating topic, the MCP server and Alexa+, COOL as benchmarked, not deployed | [the technical report](../technical-report.md#3-architecture) §3; [the vision pipeline page](../../architecture/blogger-vision.md#part-one-the-pipeline) |
| [agent-workflow.mmd](agent-workflow.mmd) | [agent-workflow.svg](agent-workflow.svg) | the agent's workflow: perception, the gates, the bounded loop and its four tools, every fail-closed edge, the Finding, the day's city and the person | the same two pages |

The Markdown pages embed the same text in a `mermaid` block, which GitHub renders;
`scripts/tests/test_hackathon_docs.py` checks that each embedded block is exactly its `.mmd` file,
that each `.mmd` has its SVG, and that the diagrams name what the rubric asks for. The README's own
architecture diagram carries a vision part drawn from the first. The trace is not a third diagram:
it is `vision_decisions.trace`'s table, the same on every reader.

## Making the SVGs

After any change to a `.mmd` file, export both again and commit them with it. The tool is Mermaid's
command-line interface, pinned to an exact version: **`@mermaid-js/mermaid-cli@12.0.0`** (it
resolved `mermaid` 12.2.0 and `puppeteer` 25.14.0 when the committed SVGs were made, on 16 October,
with Node 22.22.0). It is a writing tool run by hand, not a dependency of anything that builds,
deploys or tests. From the repository root, with Node 22.13 or newer:

```bash
for d in architecture agent-workflow; do
  npx -y @mermaid-js/mermaid-cli@12.0.0 --no-font-embed -c docs/hackathon/diagrams/mermaid-config.json \
    -i docs/hackathon/diagrams/$d.mmd -o docs/hackathon/diagrams/$d.svg
done
```

```powershell
foreach ($d in "architecture", "agent-workflow") {
  npx -y @mermaid-js/mermaid-cli@12.0.0 --no-font-embed -c docs\hackathon\diagrams\mermaid-config.json `
    -i "docs\hackathon\diagrams\$d.mmd" -o "docs\hackathon\diagrams\$d.svg"
}
```

`npx` downloads the tool and a Chromium for it the first time. Where a Chromium is already
installed and the download is not wanted, install with `PUPPETEER_SKIP_DOWNLOAD=1` and point the
tool at the browser with a Puppeteer configuration file, `-p puppeteer.json`, holding
`{"executablePath": "<the browser>", "args": ["--no-sandbox"]}`.

[mermaid-config.json](mermaid-config.json) sets Arial (Helvetica, else the viewer's sans-serif) and
plain SVG text for the labels, rather than the tool's default embedded web font and HTML labels, so
an SVG is small, holds no font file, and opens in an image viewer or a video editor as well as in a
browser. The SVGs are vectors: for the video, zoom into the part being named rather than exporting
crops. Many video editors rasterise an imported SVG at its own size (`architecture.svg` is about
2,840 px wide), which leaves a 3 times zoom with fewer source pixels than a 1080p frame; for those,
import an 8,000 px PNG instead, made by the same command with `--size 8000` and a `.png` output
(`-o architecture.png`), and zoom into that. The PNGs are working files for the video, not
committed.
