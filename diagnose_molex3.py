import re, sys
sys.stdout.reconfigure(encoding="utf-8")

extracted_name = "Molex-Japan, LLC"
ext_lower = extracted_name.lower().strip()
ext_clean = re.sub(r"[^a-z0-9\s]", " ", ext_lower)
ext_clean = " ".join(ext_clean.split())
print(f"ext_clean = '{ext_clean}'")

ALIASES = {
    "molex interconnect": "Shanghai Molex",
    "molex interconnect (shanghai)": "Shanghai Molex",
    "molex japan": "NIHON MOLEX",
    "molex japan llc": "NIHON MOLEX",
}

for alias, canonical in ALIASES.items():
    alias_clean = re.sub(r"[^a-z0-9\s]", " ", alias.lower())
    alias_clean = " ".join(alias_clean.split())
    print(f"  Checking alias '{alias_clean}' in '{ext_clean}': {alias_clean in ext_clean}")
    if alias_clean in ext_clean:
        print(f"  -> MATCHED! Resolves to '{canonical}'")
