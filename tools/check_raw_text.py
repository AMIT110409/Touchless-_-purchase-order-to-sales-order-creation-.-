import fitz

def extract_text(pdf_path):
    print(f"\n--- Extracting {pdf_path} ---")
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        print(f"Extracted {len(text)} characters.")
        print("First 500 chars:")
        print(text[:500])
        print("Last 500 chars:")
        print(text[-500:])
    except Exception as e:
        print(f"Error: {e}")

extract_text(r"test_unmapped\Bestellung BE-26-00192.pdf")
extract_text(r"test_unmapped\Ecoform Multifol (Sold-to 4020000777 - Material 11284).PDF")
