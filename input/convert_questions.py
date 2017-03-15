#!/usr/bin/env python3

path = "questions.txt"
flag = -1
text = []
res = []
header = []

with open(path, "r", encoding="utf8") as inp:
    for line in inp:
        line = line.strip()
        if not line:
            continue
        if line[0] == ':':
            header.append(line[1:])
            flag += 1
            continue
        if flag > len(text) - 1:
            text.append([])
        text[flag].append(line.split())
for t in range(len(text)):
    for x in range(len(text[t])):
        for y in range(len(text[t])):
            if x == y:
                continue
            if t > len(res) - 1:
                res.append([])
            res[t].append(text[t][x] + text[t][y])

with open(path.replace(".txt", "_encode.txt"), "w", encoding="utf8") as out:
    for r in range(len(res)):
        if r:
            out.write('\n')
        out.write(":%s\n" % header[r])
        out.write("\n".join(' '.join(t) for t in res[r]))
        out.write("\n")
