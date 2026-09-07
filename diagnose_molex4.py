from difflib import SequenceMatcher
import sys; sys.stdout.reconfigure(encoding="utf-8")

lookup = "nihon molex"
names_in_testmp = [
    "NIHON MOLEX NIHON MOLEX",         # doubled (how it appears in Test MP)
    "NIHON MOLEX",                      # what it should be
]
for name in names_in_testmp:
    v_lower = name.lower()
    score = SequenceMatcher(None, lookup, v_lower).ratio()
    print(f"  '{lookup}' vs '{v_lower}'  =>  score={score:.4f}  pass={score >= 0.65}")
