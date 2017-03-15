#!/usr/bin/env python3

import re

path = 'text.txt'

print(path)

with open(path, encoding='utf8') as inp:
    text = re.sub(r'[\W\d]+', ' ', inp.read().lower().strip()).split()
with open(path.replace('.txt', '-words.txt'), 'w', encoding='utf8') as out:
    out.write(" ".join(text))

