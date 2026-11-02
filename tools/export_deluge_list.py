"""Write the list of presets the Deluge build ships (DELUGE_PATCHES in prepare-sd-cards/deluge.py).

Run with prepare-sd-cards' environment, from that repo:
    cd ../prepare-sd-cards && uv run python ../patch-ear/tools/export_deluge_list.py ../patch-ear/lists/deluge_selection.txt
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # must be run from prepare-sd-cards (python only adds the script dir)
import deluge

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("# generated from prepare-sd-cards/deluge.py DELUGE_PATCHES; re-run tools/export_deluge_list.py after editing it\n"
               + "\n".join(deluge.DELUGE_PATCHES) + "\n")
print(f"{len(deluge.DELUGE_PATCHES)} presets -> {out}")
