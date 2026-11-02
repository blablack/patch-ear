"""Write the list of patches that actually ship on the Bento (BENTO_PATCHES in prepare-sd-cards/bento.py).

Run with prepare-sd-cards' environment, from that repo:
    cd ../prepare-sd-cards && uv run python ../patch-ear/tools/export_bento_list.py ../patch-ear/lists/bento_shipped.txt
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # must be run from prepare-sd-cards (python only adds the script dir)
import bento

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("# generated from prepare-sd-cards/bento.py BENTO_PATCHES; re-run tools/export_bento_list.py after editing it\n"
               + "\n".join(bento.BENTO_PATCHES) + "\n")
print(f"{len(bento.BENTO_PATCHES)} patches -> {out}")
