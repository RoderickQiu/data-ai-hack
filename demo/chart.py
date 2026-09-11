"""The three-line chart, built from the runs table and nothing else.

Cheaper is the easy half. Better, and needing you less, is the half that proves
this is a compound agent and not a cache — so the chart carries all three:

* **cost falling** — tokens per run, with the replay ratio behind it;
* **quality rising** — skip-class prediction accuracy, and precision@5;
* **human effort falling** — questions asked, and total human touches.

Points are coloured by ``mode``, so the step down at the first replay is visible
rather than narrated.

**Only numbers the system actually logged go on the chart.** Rows with a null
metric are drawn as gaps, never interpolated: a run that produced no skips has
no accuracy, and "no data" and "got them all wrong" are different facts.

Writes a self-contained HTML file with no CDN, because the demo is offline end
to end.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.config import DATA_DIR
from insight.store import Insight

MODE_COLOURS = {"first_run": "#e4572e", "partial_replay": "#f3a712", "full_replay": "#2e86ab"}


def series(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row.get("started_at") or "")
    return {
        "labels": [f"day {row.get('day')}" for row in ordered],
        "modes": [row.get("mode") or "first_run" for row in ordered],
        "tokens": [_num(row.get("tokens_in")) + _num(row.get("tokens_out")) for row in ordered],
        "wall_ms": [_num(row.get("wall_ms")) for row in ordered],
        "questions": [_num(row.get("questions_asked")) for row in ordered],
        "touches": [_num(row.get("human_touches")) for row in ordered],
        "accuracy": [_pct(row.get("prediction_accuracy")) for row in ordered],
        "precision": [_pct(row.get("precision_at_5")) for row in ordered],
        "replay_ratio": [_ratio(row) for row in ordered],
        "runs": len(ordered),
    }


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(value: Any) -> float | None:
    """``None`` stays ``None``. This is the honesty rule, in one function."""
    if value is None or value == "":
        return None
    try:
        return round(float(value) * 100, 1)
    except (TypeError, ValueError):
        return None


def _ratio(row: Mapping[str, Any]) -> float:
    replayed, reasoned = _num(row.get("steps_replayed")), _num(row.get("steps_reasoned"))
    total = replayed + reasoned
    return round(replayed / total, 3) if total else 0.0


def headline(data: Mapping[str, Any]) -> str:
    """The sentence the pitch ends on, with the session's real numbers in it."""
    tokens, questions, accuracy = data["tokens"], data["questions"], data["accuracy"]
    known = [value for value in accuracy if value is not None]
    if not tokens:
        return "No runs logged yet."
    first_acc = f"{known[0]:.0f}%" if known else "n/a"
    last_acc = f"{known[-1]:.0f}%" if known else "n/a"
    return (f"Run 1 it guessed your taste {first_acc} of the time and asked you "
            f"{int(questions[0])} questions, for {int(tokens[0])} tokens. "
            f"Run {data['runs']}: {last_acc}, {int(questions[-1])} questions, "
            f"{int(tokens[-1])} tokens.")


def render_html(data: Mapping[str, Any], title: str = "Memory to muscle memory") -> str:
    """One file, no CDN, no network. The venue wifi cannot break the closing slide."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 40px auto; max-width: 960px; color: #1d1d1f; }}
 h1 {{ font-size: 22px; margin-bottom: 4px; }}
 .headline {{ font-size: 17px; color: #444; margin-bottom: 28px; }}
 .chart {{ margin-bottom: 34px; }}
 .legend span {{ margin-right: 14px; font-size: 13px; }}
 .swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }}
 svg {{ width: 100%; height: 240px; background: #fbfbfd; border: 1px solid #e6e6eb; border-radius: 8px; }}
 .caption {{ font-size: 13px; color: #666; margin-top: 6px; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="headline">{headline(data)}</div>
<div class="legend">
  {"".join(f'<span><i class="swatch" style="background:{colour}"></i>{mode.replace("_", " ")}</span>' for mode, colour in MODE_COLOURS.items())}
</div>
<div id="charts"></div>
<script>
const data = {json.dumps(data)};
const colours = {json.dumps(MODE_COLOURS)};
const panels = [
  ["Cost — tokens per run", "tokens", false],
  ["Human effort — questions asked", "questions", false],
  ["Quality — skip-class prediction accuracy (%)", "accuracy", true],
];
const W = 900, H = 220, PAD = 42;
const root = document.getElementById("charts");
for (const [title, key, isPct] of panels) {{
  const values = data[key];
  const known = values.filter(v => v !== null);
  const max = isPct ? 100 : Math.max(1, ...known);
  const x = i => PAD + (values.length < 2 ? 0 : i * (W - 2 * PAD) / (values.length - 1));
  const y = v => H - PAD - (v / max) * (H - 2 * PAD);
  // Nulls break the line rather than being bridged: an absent metric is not a value.
  let path = "", pen = false;
  values.forEach((v, i) => {{
    if (v === null) {{ pen = false; return; }}
    path += (pen ? " L" : " M") + x(i) + " " + y(v); pen = true;
  }});
  const dots = values.map((v, i) => v === null ? "" :
    `<circle cx="${{x(i)}}" cy="${{y(v)}}" r="3.5" fill="${{colours[data.modes[i]] || "#888"}}"><title>${{data.labels[i]}} — ${{v}}</title></circle>`).join("");
  root.insertAdjacentHTML("beforeend", `
    <div class="chart"><strong>${{title}}</strong>
    <svg viewBox="0 0 ${{W}} ${{H}}">
      <line x1="${{PAD}}" y1="${{H - PAD}}" x2="${{W - PAD}}" y2="${{H - PAD}}" stroke="#d0d0d6"/>
      <line x1="${{PAD}}" y1="${{PAD}}" x2="${{PAD}}" y2="${{H - PAD}}" stroke="#d0d0d6"/>
      <text x="6" y="${{PAD + 4}}" font-size="11" fill="#888">${{Math.round(max)}}</text>
      <text x="6" y="${{H - PAD}}" font-size="11" fill="#888">0</text>
      <path d="${{path}}" fill="none" stroke="#8a8a93" stroke-width="1.6"/>
      ${{dots}}
    </svg>
    <div class="caption">${{values.length}} run(s). Points coloured by mode; gaps are runs where the metric had no data, never interpolated.</div>
    </div>`);
}}
</script>
</body></html>"""


def build(limit: int = 500, out: Path | None = None,
          insight: Insight | None = None) -> Path:
    rows = (insight or Insight()).run("runs_series", {"limit": limit})
    data = series(rows)
    target = out or DATA_DIR / "chart.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(data))
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the three-line chart.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", action="store_true", help="print the series instead")
    args = parser.parse_args()
    if args.json:
        print(json.dumps(series(Insight().run("runs_series", {"limit": args.limit})),
                         indent=2))
        return
    path = build(limit=args.limit, out=args.out)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
