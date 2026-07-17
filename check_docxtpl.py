import docxtpl
with open(docxtpl.__file__, "r") as f:
    print([line for line in f.readlines() if 'tr ' in line or '{%tr' in line or '{% tr' in line][:20])
