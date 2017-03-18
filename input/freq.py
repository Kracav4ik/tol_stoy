def get_nums(path):
    with open(path) as f:
        return [int(s.strip().split()[-1]) for s in f][1:]


w100_f = get_nums('../../baseline/vocab.txt')
text_f = get_nums('../savedata/vocab.txt')

print(w100_f[:100])
print(text_f[:100])
