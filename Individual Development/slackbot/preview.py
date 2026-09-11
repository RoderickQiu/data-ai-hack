"""Print Block Kit Builder links for every surface, so the UI can be reviewed
before the Slack app exists.

    python -m slackbot.preview
"""

import json
import urllib.parse

from . import blocks as B, fixtures as F
from .schemas import AutonomyIn, ClaimsIn, DigestIn, PackIn, PreferenceIn, QuestionIn

BUILDER = "https://app.slack.com/block-kit-builder/#"


def link(blocks: list[dict]) -> str:
    return BUILDER + urllib.parse.quote(json.dumps({"blocks": blocks}))


def main() -> None:
    pack_cold = PackIn(**F.PACK_COLD)
    pack_warm = PackIn(**F.PACK_WARM)
    surfaces = {
        "digest, run 1 (first run, 6 questions, near-random predictions)": B.digest_message(DigestIn(**F.DIGEST_COLD)),
        "digest, run 19 (full replay, reasons, mixed slate)": B.digest_message(DigestIn(**F.DIGEST_WARM)),
        "apply pack, run 1 (asks 3 questions, flags a gap)": B.pack_message(pack_cold)[0],
        "apply pack, run 19 (asks nothing)": B.pack_message(pack_warm)[0],
        "gap warning (rendered as a red-barred attachment)": B.pack_message(pack_cold)[1][0]["blocks"],
        "preference hypothesis": B.preference_message(PreferenceIn(**F.PREFERENCE)),
        "autonomy request": B.autonomy_message(AutonomyIn(**F.AUTONOMY)),
        "claim verification": B.claims_message(ClaimsIn(**F.CLAIMS)),
        "single question": B.question_message(QuestionIn(**F.QUESTION)),
    }
    for name, blocks in surfaces.items():
        print(f"\n{name}\n{link(blocks)}")


if __name__ == "__main__":
    main()
