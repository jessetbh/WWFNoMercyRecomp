# Starter model packs: credits and licences

The model packs bundled with aki_modelimport (aki-roadmap#145). Every one is CC0 1.0 (public domain dedication,
https://creativecommons.org/publicdomain/zero/1.0/): no attribution is required, but we credit the authors anyway.

| Pack id | Name | Author | Licence | Source |
|---|---|---|---|---|
| q-warrior | Warrior | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/lowpoly-rpg-characters |
| q-monk | Monk | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/lowpoly-rpg-characters |
| q-rogue | Rogue | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/lowpoly-rpg-characters |
| q-ranger | Ranger | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/lowpoly-rpg-characters |
| q-cleric | Cleric | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/lowpoly-rpg-characters |
| q-wizard | Wizard | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/lowpoly-rpg-characters |
| q-human | Animated Human | Quaternius (quaternius.com) | CC0 1.0 | https://opengameart.org/content/animated-human-low-poly |
| q-robot | Robot Expressive | Tomas Laulhe (Quaternius), modified by Don McCurdy (three.js examples) | CC0 1.0 | https://github.com/mrdoob/three.js/tree/dev/examples/models/gltf/RobotExpressive |

The RPG characters were imported from their .blend files with their weapons left out. All packs were built with
AkiTools `aki-model.py batch <list>.json --game all` (aki-roadmap#149: baked light keeping each pack's average
brightness), at each game's stock wrestler height.
`aki_starter_packs_hd.rtz` holds their HD textures, merged by `tools\merge_rtz.py`.
