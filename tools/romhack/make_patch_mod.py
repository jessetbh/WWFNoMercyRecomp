"""Package a ROM hack as a recomp ROM-patch mod (.nrm).

The tool is shared by all six AKI ports and lives in AkiMods (https://github.com/jessetbh/AkiMods,
tools/romhack/make_patch_mod.py). This wrapper runs it with the same arguments, from this repository
root (the tool reads this game's RecompiledFuncs/):

    python tools/romhack/make_patch_mod.py --base nomercy.z64 --hack hack.z64 \\
        --game-id wwfnomercy --id my_hack --name "My Hack" --out my_hack.nrm

It looks for the tool in AKIMODS_DIR (an AkiMods checkout), then in a sibling ..\\AkiMods folder.
"""
import os, runpy, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TOOL = os.path.join('tools', 'romhack', 'make_patch_mod.py')


def find_tool():
    roots = []
    if os.environ.get('AKIMODS_DIR'):
        roots.append(os.environ['AKIMODS_DIR'])
    roots.append(os.path.join(os.path.dirname(REPO), 'AkiMods'))
    for root in roots:
        path = os.path.join(root, TOOL)
        if os.path.isfile(path):
            return path
    sys.exit('make_patch_mod.py lives in AkiMods now. Get it with\n'
             '    git clone https://github.com/jessetbh/AkiMods\n'
             'next to this repository, or set AKIMODS_DIR to an AkiMods checkout.\n'
             'Looked in: ' + ', '.join(os.path.join(r, TOOL) for r in roots))


if __name__ == '__main__':
    tool = find_tool()
    sys.argv[0] = tool
    runpy.run_path(tool, run_name='__main__')
