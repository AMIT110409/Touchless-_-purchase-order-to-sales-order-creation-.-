"""
Simple script: run PP-StructureV3 (via PaddleX) on all test images
and save the plain OCR text/tables per image for manual inspection.
"""
from pathlib import Path

from paddlex import create_pipeline


def ppstructure_to_markdown(pipeline_result_json: dict) -> str:
    """
    Convert PaddleX LayoutParsingResultV2.json into readable text/HTML.
    """
    res = (pipeline_result_json or {}).get("res") or {}
    blocks = res.get("parsing_res_list") or []

    parts: list[str] = []
    for b in blocks:
        label = (b.get("block_label") or "").lower()
        content = b.get("block_content") or ""
        if not content:
            continue

        if label == "table" and "<table" in content.lower():
            parts.append("```html\n" + content + "\n```")
        else:
            parts.append(content)

    return "\n\n".join(parts).strip()


print("Initializing PP-StructureV3 PaddleX pipeline...")
structure_pipeline = create_pipeline(pipeline="PP-StructureV3")

test_folder = Path("test_data")
files = sorted(list(test_folder.glob("*.png")))

print(f"\nFound {len(files)} files in {test_folder}\n")

for idx, file_path in enumerate(files):
    print(f"\n{'='*80}")
    print(f"File {idx+1}: {file_path.name}")
    print('='*80)

    try:
        gen = structure_pipeline.predict(str(file_path))
        first = next(gen, None)
        first_json = getattr(first, "json", None)
        content_md = ppstructure_to_markdown(first_json if isinstance(first_json, dict) else {})

        output_file = f"ocr_output_{idx}.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"Source: {file_path.name}\n")
            f.write(f"Length: {len(content_md)} characters\n")
            f.write(f"\n{'-'*80}\n")
            f.write(content_md)

        print(f"OCR Content Length: {len(content_md)} characters")
        print(f"First 200 chars: {content_md[:200]}")
        print(f"Saved to: {output_file}")

    except Exception as e:
        print(f"ERROR processing {file_path.name}: {e}")

print("\n\nDone. Check ocr_output_*.txt files to compare PP-Structure OCR text per image.")
